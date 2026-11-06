"""Publish only the OpenTelemetry Collector process and its own publisher."""

from __future__ import annotations

import json
import os

from aelev_platform_ui_client import Settings, aggregate, observe, request, run, tls

PREFIX = "AELEV_INFRASTRUCTURE_OTEL_COLLECTOR_"


def server_state(cfg: Settings) -> str:
    context = tls(cfg.secret_root, "probe-ca.crt", "probe.crt", "probe.key")
    code, body = request(context, os.environ[PREFIX + "UI_HEALTH_URL"], cfg.timeout)
    if code == 200:
        return "ONLINE"
    return "OFFLINE" if code == 503 else "UNKNOWN"


def description(cfg: Settings) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "module_id": cfg.module_id,
            "revision": 2,
            "title": {
                "ru": "Обработка телеметрии",
                "en": "Telemetry processing",
            },
            "service_kind": "INFRASTRUCTURE",
            "sections": [],
            "components": [
                {
                    "id": "server",
                    "title": {
                        "ru": "OpenTelemetry Collector",
                        "en": "OpenTelemetry Collector",
                    },
                },
                {
                    "id": "publisher",
                    "title": {
                        "ru": "Издатель состояния OpenTelemetry Collector",
                        "en": "OpenTelemetry Collector status publisher",
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
    run(
        settings,
        "otel-collector-status",
        description(settings),
        lambda: probe(settings),
    )
