"""Standalone python -S heartbeat check; no application imports or network."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path


def healthy() -> bool:
    try:
        path = Path("/run/spire/heartbeat.json")
        if path.is_symlink() or path.stat().st_size > 8192:
            return False
        value = json.loads(path.read_bytes())
        age = (
            datetime.now(UTC) - datetime.fromisoformat(value["updated_at"])
        ).total_seconds()
        return (
            value["owner"] == "infrastructure-spire"
            and value["pid"] == 1
            and 0 <= age < 15
            and os.path.exists("/proc/1")
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if healthy() else 1)
