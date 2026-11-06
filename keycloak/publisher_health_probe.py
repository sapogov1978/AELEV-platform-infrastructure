"""Read the existing Keycloak loop heartbeat without loading its publisher."""

from __future__ import annotations

import os
import runpy
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

PREFIX = "AELEV_INFRASTRUCTURE_KEYCLOAK_"


def main() -> None:
    """Keep the owner's original journal path and freshness limit."""
    if len(sys.argv) != 2 or sys.argv[1] != PREFIX:
        raise SystemExit(2)
    try:
        interval = int(os.environ[PREFIX + "UI_INTERVAL_SECONDS"])
        timeout = int(os.environ[PREFIX + "UI_TIMEOUT_SECONDS"])
        if min(interval, timeout) <= 0:
            raise ValueError("PUBLISHER_HEALTH_CONFIGURATION_INVALID")
        check = cast(
            Callable[[Path, str, float], None],
            runpy.run_path(str(Path(__file__).with_name("publisher_health.py")))[
                "check"
            ],
        )
        check(
            Path("/var/lib/keycloak-status/outbox.sqlite"),
            os.environ[PREFIX + "UI_MODULE_ID"],
            interval * 2 + timeout * 4,
        )
    except (OSError, sqlite3.Error, ValueError, RuntimeError, KeyError):
        sys.stderr.write("PUBLISHER_LOCAL_HEALTH_UNAVAILABLE\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
