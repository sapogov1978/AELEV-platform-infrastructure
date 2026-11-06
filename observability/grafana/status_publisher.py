"""Publish only the Grafana process and its own publisher."""

from __future__ import annotations

import json
import os

from aelev_platform_ui_client import (
    Settings,
    aggregate,
    https_url,
    observe,
    request,
    run,
    tls,
)

PREFIX = "AELEV_INFRASTRUCTURE_GRAFANA_"


def server_state(cfg: Settings) -> str:
    context = tls(cfg.secret_root, "probe-ca.crt", "probe.crt", "probe.key")
    code, body = request(context, os.environ[PREFIX + "UI_HEALTH_URL"], cfg.timeout)
    if code == 200:
        if json.loads(body).get("database") != "ok":
            return "UNKNOWN"
        return "ONLINE"
    return "OFFLINE" if code == 503 else "UNKNOWN"


def description(cfg: Settings) -> bytes:
    public_url = https_url(
        "https://"
        + os.environ[PREFIX + "PUBLIC_HOST"]
        + ":"
        + os.environ[PREFIX + "PORT"]
    )
    return json.dumps(
        {
            "schema_version": 1,
            "module_id": cfg.module_id,
            "revision": 3,
            "ui_url": public_url,
            "title": {
                "ru": "Панели наблюдения и уведомления",
                "en": "Monitoring dashboards and notifications",
            },
            "service_kind": "INFRASTRUCTURE",
            "sections": [],
            "components": [
                {
                    "id": "server",
                    "title": {"ru": "Grafana", "en": "Grafana"},
                    "ui_url": public_url,
                },
                {
                    "id": "publisher",
                    "title": {
                        "ru": "Издатель состояния Grafana",
                        "en": "Grafana status publisher",
                    },
                },
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def probe(cfg: Settings) -> tuple[str, list[dict[str, str]]]:
    components = [observe("server", lambda: server_state(cfg))]
    return aggregate(components), components


if __name__ == "__main__":
    settings = Settings.environment(PREFIX)
    run(settings, "grafana-status", description(settings), lambda: probe(settings))
