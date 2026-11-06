"""Durable, exact-incarnation restart intent; not a leader-election mechanism."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

ROOT = Path("/var/lib/aelev-fencing")
RETURN_DELAYS = (30, 120, 300)
PHASES = {
    "STOP_REQUESTED",
    "STOP_CONFIRMED",
    "START_REQUESTED",
    "COMPLETED",
    "CANCELLED",
}


class IntentStore:
    def __init__(self, node: str) -> None:
        if not re.fullmatch(r"postgresql-[123]", node):
            raise ValueError("Invalid fencing intent node")
        self.node = node
        self.path = ROOT / f"docker-intent-{node}.json"

    @contextmanager
    def locked(self) -> Iterator[None]:
        ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        if ROOT.is_symlink():
            raise ValueError("Fencing state root must not be a symlink")
        descriptor = os.open(
            self.path.with_suffix(".lock"),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(descriptor)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(8193)
        value: object = json.loads(raw)
        if not isinstance(value, dict) or len(raw) > 8192:
            raise ValueError("Invalid fencing intent")
        result = cast(dict[str, Any], value)
        if (
            result.get("version") != 1
            or result.get("node") != self.node
            or result.get("cluster") is None
            or not isinstance(result.get("container_id"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", result["container_id"])
            or not isinstance(result.get("started_at"), str)
            or type(result.get("restart_required")) is not bool
            or type(result.get("start_attempts")) is not int
            or not 0 <= result["start_attempts"] <= 3
            or result.get("phase") not in PHASES
            or not isinstance(result.get("claim"), dict)
            or (
                "next_attempt_at" in result
                and (
                    type(result["next_attempt_at"]) not in {int, float}
                    or not math.isfinite(result["next_attempt_at"])
                    or result["next_attempt_at"] < 0
                )
            )
        ):
            raise ValueError("Invalid fencing intent")
        return result

    def save(self, value: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".docker-intent-", dir=ROOT)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "cancel"))
    parser.add_argument("--node", required=True)
    parser.add_argument("--container", required=True)
    args = parser.parse_args()
    store = IntentStore(args.node)
    with store.locked():
        intent = store.load()
        if intent is None or intent["container_id"] != args.container:
            raise ValueError("Exact existing fencing intent required")
        if args.action == "cancel":
            intent.update(phase="CANCELLED", restart_required=False)
            store.save(intent)
            with (ROOT / "audit.jsonl").open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "phase": "return_cancelled",
                            "node": args.node,
                            "identities": [args.container],
                            "mechanism": "docker",
                            "actor_uid": os.getuid(),
                            "recorded_at": time.time(),
                        }
                    )
                    + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
        sys.stdout.write(json.dumps(intent) + "\n")


if __name__ == "__main__":
    main()
