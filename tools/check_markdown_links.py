#!/usr/bin/env python3
"""Fail when a repository-local Markdown link points to a missing path."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import unquote


EXCLUDED = {".git", "node_modules", "runtime", "secrets"}
IMMUTABLE_ARCHIVE = Path("docs/audit/inherited-adrs")
FENCED = re.compile(r"```.*?```", re.DOTALL)
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    root = Path(parser.parse_args().root).resolve()
    missing: list[str] = []
    checked = 0
    for document in sorted(root.rglob("*.md")):
        relative_document = document.relative_to(root)
        if relative_document.parts[0] == "dist":
            continue  # Generated distributions are not repository documentation.
        if (
            relative_document.is_relative_to(IMMUTABLE_ARCHIVE)
            and relative_document != IMMUTABLE_ARCHIVE / "INDEX.md"
        ):
            continue  # Imported bytes retain links relative to their source repository.
        if any(part in EXCLUDED for part in document.parts):
            continue
        text = FENCED.sub("", document.read_text(encoding="utf-8"))
        for raw_target in LINK.findall(text):
            target = raw_target.strip().split()[0].strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            path_text = unquote(target.split("#", 1)[0])
            if not path_text:
                continue
            resolved = (document.parent / path_text).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            checked += 1
            if not resolved.exists():
                missing.append(f"{document.relative_to(root)} -> {path_text}")
    if missing:
        print("Missing local Markdown targets:")
        print("\n".join(missing))
        return 1
    print(f"Markdown link validation passed: {checked} local targets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
