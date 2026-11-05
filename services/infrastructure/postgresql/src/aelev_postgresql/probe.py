"""Arm a wall-clock limit before importing TLS and the readiness implementation."""

from __future__ import annotations

import signal
import sys

BUDGET_SECONDS = 3.0


def main() -> None:
    """Bound initialization and network checks, without application imports."""

    def expired(signum: int, frame: object) -> None:
        raise TimeoutError("Local readiness budget exhausted")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, BUDGET_SECONDS)
    try:
        from aelev_postgresql.health import check

        check()
    except (OSError, ValueError):
        sys.stderr.write("POSTGRESQL_LOCAL_READINESS_UNAVAILABLE\n")
        raise SystemExit(1) from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    main()
