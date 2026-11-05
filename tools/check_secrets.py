#!/usr/bin/env python3
"""Dependency-free fail-closed scan for tracked or newly added secret material."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])"),
    "github-token": re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}"),
    "openai-key": re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
}
SKIP_PARTS = {".git", "node_modules", ".venv", "__pycache__", "graphify-out"}
SECRET_SUFFIXES = {".key", ".p12", ".pfx"}


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    allowlist_path = root / "docs/audit/secret-scan-allowlist.json"
    document = json.loads(allowlist_path.read_text(encoding="utf-8"))
    allowed = document.get("synthetic_private_keys", {})
    if document.get("schema_version") != 1 or not isinstance(allowed, dict):
        print("Secret scan allowlist is invalid")
        return 1
    errors: list[str] = []
    used: set[str] = set()
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    for raw in listed:
        if not raw:
            continue
        relative = Path(raw.decode("utf-8"))
        path = root / relative
        if not path.is_file() or SKIP_PARTS.intersection(relative.parts):
            continue
        name = path.name.lower()
        if name == ".env" or (
            path.suffix.lower() in SECRET_SUFFIXES and "fixture" not in name
        ):
            errors.append(f"secret-shaped filename is forbidden: {relative.as_posix()}")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for kind, pattern in PATTERNS.items():
            if not pattern.search(text):
                continue
            key = relative.as_posix()
            reason = allowed.get(key)
            if kind == "private-key" and isinstance(reason, str) and len(reason) >= 40:
                used.add(key)
            else:
                errors.append(f"{kind} material detected: {key}")
    for key in allowed:
        if key not in used:
            errors.append(f"stale synthetic-key allowlist entry: {key}")
    if errors:
        print("Secret scan failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print(f"Secret scan passed with {len(used)} reviewed synthetic fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
