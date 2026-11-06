"""Bounded mTLS executor probe without application or SQL-driver imports."""

from __future__ import annotations

import signal
import sys

BUDGET_SECONDS = 3.0
MAX_RESPONSE_BYTES = 4096


def check() -> None:
    """Read only the local executor endpoint, without following redirects."""
    import http.client
    import json
    import time

    from aelev_postgresql.configuration import setting, tls_context

    deadline = time.monotonic() + BUDGET_SECONDS
    expected = setting("FENCING_MODE")
    if expected not in {"docker", "kubernetes"}:
        raise ValueError("Invalid executor mechanism")
    context = tls_context()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Executor readiness budget exhausted")
    connection = http.client.HTTPSConnection(
        "localhost", int(setting("FENCING_PORT")), context=context, timeout=remaining
    )
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if (
            response.status != 200
            or len(body) > MAX_RESPONSE_BYTES
            or time.monotonic() > deadline
            or json.loads(body) != {"mechanism": expected}
        ):
            raise ValueError("Local executor readiness unavailable")
    except http.client.HTTPException:
        raise ValueError("Invalid local executor response") from None
    finally:
        connection.close()


def main() -> None:
    """Limit imports, TLS initialization and HTTP to one wall-clock budget."""

    def expired(signum: int, frame: object) -> None:
        raise TimeoutError("Executor readiness budget exhausted")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, BUDGET_SECONDS)
    try:
        check()
    except (OSError, ValueError):
        sys.stderr.write("POSTGRESQL_FENCING_READINESS_UNAVAILABLE\n")
        raise SystemExit(1) from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    main()
