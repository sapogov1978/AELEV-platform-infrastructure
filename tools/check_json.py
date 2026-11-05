#!/usr/bin/env python3
"""Parse every versioned JSON document without changing it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


EXCLUDED = {".git", "node_modules", "runtime", "secrets"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    root = Path(parser.parse_args().root).resolve()
    files = sorted(
        path
        for path in root.rglob("*.json")
        if not any(part in EXCLUDED for part in path.parts)
    )
    errors: list[str] = []
    for path in files:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            errors.append(f"{path.relative_to(root)}: {error}")
    if errors:
        print("Invalid JSON:")
        print("\n".join(errors))
        return 1
    print(f"JSON validation passed: {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
