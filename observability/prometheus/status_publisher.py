"""Publish only the Prometheus process and its own publisher."""

from __future__ import annotations

import json
import os

from aelev_platform_ui_client import (
    Settings,
    aggregate,
    observe,
    request,
    run,
    tls,
)

from aelev_observability_tools.operator import (
    Settings as OperatorSettings,
    probe as operator_probe,
)

PREFIX = "AELEV_INFRASTRUCTURE_PROMETHEUS_"


def server_state(cfg: Settings) -> str:
    context = tls(cfg.secret_root, "probe-ca.crt", "probe.crt", "probe.key")
    code, body = request(context, os.environ[PREFIX + "UI_HEALTH_URL"], cfg.timeout)
    if code == 200:
        return "ONLINE"
    return "OFFLINE" if code == 503 else "UNKNOWN"


def operator_state(cfg: Settings) -> str:
    context = tls(cfg.secret_root, "probe-ca.crt", "probe.crt", "probe.key")
    settings = OperatorSettings.environment(PREFIX)
    return (
        "ONLINE"
        if operator_probe(settings, "prometheus-operator", context) == 200
        else "UNKNOWN"
    )


def description(cfg: Settings) -> bytes:
    public_url = OperatorSettings.environment(PREFIX).public_url
    return json.dumps(
        {
            "schema_version": 1,
            "module_id": cfg.module_id,
            "revision": int(os.environ[PREFIX + "UI_REVISION"]),
            "ui_url": public_url,
            "title": {
                "ru": "Хранилище метрик",
                "en": "Metrics storage",
            },
            "service_kind": "INFRASTRUCTURE",
            "sections": [],
            "components": [
                {
                    "id": "server",
                    "title": {"ru": "Prometheus", "en": "Prometheus"},
                    "ui_url": public_url,
                },
                {
                    "id": "operator",
                    "title": {"ru": "Операторский вход", "en": "Operator access"},
                    "ui_url": public_url,
                },
                {
                    "id": "publisher",
                    "title": {
                        "ru": "Издатель состояния Prometheus",
                        "en": "Prometheus status publisher",
                    },
                },
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def probe(cfg: Settings) -> tuple[str, list[dict[str, str]]]:
    components = [
        observe("server", lambda: server_state(cfg)),
        observe("operator", lambda: operator_state(cfg)),
    ]
    return aggregate(components), components


if __name__ == "__main__":
    settings = Settings.environment(PREFIX)
    run(settings, "prometheus-status", description(settings), lambda: probe(settings))
