"""Seed only an empty or byte-identical owned configuration volume."""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path
from typing import Any

from .files import atomic, reserve


def seed(root: Path, request: dict[str, Any]) -> None:
    marker = request["marker"]
    if marker.get("owner") != "infrastructure-spire" or marker.get("role") not in {
        "server-configuration",
        "agent-bootstrap",
        "operator-request",
        "public-source-secrets",
    }:
        raise ValueError("SPIRE_CONFIGURATION_OWNER_INVALID")
    reserve(root, marker)
    files: dict[str, str] = request["files"]
    allowed = {"plan.json", "bundle.crt", "join-token", "request.json"}
    if marker["role"] == "public-source-secrets":
        allowed = {"source.crt", "source.key", "source-client-ca.crt"}
        if set(files) != allowed:
            raise ValueError("SPIRE_CONFIGURATION_FILES_INVALID")
    if set(files) - allowed or not 1 <= len(files) <= 3:
        raise ValueError("SPIRE_CONFIGURATION_FILES_INVALID")
    for name, encoded in files.items():
        if not re.fullmatch(r"[A-Za-z0-9+/=\s]+", encoded) or len(encoded) > 131072:
            raise ValueError("SPIRE_CONFIGURATION_BYTES_INVALID")
        raw = base64.b64decode(encoded, validate=True)
        path = root / name
        if path.exists():
            if path.is_symlink() or path.read_bytes() != raw:
                raise ValueError("SPIRE_EXISTING_CONFIGURATION_PRESERVED")
        else:
            atomic(path, raw, 0o600)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/var/lib/spire"))
    arguments = parser.parse_args()
    raw = sys.stdin.buffer.read(400001)
    if len(raw) > 400000:
        raise ValueError("SPIRE_CONFIGURATION_TOO_LARGE")
    seed(arguments.root, json.loads(raw))


if __name__ == "__main__":
    main()
