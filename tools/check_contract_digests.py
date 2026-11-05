#!/usr/bin/env python3
"""Verify service-owned contract checksums and every file pinned by a manifest."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    errors = []
    checked = 0
    for checksum in sorted((root / "services").rglob("*.sha256")):
        if "contracts" not in checksum.parts:
            continue
        artifact = checksum.with_suffix(".json")
        expected = checksum.read_text(encoding="utf-8").strip()
        if not artifact.is_file() or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            errors.append(f"invalid contract checksum: {checksum.relative_to(root)}")
            continue
        checked += 1
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != expected:
            errors.append(f"contract digest mismatch: {artifact.relative_to(root)}")
        if artifact.name != "manifest.json":
            continue
        manifest = json.loads(artifact.read_text(encoding="utf-8"))
        for relative, expected_file in manifest.get("files", {}).items():
            path = (artifact.parent / relative).resolve()
            if not path.is_relative_to(artifact.parent) or not path.is_file():
                errors.append(f"invalid contract manifest file: {relative}")
                continue
            checked += 1
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected_file:
                errors.append(
                    f"contract file digest mismatch: {path.relative_to(root)}"
                )
    if checked == 0:
        errors.append("no service-owned contract checksums found")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Contract digest validation passed: {checked} pinned artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
