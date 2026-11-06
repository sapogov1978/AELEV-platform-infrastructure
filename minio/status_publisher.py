"""Observe only the eight owned MinIO nodes and their write quorum."""

from __future__ import annotations

import json
import os
from functools import partial
from urllib.parse import urlsplit, urlunsplit

from aelev_platform_ui_client import (
    Settings,
    aggregate,
    observe_many,
    request,
    run,
    tls,
)

PREFIX = "AELEV_INFRASTRUCTURE_MINIO_"
NODES = tuple(f"site-{site}-{node}" for site in ("a", "b") for node in range(1, 5))


def endpoint(node: str) -> str:
    site, number = node.rsplit("-", 1)
    value = os.environ[
        PREFIX + site.upper().replace("-", "_") + "_NODE_" + number + "_ENDPOINT"
    ]
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("owned HTTPS MinIO endpoint required")
    return urlunsplit((parsed.scheme, parsed.netloc, "/minio/health/cluster", "", ""))


def node_state(cfg: Settings, node: str) -> str:
    context = tls(cfg.secret_root, "probe-ca.crt")
    code, _ = request(context, endpoint(node), cfg.timeout)
    # This endpoint returns 503 specifically when write quorum is unavailable.
    return {200: "ONLINE", 503: "OFFLINE"}.get(code, "UNKNOWN")


def description(cfg: Settings) -> bytes:
    components = [
        {
            "id": node,
            "title": {"ru": "MinIO " + node.upper(), "en": "MinIO " + node.upper()},
        }
        for node in NODES
    ]
    components.append(
        {
            "id": "publisher",
            "title": {"ru": "Издатель состояния MinIO", "en": "MinIO status publisher"},
        }
    )
    return json.dumps(
        {
            "schema_version": 1,
            "module_id": cfg.module_id,
            "revision": 2,
            "title": {
                "ru": "Хранилище артефактов",
                "en": "Artifact storage",
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
        [(node, partial(node_state, cfg, node)) for node in NODES]
    )
    return aggregate(components), components


if __name__ == "__main__":
    settings = Settings.environment(PREFIX)
    run(settings, "minio-status", description(settings), lambda: probe(settings))
