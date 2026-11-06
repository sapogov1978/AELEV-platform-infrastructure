"""Fast local publisher liveness; executable directly with Python -S."""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path


def check(path: Path, module_id: str, maximum_age: float) -> None:
    """Read only the owner's committed loop heartbeat, never a network endpoint."""
    if not module_id or not 0 < maximum_age <= 3600:
        raise ValueError("PUBLISHER_HEALTH_CONFIGURATION_INVALID")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.05) as db:
        row = db.execute("SELECT module, heartbeat FROM state WHERE id=1").fetchone()
    if row is None or row[0] != module_id:
        raise ValueError("PUBLISHER_HEALTH_OWNER_MISMATCH")
    try:
        age = time.time() - float(row[1])
    except (TypeError, ValueError):
        raise RuntimeError("PUBLISHER_HEARTBEAT_INVALID") from None
    if not 0 <= age <= maximum_age:
        raise RuntimeError("PUBLISHER_LOOP_STALLED")


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("AELEV_"):
        raise SystemExit(2)
    prefix = sys.argv[1] + "UI_"
    try:
        interval = int(os.environ[prefix + "INTERVAL_SECONDS"])
        timeout = int(os.environ[prefix + "TIMEOUT_SECONDS"])
        if min(interval, timeout) <= 0:
            raise ValueError("PUBLISHER_HEALTH_CONFIGURATION_INVALID")
        check(
            Path("/var/lib/aelev-status/outbox.sqlite"),
            os.environ[prefix + "MODULE_ID"],
            interval * 2 + timeout * 40,
        )
    except (OSError, sqlite3.Error, ValueError, RuntimeError, KeyError):
        # Do not expose configuration, paths, SQL or credentials in Docker output.
        sys.stderr.write("PUBLISHER_LOCAL_HEALTH_UNAVAILABLE\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
