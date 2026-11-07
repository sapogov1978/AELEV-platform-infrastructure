"""Provision Grafana's owner-controlled operator OIDC client."""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, cast

LOGGER = logging.getLogger(__name__)
PREFIX = "AELEV_INFRASTRUCTURE_GRAFANA_"
OPERATOR_USERNAME = "platform-operator"
OPERATOR_ROLE = "grafana.operator"
OPERATOR_ROLE_DESCRIPTION = "Grafana organization operator"
OIDC_CA = Path("/run/secrets/oidc/ca.crt")
OIDC_SECRET = Path("/run/secrets/oidc/client-secret")
ADMIN_ROOT = Path("/run/secrets/status-admin")


class GrafanaOidcCaPermissionError(PermissionError):
    """The one-off role cannot read its public IdP trust input."""


class GrafanaAdminSecretPermissionError(PermissionError):
    """The one-off role cannot read its deployment-admin files."""


class GrafanaOidcOutputPermissionError(PermissionError):
    """The one-off role cannot write its owner-local client secret."""


def required(name: str) -> str:
    """Read one non-secret required setting."""
    value = os.environ.get(PREFIX + name, "").strip()
    if not value:
        raise RuntimeError("GRAFANA_OIDC_SETTING_MISSING:" + name)
    return value


def exact_operator() -> str:
    """Accept only the canonical human operator identity."""
    username = required("OIDC_OPERATOR_USER")
    if username != OPERATOR_USERNAME:
        raise RuntimeError("GRAFANA_OPERATOR_MUST_BE_PLATFORM_OPERATOR")
    return username


def https_origin(name: str) -> str:
    """Validate a credential-free HTTPS origin without a path."""
    value = required(name).rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("GRAFANA_OIDC_HTTPS_ORIGIN_REQUIRED:" + name)
    return value


def client_description(client_id: str, public_url: str) -> dict[str, Any]:
    """Return the exact confidential Authorization Code client declaration."""
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}", client_id):
        raise RuntimeError("GRAFANA_OIDC_CLIENT_ID_INVALID")
    origin = public_url.rstrip("/")
    return {
        "clientId": client_id,
        "name": "DRP Grafana",
        "enabled": True,
        "publicClient": False,
        "clientAuthenticatorType": "client-secret",
        "standardFlowEnabled": True,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "fullScopeAllowed": False,
        "redirectUris": [origin + "/login/generic_oauth"],
        "webOrigins": [origin],
        "attributes": {
            "pkce.code.challenge.method": "S256",
            "post.logout.redirect.uris": origin + "/*",
            "aelev.owner": "grafana",
            "aelev.purpose": "operator-ui",
        },
        "protocolMappers": [
            {
                "name": "grafana-audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {
                    "included.custom.audience": "grafana",
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                },
            }
        ],
        "defaultClientScopes": ["profile", "email", "roles"],
    }


def _call(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    body: object | None = None,
    form: dict[str, str] | None = None,
) -> tuple[int, object | None]:
    headers: dict[str, str] = {}
    data: bytes | None = None
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, separators=(",", ":")).encode()
    elif form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return 404, None
        raise RuntimeError("GRAFANA_OIDC_ADMIN_HTTP_" + str(error.code)) from error


def _write_secret(value: str, destination: Path = OIDC_SECRET) -> None:
    """Replace the client secret atomically without exposing its bytes."""
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = destination.with_suffix(".pending")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(value + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except PermissionError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise GrafanaOidcOutputPermissionError from error
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_admin(name: str) -> str:
    try:
        return (ADMIN_ROOT / name).read_text().strip()
    except PermissionError as error:
        raise GrafanaAdminSecretPermissionError from error


def provision() -> None:
    """Reconcile only Grafana's client, role, and exact operator grant."""
    username = exact_operator()
    internal_url = https_origin("OIDC_INTERNAL_URL")
    public_url = https_origin("OIDC_PUBLIC_URL")
    realm_name = required("OIDC_REALM")
    client_id = required("OIDC_CLIENT_ID")
    try:
        context = ssl.create_default_context(cafile=str(OIDC_CA))
    except PermissionError as error:
        raise GrafanaOidcCaPermissionError from error
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
    _, token_response = _call(
        opener,
        internal_url + "/realms/master/protocol/openid-connect/token",
        method="POST",
        form={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": _read_admin("admin-user"),
            "password": _read_admin("admin-password"),
        },
    )
    if not isinstance(token_response, dict) or not isinstance(
        token_response.get("access_token"), str
    ):
        raise RuntimeError("GRAFANA_OIDC_ADMIN_TOKEN_INVALID")
    token = cast(str, token_response["access_token"])
    realm = internal_url + "/admin/realms/" + urllib.parse.quote(realm_name, safe="")

    role_path = "/roles/" + urllib.parse.quote(OPERATOR_ROLE, safe="")
    status, role = _call(opener, realm + role_path, token=token)
    if status == 404:
        _call(
            opener,
            realm + "/roles",
            method="POST",
            token=token,
            body={"name": OPERATOR_ROLE, "description": OPERATOR_ROLE_DESCRIPTION},
        )
        _, role = _call(opener, realm + role_path, token=token)
    if (
        not isinstance(role, dict)
        or role.get("description") != OPERATOR_ROLE_DESCRIPTION
    ):
        raise RuntimeError("GRAFANA_OIDC_ROLE_OWNER_CONFLICT")

    clients_path = "/clients?clientId=" + urllib.parse.quote(client_id, safe="")
    _, client_matches = _call(opener, realm + clients_path, token=token)
    if not isinstance(client_matches, list) or len(client_matches) > 1:
        raise RuntimeError("GRAFANA_OIDC_CLIENT_AMBIGUOUS")
    description = client_description(client_id, public_url)
    if client_matches:
        current = cast(dict[str, Any], client_matches[0])
        attributes = current.get("attributes", {})
        if (
            not isinstance(attributes, dict)
            or attributes.get("aelev.owner") != "grafana"
        ):
            raise RuntimeError("GRAFANA_OIDC_CLIENT_OWNER_CONFLICT")
        _call(
            opener,
            realm + "/clients/" + str(current["id"]),
            method="PUT",
            token=token,
            body=description,
        )
    else:
        _call(
            opener,
            realm + "/clients",
            method="POST",
            token=token,
            body=description,
        )
        _, created = _call(opener, realm + clients_path, token=token)
        if not isinstance(created, list) or len(created) != 1:
            raise RuntimeError("GRAFANA_OIDC_CLIENT_CREATE_FAILED")
        current = cast(dict[str, Any], created[0])

    client_path = "/clients/" + str(current["id"])
    _call(
        opener,
        realm + client_path + "/scope-mappings/realm",
        method="POST",
        token=token,
        body=[role],
    )

    _, users = _call(
        opener,
        realm
        + "/users?username="
        + urllib.parse.quote(username, safe="")
        + "&exact=true",
        token=token,
    )
    if not isinstance(users, list) or len(users) != 1 or not users[0].get("enabled"):
        raise RuntimeError("GRAFANA_OPERATOR_NOT_FOUND_OR_DISABLED")
    _call(
        opener,
        realm + "/users/" + str(users[0]["id"]) + "/role-mappings/realm",
        method="POST",
        token=token,
        body=[role],
    )

    _, secret_response = _call(
        opener, realm + client_path + "/client-secret", token=token
    )
    if not isinstance(secret_response, dict) or not isinstance(
        secret_response.get("value"), str
    ):
        raise RuntimeError("GRAFANA_OIDC_CLIENT_SECRET_INVALID")
    secret = cast(str, secret_response["value"])
    if not secret:
        raise RuntimeError("GRAFANA_OIDC_CLIENT_SECRET_INVALID")
    _write_secret(secret)
    LOGGER.info(json.dumps({"event": "grafana_operator_oidc_configured"}))


def main() -> None:
    """Run provisioning with a secret-free failure type for lifecycle evidence."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        provision()
    except Exception as error:
        print("Provisioning failed: " + type(error).__name__, file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
