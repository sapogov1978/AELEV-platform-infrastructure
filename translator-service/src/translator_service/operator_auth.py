"""Owner-scoped OIDC authorization for operator reads and translation."""

from __future__ import annotations

import os
import re
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import jwt
from aelev_platform_ui_client import https_url
from fastapi import HTTPException

PREFIX = "AELEV_INFRASTRUCTURE_TRANSLATOR_"
ROLE = "operator-read"
TRANSLATE_ROLE = "operator-translate"


@dataclass(frozen=True)
class Settings:
    issuer: str
    backchannel: str
    client_id: str
    ca_file: str

    @property
    def realm(self) -> str:
        return urlsplit(self.issuer).path.removeprefix("/realms/")

    @classmethod
    def environment(cls) -> Settings:
        issuer = https_url(os.environ[PREFIX + "API_OIDC_ISSUER"])
        backchannel = https_url(os.environ[PREFIX + "API_OIDC_BACKCHANNEL_URL"])
        parsed = urlsplit(issuer)
        if not re.fullmatch(r"/realms/[A-Za-z0-9._-]+", parsed.path) or parsed.query:
            raise ValueError("TRANSLATOR_OPERATOR_ISSUER_INVALID")
        remote = urlsplit(backchannel)
        if remote.path not in {"", "/"} or remote.query:
            raise ValueError("TRANSLATOR_OPERATOR_BACKCHANNEL_REQUIRES_ORIGIN")
        client_id = os.environ[PREFIX + "API_OIDC_CLIENT_ID"]
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", client_id):
            raise ValueError("TRANSLATOR_OPERATOR_CLIENT_INVALID")
        ca_name = os.environ[PREFIX + "API_OIDC_CA_FILE"]
        if Path(ca_name).name != ca_name or ca_name in {"", ".", ".."}:
            raise ValueError("TRANSLATOR_OPERATOR_CA_FILENAME_REQUIRED")
        ca_file = "/run/secrets/translator-api/" + ca_name
        ssl.create_default_context(cafile=ca_file)
        return cls(issuer.rstrip("/"), backchannel.rstrip("/"), client_id, ca_file)


class Verifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.keys = jwt.PyJWKClient(
            settings.backchannel
            + "/realms/"
            + settings.realm
            + "/protocol/openid-connect/certs",
            ssl_context=ssl.create_default_context(cafile=settings.ca_file),
            timeout=5,
            lifespan=300,
        )

    def verify(self, token: str, role: str = ROLE) -> str:
        """Validate the exact permission and return a pseudonymous actor identity."""
        try:
            if (
                len(token) > 16384
                or jwt.get_unverified_header(token).get("alg") != "RS256"
            ):
                raise ValueError("unsupported access token")
            key = self.keys.get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.settings.issuer,
                audience=self.settings.client_id,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
            if (
                claims.get("typ") != "Bearer"
                or not isinstance(claims["sub"], str)
                or not claims["sub"]
                or claims.get("azp") != self.settings.client_id
            ):
                raise ValueError("own access token required")
        except jwt.PyJWKClientConnectionError as error:
            raise HTTPException(503, "identity trust unavailable") from error
        except (jwt.PyJWTError, ValueError, KeyError, TypeError) as error:
            raise HTTPException(
                401, "invalid access token", headers={"WWW-Authenticate": "Bearer"}
            ) from error
        access = claims.get("resource_access")
        client: Any = (
            cast(dict[str, Any], access).get(self.settings.client_id)
            if isinstance(access, dict)
            else None
        )
        roles: Any = (
            cast(dict[str, Any], client).get("roles")
            if isinstance(client, dict)
            else None
        )
        if not isinstance(roles, list) or role not in roles:
            detail = (
                "operator read permission required"
                if role == ROLE
                else "operator translate permission required"
            )
            raise HTTPException(403, detail)
        from translator_service.contracts import digest

        return digest((self.settings.issuer + "\0" + claims["sub"]).encode())
