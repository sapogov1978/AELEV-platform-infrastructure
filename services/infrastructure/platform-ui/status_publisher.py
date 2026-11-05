"""Publish only this shell's own nginx and API, through its authenticated API."""

from __future__ import annotations

import json
from functools import partial

from aelev_platform_ui_client import (
    Settings,
    aggregate,
    observe_many,
    request,
    run,
    tls,
)

PREFIX = "AELEV_INFRASTRUCTURE_PLATFORM_UI_"


def component_state(cfg: Settings, component: str) -> str:
    if component == "api":
        context = tls(cfg.secret_root, "probe-ca.crt", "probe.crt", "probe.key")
        url = "https://platform-ui-api:8444/api/readyz"
        expected = {"status": "ready"}
    else:
        context = tls(cfg.secret_root, "shell-ca.crt")
        url = cfg.ui_url + "/healthz"
        expected = {"status": "ok"}
    code, body = request(context, url, cfg.timeout)
    if code == 200 and json.loads(body) == expected:
        return "ONLINE"
    return "OFFLINE" if code == 503 else "UNKNOWN"


def description(cfg: Settings) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "module_id": cfg.module_id,
            "revision": 2,
            "title": {
                "ru": "Интерфейс платформы",
                "en": "Platform interface",
            },
            "service_kind": "INFRASTRUCTURE",
            "sections": [],
            "components": [
                {
                    "id": "nginx",
                    "title": {"ru": "Оболочка / nginx", "en": "Shell / nginx"},
                },
                {"id": "api", "title": {"ru": "API состояния", "en": "Status API"}},
                {
                    "id": "publisher",
                    "title": {
                        "ru": "Издатель собственного состояния",
                        "en": "Own status publisher",
                    },
                },
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def probe(cfg: Settings) -> tuple[str, list[dict[str, str]]]:
    components = observe_many(
        [(role, partial(component_state, cfg, role)) for role in ("nginx", "api")]
    )
    return aggregate(components), components


if __name__ == "__main__":
    settings = Settings.environment(PREFIX)
    run(settings, "platform-ui-status", description(settings), lambda: probe(settings))
