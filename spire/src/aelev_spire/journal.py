"""Durable technical action history, not a replacement for security audit."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def append(root: Path, value: dict[str, Any]) -> None:
    path = root / "actions.jsonl"
    if path.is_symlink() or (path.exists() and path.stat().st_size > 16 * 1024**2):
        raise ValueError("SPIRE_OPERATOR_JOURNAL_UNAVAILABLE")
    previous, sequence = "0" * 64, 0
    with path.open("a+b") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.seek(0)
        for raw in stream:
            record = json.loads(raw)
            actual = record.pop("hash")
            sha = hashlib.sha256(
                json.dumps(record, sort_keys=True).encode()
            ).hexdigest()
            if (
                actual != sha
                or record["previous_hash"] != previous
                or record["sequence"] != sequence + 1
            ):
                raise ValueError("SPIRE_OPERATOR_JOURNAL_CHAIN_INVALID")
            previous, sequence = sha, record["sequence"]
        record = {**value, "sequence": sequence + 1, "previous_hash": previous}
        sha = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        record["hash"] = sha
        stream.seek(0, os.SEEK_END)
        stream.write(json.dumps(record, sort_keys=True).encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(root, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
