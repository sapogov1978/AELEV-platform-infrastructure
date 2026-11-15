#!/usr/bin/env python3
"""Issue a new owner's deployment tokens through local Grafana OIDC and API."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import ssl
import stat
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import uuid4


class ProvisionError(RuntimeError):
    """Public code; response bodies and credentials never enter diagnostics."""


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or any(c.isspace() or ord(c) < 32 for c in value)
        or "\\" in value
    ):
        raise ProvisionError("GRAFANA_LOCAL_HTTPS_ORIGIN_REQUIRED")
    return value.rstrip("/")


def private(path: Path) -> None:
    mode = path.lstat().st_mode
    if (
        not stat.S_ISREG(mode)
        or (os.name != "nt" and mode & 0o077)
        or path.stat().st_size > 65536
    ):
        raise ProvisionError("GRAFANA_LOCAL_PRIVATE_FILE_REQUIRED")


def save(path: Path, value: bytes, *, replace: bool = False) -> None:
    pending = path.with_name(".pending-" + uuid4().hex)
    try:
        with os.fdopen(
            os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb"
        ) as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(pending, path)
        else:
            os.link(pending, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        pending.unlink(missing_ok=True)


def journal(path: Path, value: dict[str, Any], *, replace: bool = False) -> None:
    save(
        path,
        (json.dumps(value, sort_keys=True, indent=2) + "\n").encode(),
        replace=replace,
    )


class Redirects(HTTPRedirectHandler):
    def __init__(self, allowed: set[str]) -> None:
        super().__init__()
        self.allowed = allowed

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        parsed = urlsplit(newurl)
        if (
            parsed.scheme + "://" + parsed.netloc not in self.allowed
            or parsed.username
            or parsed.password
            or "\\" in newurl
        ):
            raise ProvisionError("GRAFANA_LOCAL_REDIRECT_REJECTED")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        previous = urlsplit(req.full_url)
        if redirected is not None and (previous.scheme, previous.netloc) != (
            parsed.scheme,
            parsed.netloc,
        ):
            # A navigation back from the IdP must not present the IdP's Origin
            # as the source of an API request to Grafana (its CSRF boundary).
            redirected.remove_header("Origin")
        return redirected


class LoginForm(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action = ""
        self.values: dict[str, str] = {}
        self.active = False

    def handle_starttag(
        self, tag: str, attributes: list[tuple[str, str | None]]
    ) -> None:
        attrs = dict(attributes)
        if tag == "form" and attrs.get("id") == "kc-form-login":
            if self.action:
                raise ProvisionError("GRAFANA_LOCAL_LOGIN_FORM_INVALID")
            self.action = attrs.get("action") or ""
            self.active = True
        if tag == "input" and self.active and attrs.get("type") == "hidden":
            name = attrs.get("name")
            if name:
                self.values[name] = attrs.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.active = False


class Operator:
    def __init__(self, grafana: str, identity: str, trust: Path) -> None:
        self.grafana, self.identity = origin(grafana), origin(identity)
        context = ssl.create_default_context(cafile=str(trust))
        self.opener = build_opener(
            HTTPSHandler(context=context),
            HTTPCookieProcessor(http.cookiejar.CookieJar()),
            Redirects({self.grafana, self.identity}),
            ProxyHandler({}),
        )

    def request(self, request: Request) -> tuple[int, bytes]:
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(1048577)
                if len(raw) > 1048576:
                    raise ProvisionError("GRAFANA_LOCAL_RESPONSE_LIMIT")
                return response.status, raw
        except HTTPError as error:
            # A POST may have been committed even if its response was lost.
            raise ProvisionError("GRAFANA_LOCAL_HTTP_" + str(error.code)) from None
        except URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                raise ProvisionError("GRAFANA_LOCAL_TLS_CERTIFICATE_REJECTED") from None
            if isinstance(error.reason, ssl.SSLError):
                raise ProvisionError("GRAFANA_LOCAL_TLS_HANDSHAKE_FAILED") from None
            if isinstance(error.reason, ConnectionRefusedError):
                raise ProvisionError("GRAFANA_LOCAL_ENDPOINT_UNREACHABLE") from None
            raise ProvisionError("GRAFANA_LOCAL_RESULT_UNKNOWN") from None
        except (TimeoutError, OSError):
            raise ProvisionError("GRAFANA_LOCAL_RESULT_UNKNOWN") from None

    def call(self, method: str, path: str, value: dict[str, Any] | None = None) -> Any:
        if not path.startswith("/api/") or "?" in path or "#" in path:
            raise ProvisionError("GRAFANA_LOCAL_API_PATH_INVALID")
        _, raw = self.request(
            Request(
                self.grafana + path,
                data=json.dumps(value).encode() if value is not None else None,
                method=method,
                headers={"Content-Type": "application/json", "Origin": self.grafana},
            )
        )
        try:
            return json.loads(raw)
        except ValueError:
            raise ProvisionError("GRAFANA_LOCAL_RESPONSE_INVALID") from None

    def login(self, username: str, password_file: Path, realm: str) -> None:
        if (
            username != "platform-operator"
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", realm) is None
        ):
            raise ProvisionError("GRAFANA_LOCAL_OPERATOR_INVALID")
        private(password_file)
        _, raw = self.request(Request(self.grafana + "/login/generic_oauth"))
        form = LoginForm()
        form.feed(raw.decode())
        target = urlsplit(form.action)
        if (
            target.scheme + "://" + target.netloc != self.identity
            or target.path != "/realms/" + realm + "/login-actions/authenticate"
            or target.username
            or target.password
            or target.fragment
        ):
            raise ProvisionError("GRAFANA_LOCAL_LOGIN_FORM_INVALID")
        password = password_file.read_text().strip()
        if not password or len(password) > 1024:
            raise ProvisionError("GRAFANA_LOCAL_OPERATOR_INVALID")
        form.values.update(username=username, password=password, credentialId="")
        self.request(
            Request(
                form.action,
                data=urlencode(form.values).encode(),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": self.identity,
                },
            )
        )
        user = self.call("GET", "/api/user")
        orgs = self.call("GET", "/api/user/orgs")
        if (
            user.get("login") != username
            or user.get("isGrafanaAdmin") is not False
            or [org["role"] for org in orgs if org["orgId"] == user["orgId"]]
            != ["Admin"]
        ):
            raise ProvisionError("GRAFANA_LOCAL_OPERATOR_ROLE_INVALID")


def issue(
    operator: Operator, output: Path, folder: str, title: str, alert: bool
) -> dict[str, Any]:
    if (
        re.fullmatch(r"[a-z][a-z0-9-]{0,39}", folder) is None
        or not 1 <= len(title) <= 120
        or any(ord(c) < 32 for c in title)
    ):
        raise ProvisionError("GRAFANA_LOCAL_OWNER_INVALID")
    # A new operation has a new private directory. Never overwrite credentials or
    # replay an ambiguous one-time token issue, including after a process crash.
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    if os.name != "nt":
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    receipt = output / "receipt.json"
    state: dict[str, Any] = {
        "schema_version": 1,
        "owner": "grafana-local-publisher",
        "operation": str(uuid4()),
        "grafana": operator.grafana,
        "folder": folder,
        "status": "IN_PROGRESS",
    }
    journal(receipt, state)

    def mutate(method: str, path: str, value: dict[str, Any]) -> Any:
        state["pending"] = {"method": method, "path": path}
        journal(receipt, state, replace=True)
        result = operator.call(method, path, value)
        state.pop("pending")
        return result

    try:
        folder_row = mutate("POST", "/api/folders", {"uid": folder, "title": title})
        if folder_row["uid"] != folder:
            raise ProvisionError("GRAFANA_LOCAL_RESPONSE_INVALID")
        for role, name, lifetime in [("Viewer", "publisher", 86400)] + (
            [("Admin", "provisioner", 900)] if alert else []
        ):
            account_name = folder + "-" + name + "-" + state["operation"]
            account = mutate(
                "POST", "/api/serviceaccounts", {"name": account_name, "role": role}
            )
            identifier = account["id"]
            if (
                type(identifier) is not int
                or identifier <= 0
                or account["role"] != role
            ):
                raise ProvisionError("GRAFANA_LOCAL_RESPONSE_INVALID")
            state[name] = {"account_id": identifier, "account_name": account_name}
            if role == "Viewer":
                mutate(
                    "POST",
                    "/api/folders/" + folder + "/permissions",
                    {"items": [{"userId": identifier, "permission": 2}]},
                )
            token = mutate(
                "POST",
                f"/api/serviceaccounts/{identifier}/tokens",
                {"name": name + "-" + state["operation"], "secondsToLive": lifetime},
            )
            if (
                type(token["id"]) is not int
                or not isinstance(token["key"], str)
                or not token["key"]
            ):
                raise ProvisionError("GRAFANA_LOCAL_RESPONSE_INVALID")
            save(output / (name + ".token"), (token["key"] + "\n").encode())
            state[name]["token_id"] = token["id"]
            journal(receipt, state, replace=True)
        state["status"] = "TOKENS_ISSUED"
        journal(receipt, state, replace=True)
        return state
    except Exception:
        state["status"] = "INSPECTION_REQUIRED"
        journal(receipt, state, replace=True)
        raise


def retire(operator: Operator, output: Path) -> dict[str, Any]:
    receipt = output / "receipt.json"
    private(receipt)
    state: dict[str, Any] = json.loads(receipt.read_text())
    if not isinstance(state, dict):
        raise ProvisionError("GRAFANA_LOCAL_RECEIPT_INVALID")
    if (
        state.get("owner") != "grafana-local-publisher"
        or state.get("schema_version") != 1
        or state.get("grafana") != operator.grafana
        or state.get("status")
        not in {"TOKENS_ISSUED", "PROVISIONER_RETIRED", "RETIREMENT_PENDING"}
    ):
        raise ProvisionError("GRAFANA_LOCAL_RECEIPT_INVALID")
    if state["status"] == "PROVISIONER_RETIRED":
        return state
    provisioner = state.get("provisioner")
    if not isinstance(provisioner, dict):
        raise ProvisionError("GRAFANA_LOCAL_PROVISIONER_MISSING")
    identifier = provisioner.get("account_id")
    if type(identifier) is not int or identifier <= 0:
        raise ProvisionError("GRAFANA_LOCAL_RECEIPT_INVALID")
    path = f"/api/serviceaccounts/{identifier}"
    try:
        account = operator.call("GET", path)
    except ProvisionError as error:
        if (
            str(error) != "GRAFANA_LOCAL_HTTP_404"
            or state["status"] != "RETIREMENT_PENDING"
        ):
            raise
    else:
        if account["name"] != provisioner["account_name"] or account["role"] != "Admin":
            raise ProvisionError("GRAFANA_LOCAL_PROVISIONER_MISMATCH")
        state["status"] = "RETIREMENT_PENDING"
        journal(receipt, state, replace=True)
        # Removing this exact temporary identity revokes all of its tokens.
        operator.call("DELETE", path)
    state["status"] = "PROVISIONER_RETIRED"
    journal(receipt, state, replace=True)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("issue", "retire-provisioner"))
    parser.add_argument("--environment", choices=("local", "test"), required=True)
    parser.add_argument("--grafana-url", required=True)
    parser.add_argument("--identity-url", required=True)
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--operator-password-file", type=Path, required=True)
    parser.add_argument("--realm", default="aelev")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--folder")
    parser.add_argument("--title")
    parser.add_argument("--with-alert-provisioner", action="store_true")
    args = parser.parse_args()
    try:
        if args.operation == "issue" and (not args.folder or not args.title):
            parser.error("issue requires folder and title")
        operator = Operator(args.grafana_url, args.identity_url, args.ca_file)
        operator.login("platform-operator", args.operator_password_file, args.realm)
        value = (
            issue(
                operator,
                args.output,
                args.folder,
                args.title,
                args.with_alert_provisioner,
            )
            if args.operation == "issue"
            else retire(operator, args.output)
        )
        print(
            json.dumps({key: value[key] for key in ("status", "operation", "folder")})
        )
        return 0
    except (ProvisionError, OSError, ValueError, KeyError, TypeError) as error:
        print(
            str(error)
            if isinstance(error, ProvisionError)
            else "GRAFANA_LOCAL_OPERATION_FAILED",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
