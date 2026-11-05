"""Idempotently provision the own PKCE client and separate operator permissions."""

import os
import ssl
from pathlib import Path
from typing import Any

import httpx

from translator_service.api_server import swagger_url
from translator_service.operator_auth import PREFIX, ROLE, TRANSLATE_ROLE, Settings


def provision() -> None:
    settings = Settings.environment()
    user = os.environ[PREFIX + "API_OPERATOR_USER"]
    if not user:
        raise ValueError("TRANSLATOR_OPERATOR_USER_REQUIRED")
    origin = swagger_url().removesuffix("/docs")
    admin = Path("/run/secrets/status-admin")
    with httpx.Client(
        base_url=settings.backchannel,
        verify=ssl.create_default_context(cafile=settings.ca_file),
        timeout=20,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        reply = client.post(
            "/realms/master/protocol/openid-connect/token",
            data={
                "client_id": "admin-cli",
                "grant_type": "password",
                "username": (admin / "admin-user").read_text().strip(),
                "password": (admin / "admin-password").read_text().strip(),
            },
        )
        reply.raise_for_status()
        client.headers["Authorization"] = "Bearer " + reply.json()["access_token"]
        base = "/admin/realms/" + settings.realm

        def request(method: str, path: str, **kwargs: Any) -> httpx.Response:
            response = client.request(method, base + path, **kwargs)
            response.raise_for_status()
            return response

        users = request(
            "GET", "/users", params={"username": user, "exact": "true"}
        ).json()
        if len(users) != 1 or not users[0].get("enabled"):
            raise ValueError("EXISTING_ENABLED_OPERATOR_REQUIRED")
        description = {
            "clientId": settings.client_id,
            "name": "Translator operations",
            "protocol": "openid-connect",
            "enabled": True,
            "publicClient": True,
            "standardFlowEnabled": True,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False,
            "fullScopeAllowed": False,
            "redirectUris": [origin + "/docs/oauth2-redirect.html"],
            "webOrigins": [origin],
            "attributes": {
                "pkce.code.challenge.method": "S256",
                "aelev.owner": "translator-service",
            },
            "protocolMappers": [
                {
                    "name": "operator-audience",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-audience-mapper",
                    "config": {
                        "included.client.audience": settings.client_id,
                        "access.token.claim": "true",
                        "id.token.claim": "false",
                    },
                }
            ],
        }
        matches = request(
            "GET", "/clients", params={"clientId": settings.client_id}
        ).json()
        if matches:
            if (
                len(matches) != 1
                or matches[0].get("attributes", {}).get("aelev.owner")
                != "translator-service"
            ):
                raise ValueError("FOREIGN_OPERATOR_CLIENT")
            client_path = "/clients/" + matches[0]["id"]
            request("PUT", client_path, json=description)
        else:
            request("POST", "/clients", json=description)
            matches = request(
                "GET", "/clients", params={"clientId": settings.client_id}
            ).json()
            client_path = "/clients/" + matches[0]["id"]
        roles: list[dict[str, Any]] = []
        for name in (ROLE, TRANSLATE_ROLE):
            role_path = client_path + "/roles/" + name
            role = client.get(base + role_path)
            if role.status_code == 404:
                request("POST", client_path + "/roles", json={"name": name})
                role = request("GET", role_path)
            else:
                role.raise_for_status()
            roles.append(role.json())
        client_uuid = matches[0]["id"]
        request(
            "POST", client_path + "/scope-mappings/clients/" + client_uuid, json=roles
        )
        request(
            "POST",
            "/users/" + users[0]["id"] + "/role-mappings/clients/" + client_uuid,
            json=roles,
        )


if __name__ == "__main__":
    provision()
