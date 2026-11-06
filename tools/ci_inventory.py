#!/usr/bin/env python3
"""Validate and expose the exhaustive local test inventory used by CI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from source_scope import source_files

MODES = {"pytest", "lock-only"}


def load(root: Path) -> dict[str, object]:
    value = json.loads((root / "tools/ci_suites.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("CI_INVENTORY_SCHEMA_INVALID")
    return value


def validate(root: Path, value: dict[str, object]) -> list[str]:
    errors: list[str] = []
    python = value.get("python")
    ui = value.get("ui")
    if not isinstance(python, list) or not isinstance(ui, list):
        return ["CI inventory must contain python and ui lists"]

    declared_python: set[str] = set()
    for entry in python:
        if not isinstance(entry, dict):
            errors.append("invalid Python suite entry")
            continue
        path = entry.get("path")
        mode = entry.get("mode")
        if not isinstance(path, str) or mode not in MODES:
            errors.append(f"invalid Python suite: {entry!r}")
            continue
        if path in declared_python:
            errors.append(f"duplicate Python suite: {path}")
        declared_python.add(path)
        directory = root / path
        if (
            not (directory / "pyproject.toml").is_file()
            or not (directory / "poetry.lock").is_file()
        ):
            errors.append(f"Python suite lacks pyproject.toml or poetry.lock: {path}")
        if mode == "pytest" and not any(
            candidate.is_file()
            for pattern in ("test_*.py", "tests/test_*.py", "test/test_*.py")
            for candidate in directory.glob(pattern)
        ):
            errors.append(f"pytest suite has no local tests: {path}")
        if "test_owner" in entry:
            owner = entry["test_owner"]
            parents = {parent.as_posix() for parent in Path(path).parents}
            if owner not in parents or not any(
                candidate.get("path") == owner and candidate.get("mode") == "pytest"
                for candidate in python
                if isinstance(candidate, dict)
            ):
                errors.append(f"invalid parent test owner: {path}: {owner}")

    actual_python = {
        path.parent.relative_to(root).as_posix()
        for path in source_files(root, "pyproject.toml")
    }
    for path in sorted(actual_python - declared_python):
        errors.append(f"unclassified Python project: {path}")
    for path in sorted(declared_python - actual_python):
        errors.append(f"missing declared Python project: {path}")

    declared_ui: set[str] = set()
    for entry in ui:
        if not isinstance(entry, str):
            errors.append(f"invalid UI suite: {entry!r}")
            continue
        if entry in declared_ui:
            errors.append(f"duplicate UI suite: {entry}")
        declared_ui.add(entry)
        directory = root / entry
        try:
            package = json.loads(
                (directory / "package.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"UI package is unreadable: {entry}: {error}")
            continue
        if not (directory / "package-lock.json").is_file():
            errors.append(f"UI suite lacks package-lock.json: {entry}")
        scripts = package.get("scripts", {})
        for script in ("test", "build"):
            if not isinstance(scripts, dict) or not isinstance(
                scripts.get(script), str
            ):
                errors.append(f"UI suite lacks {script} script: {entry}")
    actual_ui = {
        path.parent.relative_to(root).as_posix()
        for path in source_files(root, "package-lock.json")
    }
    for path in sorted(actual_ui - declared_ui):
        errors.append(f"unclassified UI package: {path}")
    for path in sorted(declared_ui - actual_ui):
        errors.append(f"missing declared UI package: {path}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", choices=("python", "ui"))
    parser.add_argument("--github-output", action="store_true")
    parser.add_argument("root", nargs="?", default=".")
    arguments = parser.parse_args()
    root = Path(arguments.root).resolve()
    try:
        inventory = load(root)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"CI inventory is unreadable: {error}", file=sys.stderr)
        return 1
    errors = validate(root, inventory)
    if errors:
        print("CI inventory validation failed:", file=sys.stderr)
        print("\n".join(f"- {error}" for error in errors), file=sys.stderr)
        return 1
    if arguments.matrix:
        matrix = inventory[arguments.matrix]
        encoded = json.dumps(matrix, separators=(",", ":"))
        print(f"matrix={encoded}" if arguments.github_output else encoded)
    else:
        print(
            f"CI inventory passed: {len(inventory['python'])} Python projects, "
            f"{len(inventory['ui'])} UI packages"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
