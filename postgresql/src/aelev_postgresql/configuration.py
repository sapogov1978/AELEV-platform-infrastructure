"""Canonical cluster safety and TLS configuration, without database imports."""

from __future__ import annotations

import os
import ssl
from pathlib import Path
from typing import Any

PREFIX = "AELEV_INFRASTRUCTURE_POSTGRESQL_"
RUNTIME = Path("/run/aelev")
SECRETS = RUNTIME / "secrets"


def setting(name: str) -> str:
    """Read one required public configuration value."""
    value = os.environ.get(PREFIX + name, "")
    if not value:
        raise ValueError(f"Required configuration: {PREFIX}{name}")
    return value


def safety_config() -> dict[str, Any]:
    """Return fixed safety invariants, not operator-tunable fallback values."""
    return {
        "ttl": 30,
        "loop_wait": 5,
        "retry_timeout": 5,
        "primary_stop_timeout": 5,
        "synchronous_mode": "quorum",
        "synchronous_mode_strict": True,
        "synchronous_node_count": 1,
        "check_timeline": True,
        "failsafe_mode": False,
        "postgresql": {
            "use_pg_rewind": True,
            "use_slots": True,
            "parameters": {
                "synchronous_commit": "on",
                "fsync": "on",
                "full_page_writes": "on",
                "wal_log_hints": "on",
                "max_slot_wal_keep_size": "4GB",
                "password_encryption": "scram-sha-256",
            },
        },
    }


def tls_context() -> ssl.SSLContext:
    """Use verified TLS and the existing dedicated administrative identity."""
    context = ssl.create_default_context(cafile=str(SECRETS / "ca.crt"))
    context.load_cert_chain(SECRETS / "admin.crt", SECRETS / "admin.key")
    return context


def desired_cluster_config() -> dict[str, Any]:
    """Materialize safety rules and capacity from their single public source."""
    config = safety_config()
    connections = int(setting("MAX_CONNECTIONS"))
    if connections < 10:
        raise ValueError("MAX_CONNECTIONS must leave room for cluster operations")
    config["postgresql"]["parameters"].update(
        max_connections=connections, shared_buffers=setting("SHARED_BUFFERS")
    )
    return config


def includes(actual: object, expected: dict[str, Any]) -> bool:
    """Fail closed on missing, malformed or weakened persisted safety fields."""
    return isinstance(actual, dict) and all(
        key in actual
        and (
            includes(actual[key], value)
            if isinstance(value, dict)
            else type(actual[key]) is type(value) and actual[key] == value
        )
        for key, value in expected.items()
    )
