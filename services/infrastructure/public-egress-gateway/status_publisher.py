"""Publish only this provider's components; no discovery of clients or Docker."""

from __future__ import annotations

import json
import os

from aelev_platform_ui_client import Settings, aggregate, observe, run

from api_contract import swagger_url
from health import PREFIX, probe


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
            lambda: probe(
                "public-egress-gateway",
                8080,
                os.environ[PREFIX + "SERVER_NAME"],
                "/run/secrets/gateway/server-ca.pem",
                cfg.timeout,
            ),
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
