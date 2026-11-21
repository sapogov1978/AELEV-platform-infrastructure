"""Publish only this provider's components; no discovery of clients or Docker."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from aelev_platform_ui_client import Settings, aggregate, observe, run

from api_contract import swagger_url
from health import PREFIX, probe

NETWORK_SUFFIXES = ("URL", "SERVER_NAME", "CA_FILE")
NETWORK_PREFIX = PREFIX + "UI_GATEWAY_PROBE_"


def network_name(value: str) -> str:
    if not value.isascii() or len(value) > 253 or "%" in value:
        raise ValueError("GATEWAY_PUBLISHER_NAME_INVALID")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
            for label in value.split(".")
        ):
            raise ValueError("GATEWAY_PUBLISHER_NAME_INVALID") from None
    return value


def network_gateway() -> tuple[str, int, str, str] | None:
    """Select explicit HTTPS inputs; partial profiles never select legacy."""
    profile = os.environ.get(PREFIX + "UI_PROBE_PROFILE")
    present = any(NETWORK_PREFIX + key in os.environ for key in NETWORK_SUFFIXES)
    if profile is None and not present:
        return None
    if profile != "network":
        raise ValueError("GATEWAY_PUBLISHER_NETWORK_PROFILE_REQUIRED")
    values = {key: os.environ.get(NETWORK_PREFIX + key, "") for key in NETWORK_SUFFIXES}
    if not all(values.values()):
        raise ValueError("GATEWAY_PUBLISHER_NETWORK_INPUTS_REQUIRED")
    raw, server_name, ca = (values[key] for key in NETWORK_SUFFIXES)
    try:
        route = urlsplit(raw)
        port = route.port
        if (
            not raw.isascii()
            or len(raw) > 512
            or any(ord(char) <= 32 or ord(char) == 127 for char in raw)
            or any(char in raw for char in ("\\", "%", "?", "#"))
            or route.scheme != "https"
            or not route.hostname
            or route.username is not None
            or route.password is not None
            or route.path != "/readyz"
            or route.query
            or route.fragment
            or route.netloc.endswith(":")
            or port is not None
            and not 1 <= port <= 65535
        ):
            raise ValueError
    except ValueError:
        raise ValueError("GATEWAY_PUBLISHER_HTTPS_READYZ_REQUIRED") from None
    network_name(server_name)
    path = Path(ca)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or any(ord(char) <= 32 or ord(char) == 127 for char in ca)
    ):
        raise ValueError("GATEWAY_PUBLISHER_PUBLIC_CA_PATH_INVALID")
    return network_name(route.hostname), port or 443, server_name, ca


def gateway_state(cfg: Settings) -> str:
    target = network_gateway()
    if target is not None:
        return probe(*target, cfg.timeout)
    return probe(
        "public-egress-gateway",
        8080,
        os.environ[PREFIX + "SERVER_NAME"],
        "/run/secrets/gateway/server-ca.pem",
        cfg.timeout,
    )


def description(cfg: Settings) -> bytes:
    url = swagger_url(os.environ[PREFIX + "DOCS_PUBLIC_URL"])
    return json.dumps(
        {
            "schema_version": 1,
            "module_id": cfg.module_id,
            "revision": 2,
            "title": {
                "ru": "Контролируемый доступ к публичной сети",
                "en": "Controlled public network access",
            },
            "service_kind": "INFRASTRUCTURE",
            "sections": [],
            "swagger_url": url,
            "components": [
                {
                    "id": "gateway",
                    "swagger_url": url,
                    "title": {
                        "ru": "Public Egress Gateway",
                        "en": "Public Egress Gateway",
                    },
                },
                {
                    "id": "publisher",
                    "title": {"ru": "Издатель состояния", "en": "Status publisher"},
                },
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def observe_provider(cfg: Settings) -> tuple[str, list[dict[str, str]]]:
    components = [
        observe(
            "gateway",
            lambda: gateway_state(cfg),
        )
    ]
    return aggregate(components), components


if __name__ == "__main__":
    settings = Settings.environment(PREFIX)
    run(
        settings,
        "public-egress-gateway-status",
        description(settings),
        lambda: observe_provider(settings),
    )
