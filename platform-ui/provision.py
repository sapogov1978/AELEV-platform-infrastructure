"""One-off owner-only OIDC provisioning; administrative identity stays out of runtime."""

from __future__ import annotations

import json
import ssl
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


def apply(config: dict[str, str], secret_root: Path) -> None:
    context = ssl.create_default_context(cafile=str(secret_root / "oidc-ca.crt"))
    with httpx.Client(
        base_url=config["url"],
        verify=context,
        timeout=20,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        token = client.post(
            "/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": config["admin_user"],
                "password": (secret_root / "admin-password").read_text().strip(),
            },
        )
        token.raise_for_status()
        client.headers["Authorization"] = "Bearer " + token.json()["access_token"]
        realm = "/admin/realms/" + quote(config["realm"], safe="")

        def request(method: str, path: str, **kwargs: Any) -> httpx.Response:
            response = client.request(method, realm + path, **kwargs)
            response.raise_for_status()
            return response

        for role in ("platform-ui-viewer", "platform-ui-publisher"):
            response = client.get(realm + "/roles/" + role)
            if response.status_code == 404:
                request(
                    "POST",
                    "/roles",
                    json={
                        "name": role,
                        "description": "Technical platform-ui permission",
                    },
                )
            else:
                response.raise_for_status()
        client_id = config["client_id"]
        description = {
            "clientId": client_id,
            "name": "DRP Platform UI",
            "enabled": True,
            "publicClient": True,
            "standardFlowEnabled": True,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False,
            "redirectUris": [config["public_url"] + "/*"],
            "webOrigins": [config["public_url"]],
            "attributes": {
                "pkce.code.challenge.method": "S256",
                "post.logout.redirect.uris": config["public_url"] + "/*",
                "aelev.owner": "platform-ui",
            },
            "protocolMappers": [
                {
                    "name": "platform-ui-audience",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-audience-mapper",
                    "config": {
                        "included.client.audience": client_id,
                        "access.token.claim": "true",
                        "id.token.claim": "false",
                    },
                }
            ],
        }
        matches = request("GET", "/clients", params={"clientId": client_id}).json()
        if matches:
            if matches[0].get("attributes", {}).get("aelev.owner") != "platform-ui":
                raise ValueError(
                    "refusing to replace an OIDC client owned by another deployment"
                )
            request("PUT", "/clients/" + matches[0]["id"], json=description)
        else:
            request("POST", "/clients", json=description)
        username = config["operator_user"]
        users = request(
            "GET", "/users", params={"username": username, "exact": "true"}
        ).json()
        created = not users
        if not users:
            request(
                "POST",
                "/users",
                json={
                    "username": username,
                    "enabled": True,
                    "firstName": "DRP",
                    "lastName": "Operator",
                    "email": username + "@bootstrap.invalid",
                    "attributes": {"aelev.owner": ["platform-ui"]},
                    "credentials": [
                        {
                            "type": "password",
                            "temporary": False,
                            "value": (secret_root / "operator-password")
                            .read_text()
                            .strip(),
                        }
                    ],
                },
            )
            users = request(
                "GET", "/users", params={"username": username, "exact": "true"}
            ).json()
        role = request("GET", "/roles/platform-ui-viewer").json()
        if not created:
            granted = request(
                "GET", "/users/" + users[0]["id"] + "/role-mappings/realm"
            ).json()
            if not any(value["id"] == role["id"] for value in granted):
                raise ValueError(
                    "existing operator has no viewer grant; explicit administration required"
                )
            print("Own OIDC client and existing authorized operator verified.")
            return
        request(
            "POST", "/users/" + users[0]["id"] + "/role-mappings/realm", json=[role]
        )
    print("Own OIDC client, roles and operator are configured.")


if __name__ == "__main__":
    try:
        apply(json.loads(Path(sys.argv[1]).read_text()), Path("/run/secrets/provision"))
    except Exception as error:
        print("Provisioning failed: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
