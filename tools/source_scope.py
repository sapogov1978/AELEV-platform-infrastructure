"""Enumerate active source files in the flat infrastructure repository."""

from collections.abc import Iterator
from pathlib import Path

EXCLUDED = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "runtime",
    "dist",
    "graphify-out",
    "obsolete",
    ".codex",
}


def source_files(root: Path, pattern: str) -> Iterator[Path]:
    for path in root.rglob(pattern):
        if path.is_file() and not EXCLUDED.intersection(path.relative_to(root).parts):
            yield path
