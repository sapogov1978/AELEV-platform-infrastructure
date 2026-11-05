#!/usr/bin/env python3
"""Run one exact suite from the validated CI inventory without shell expansion."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from ci_inventory import load, validate
from prepare_ci_assets import prepare


def run(command: list[str], directory: Path) -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        str(path) for path in (directory, directory / "src")
    )
    if directory.name == "translator-service":
        environment["AELEV_TRANSLATION_CONTRACT_ROOT"] = str(directory / "contracts")
    subprocess.run(command, cwd=directory, env=environment, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("python", "ui"))
    parser.add_argument("suite")
    parser.add_argument("--root", default=".")
    arguments = parser.parse_args()
    root = Path(arguments.root).resolve()
    inventory = load(root)
    errors = validate(root, inventory)
    if errors:
        raise SystemExit("CI_INVENTORY_INVALID: " + "; ".join(errors))
    directory = root / arguments.suite
    if arguments.kind == "ui":
        if arguments.suite not in inventory["ui"]:
            raise SystemExit("CI_UI_SUITE_NOT_DECLARED")
        run(["npm", "ci", "--ignore-scripts", "--install-links"], directory)
        run(["npm", "audit", "--audit-level=high"], directory)
        run(["npm", "test"], directory)
        run(["npm", "run", "build"], directory)
        return 0
    entries = {
        str(entry["path"]): str(entry["mode"])
        for entry in inventory["python"]
        if isinstance(entry, dict)
    }
    mode = entries.get(arguments.suite)
    if mode is None:
        raise SystemExit("CI_PYTHON_SUITE_NOT_DECLARED")
    if mode == "lock-only":
        run(["poetry", "check", "--lock"], directory)
        return 0
    run(["poetry", "install", "--no-interaction", "--no-ansi"], directory)
    prepare(directory)
    if mode == "pytest":
        run(["poetry", "run", "python", "-m", "pytest"], directory)
    else:
        raise SystemExit(f"CI_PYTHON_MODE_UNSUPPORTED: {mode!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
