#!/usr/bin/env python3
"""Require separate line and branch coverage floors for privileged recovery code."""

from __future__ import annotations

import json
import sys
from pathlib import Path

FLOORS = {
    "recovery.py": (75, 65),
    "recovery_cli.py": (90, 80),
    "recovery_host.py": (80, 60),
}


def validate(report: dict[str, object]) -> list[str]:
    errors = []
    if not report.get("meta", {}).get("branch_coverage"):
        errors.append("branch measurement is required")
    files = report.get("files", {})
    for name, (line_floor, branch_floor) in FLOORS.items():
        matching = [
            value["summary"]
            for path, value in files.items()
            if Path(path).name == name
        ]
        if len(matching) != 1:
            errors.append(f"missing or ambiguous recovery module: {name}")
            continue
        summary = matching[0]
        lines, branches = summary["num_statements"], summary["num_branches"]
        if lines <= 0 or branches <= 0:
            errors.append(f"empty recovery coverage: {name}")
            continue
        line_percent = summary["covered_lines"] / lines * 100
        branch_percent = summary["covered_branches"] / branches * 100
        print(f"{name}: lines={line_percent:.2f}% branches={branch_percent:.2f}%")
        if line_percent < line_floor or branch_percent < branch_floor:
            errors.append(
                f"{name}: coverage below line/branch floor "
                f"{line_floor}%/{branch_floor}%"
            )
    return errors


def main() -> int:
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    errors = validate(report)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Recovery line and branch coverage gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
