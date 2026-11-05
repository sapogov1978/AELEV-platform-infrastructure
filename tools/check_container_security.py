#!/usr/bin/env python3
"""Require an explicit non-root final image user or a reviewed exact exception."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FROM = re.compile(r"^\s*FROM\s+", re.IGNORECASE)
USER = re.compile(r"^\s*USER\s+([^\s#]+)", re.IGNORECASE)
ROOT_USERS = {"0", "0:0", "root", "root:root"}


def final_user(path: Path) -> str | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    stage = max(
        (index for index, line in enumerate(lines) if FROM.match(line)), default=-1
    )
    users = [
        match.group(1)
        for line in lines[stage + 1 :]
        if (match := USER.match(line))
    ]
    return users[-1] if users else None


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    exceptions_path = root / "docs/audit/container-security-exceptions.json"
    try:
        document = json.loads(exceptions_path.read_text(encoding="utf-8"))
        exceptions = document["dockerfiles"]
    except (OSError, KeyError, json.JSONDecodeError, TypeError) as error:
        print(f"Container security exceptions are unreadable: {error}")
        return 1
    if document.get("schema_version") != 1 or not isinstance(exceptions, dict):
        print("Container security exceptions have an unsupported schema")
        return 1
    dockerfiles = sorted(
        path
        for path in (root / "services").rglob("Dockerfile*")
        if path.is_file() and not path.name.endswith(".dockerignore")
    )
    errors: list[str] = []
    used: set[str] = set()
    explicit = 0
    for path in dockerfiles:
        relative = path.relative_to(root).as_posix()
        user = final_user(path)
        if user is not None and user not in ROOT_USERS:
            explicit += 1
            continue
        reason = exceptions.get(relative)
        if not isinstance(reason, str) or len(reason.strip()) < 40:
            errors.append(
                f"final image lacks non-root USER and reviewed reason: {relative}"
            )
        else:
            used.add(relative)
    for relative, reason in exceptions.items():
        if not isinstance(relative, str) or not isinstance(reason, str):
            errors.append("exception keys and reasons must be strings")
        elif relative not in used:
            errors.append(f"stale or unnecessary container exception: {relative}")
    if errors:
        print("Container security validation failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print(
        f"Container security passed: {len(dockerfiles)} Dockerfiles, "
        f"{explicit} explicit non-root final users, {len(used)} reviewed exceptions"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
