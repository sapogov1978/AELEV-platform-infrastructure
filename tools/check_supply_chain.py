#!/usr/bin/env python3
"""Check repository-controlled supply-chain references that can be proven offline."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from source_scope import source_files

USE = re.compile(r"^\s*-\s+uses:\s+([^\s#]+)", re.MULTILINE)
SHA = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    errors: list[str] = []
    references = 0
    for path in sorted((root / ".github/workflows").glob("*.yml")):
        for value in USE.findall(path.read_text(encoding="utf-8")):
            references += 1
            if value.startswith("./"):
                continue
            if SHA.fullmatch(value) is None:
                errors.append(
                    f"GitHub Action is not pinned to a full SHA: {path.name}: {value}"
                )
    active_wheels = set()
    for path in source_files(root, "pyproject.toml"):
        active_wheels.update(
            re.findall(
                r"vendor/(aelev_platform_ui_client-[0-9.]+-py3-none-any\.whl)",
                path.read_text(encoding="utf-8"),
            )
        )
    if active_wheels != {"aelev_platform_ui_client-1.1.1-py3-none-any.whl"}:
        errors.append(
            f"unexpected active Platform UI client wheels: {sorted(active_wheels)}"
        )
    if errors:
        print("Supply-chain validation failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print(
        f"Supply-chain references passed: {references} pinned workflow uses; "
        "active Platform UI client is exactly 1.1.1"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
