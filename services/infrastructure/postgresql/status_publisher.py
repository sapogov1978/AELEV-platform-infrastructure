"""Read native Patroni and etcd health; no SQL, elections or administrative calls."""

from __future__ import annotations

import json
import os
from functools import partial

from aelev_platform_ui_client import (
    Settings,
    aggregate,
    observe_many,
    request,
    run,
    tls,
)

PREFIX = "AELEV_INFRASTRUCTURE_POSTGRESQL_"
COMPONENTS = tuple(
    f"{role}-{node}" for node in range(1, 4) for role in ("postgresql", "etcd")
)


def node_state(cfg: Settings, component: str) -> str:
    role, node = component.rsplit("-", 1)
    etcd = role == "etcd"
    host = os.environ[PREFIX + f"NODE_{node}_" + ("ETCD_HOST" if etcd else "HOST")]
    port = os.environ[
        PREFIX + f"NODE_{node}_" + ("ETCD_CLIENT_PORT" if etcd else "REST_PORT")
    ]
    context = tls(
        cfg.secret_root, role + "-ca.crt", role + "-probe.crt", role + "-probe.key"
    )
    code, body = request(context, f"https://{host}:{port}/health", cfg.timeout)
    payload = json.loads(body)
    if etcd:
        if code == 200 and payload.get("health") in (True, "true"):
            return "ONLINE"
        if code in (500, 503) and payload.get("health") in (False, "false"):
            return "OFFLINE"
    else:
        if code == 200 and payload.get("state") == "running":
            return "ONLINE"
        if (
            code == 503
            and isinstance(payload.get("state"), str)
            and payload["state"] != "running"
        ):
            return "OFFLINE"
    return "UNKNOWN"


def description(cfg: Settings) -> bytes:
    revision = int(os.environ[PREFIX + "UI_DESCRIPTION_REVISION"])
    if revision < 4:
        raise ValueError("UI_DESCRIPTION_REVISION must be at least 4")
    components = [
        {
            "id": name,
            "title": {
                "ru": name.replace("postgresql", "PostgreSQL / Patroni").replace(
                    "etcd", "etcd"
                ),
                "en": name.replace("postgresql", "PostgreSQL / Patroni"),
            },
        }
        for name in COMPONENTS
    ]
    if os.environ[PREFIX + "FENCING_MODE"] != "watchdog":
        components.append(
            {
                "id": "fencer",
                "title": {
                    "ru": "Исполнитель ограждения PostgreSQL",
                    "en": "PostgreSQL fencing executor",
                },
            }
        )
    components.append(
        {
            "id": "writer",
            "title": {
                "ru": "Доступность пишущего узла",
                "en": "Writable primary availability",
            },
        }
    )
    components.append(
        {
            "id": "publisher",
            "title": {
                "ru": "Издатель состояния PostgreSQL",
                "en": "PostgreSQL status publisher",
            },
        }
    )
    return json.dumps(
        {
            "schema_version": 1,
            "module_id": cfg.module_id,
            "revision": revision,
            "title": {
                "ru": "Хранилище структурированных данных",
                "en": "Structured data storage",
            },
            "service_kind": "INFRASTRUCTURE",
            "sections": [],
            "components": components,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def probe(cfg: Settings) -> tuple[str, list[dict[str, str]]]:
    components = observe_many(
        [(name, partial(node_state, cfg, name)) for name in COMPONENTS]
    )
    if os.environ[PREFIX + "FENCING_MODE"] != "watchdog":
        components += observe_many([("fencer", partial(fencer_state, cfg))])
    components += observe_many([("writer", partial(writer_state, cfg))])
    return aggregate(components), components


def primary_state(cfg: Settings, number: int) -> str:
    """Check real primary role AND native lock, not just a live PostgreSQL."""
    host = os.environ[PREFIX + f"NODE_{number}_HOST"]
    port = os.environ[PREFIX + f"NODE_{number}_REST_PORT"]
    context = tls(
        cfg.secret_root,
        "postgresql-ca.crt",
        "postgresql-probe.crt",
        "postgresql-probe.key",
    )
    code, body = request(context, f"https://{host}:{port}/primary", cfg.timeout)
    payload = json.loads(body)
    if (
        code == 200
        and payload.get("role") == "primary"
        and payload.get("state") == "running"
    ):
        return "ONLINE"
    if (
        code == 503
        and isinstance(payload.get("role"), str)
        and isinstance(payload.get("state"), str)
    ):
        return "OFFLINE"
    return "UNKNOWN"


def writer_state(cfg: Settings) -> str:
    """Uncertainty fails closed; a DCS lock alone is not a writable primary."""
    states = observe_many(
        [(str(n), partial(primary_state, cfg, n)) for n in range(1, 4)]
    )
    online = sum(item["state"] == "ONLINE" for item in states)
    if online > 1:
        return "OFFLINE"
    if any(item["state"] == "UNKNOWN" for item in states):
        return "UNKNOWN"
    return "ONLINE" if online == 1 else "OFFLINE"


def fencer_state(cfg: Settings) -> str:
    """Probe every configured executor using a GET-only certificate identity."""
    context = tls(
        cfg.secret_root,
        "postgresql-ca.crt",
        "postgresql-probe.crt",
        "postgresql-probe.key",
    )
    endpoints = {
        os.environ[PREFIX + f"NODE_{number}_FENCING_URL"].rstrip("/")
        for number in range(1, 4)
    }
    for endpoint in endpoints:
        code, body = request(context, endpoint + "/health", cfg.timeout)
        if (
            code != 200
            or json.loads(body).get("mechanism") != os.environ[PREFIX + "FENCING_MODE"]
        ):
            return "UNKNOWN"
    return "ONLINE"


if __name__ == "__main__":
    settings = Settings.environment(PREFIX)
    run(settings, "postgresql-status", description(settings), lambda: probe(settings))
