"""Bounded local Patroni readiness; no SQL driver or application startup."""

from __future__ import annotations

import http.client
import json
import time

from aelev_postgresql.configuration import (
    desired_cluster_config,
    includes,
    setting,
    tls_context,
)
from aelev_postgresql.probe import BUDGET_SECONDS

MAX_RESPONSE_BYTES = 65536


def check() -> None:
    """Check this member and its persisted safety policy within one budget."""
    deadline = time.monotonic() + BUDGET_SECONDS
    node = setting("NODE_ID")
    if node not in {"1", "2", "3"}:
        raise ValueError("Invalid node identity")
    expected = desired_cluster_config()
    context = tls_context()
    for path in ("/health", "/config"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Local readiness budget exhausted")
        connection = http.client.HTTPSConnection(
            setting(f"NODE_{node}_HOST"),
            int(setting(f"NODE_{node}_REST_PORT")),
            context=context,
            timeout=remaining,
        )
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if (
                response.status != 200
                or len(body) > MAX_RESPONSE_BYTES
                or time.monotonic() > deadline
            ):
                raise ValueError("Local Patroni readiness unavailable")
            if path == "/config" and not includes(json.loads(body), expected):
                raise ValueError("Persisted cluster safety configuration mismatch")
        except http.client.HTTPException:
            raise ValueError("Invalid local Patroni response") from None
        finally:
            connection.close()


def main() -> None:
    """Keep the legacy direct module command compatible."""
    from aelev_postgresql.probe import main as probe_main

    probe_main()


if __name__ == "__main__":
    main()
