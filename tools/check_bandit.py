#!/usr/bin/env python3
"""Run pinned Bandit scope and reject new medium or any high findings."""

from __future__ import annotations

import collections
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from source_scope import source_files

EXCLUDED_PARTS = {"test", "tests", "__pycache__", "runtime", "obsolete"}


def fingerprint(result: dict[str, Any], root: Path) -> str:
    path = Path(result["filename"]).resolve().relative_to(root).as_posix()
    code = re.sub(r"(?m)^\d+\s*", "", result["code"]).strip()
    digest = hashlib.sha256(code.encode()).hexdigest()
    return f"{path}|{result['test_id']}|{digest}"


def validate_report(
    report: dict[str, Any], baseline: dict[str, Any], root: Path
) -> list[str]:
    errors = []
    if baseline.get("schema_version") != 2 or not isinstance(
        baseline.get("known_medium_findings"), dict
    ):
        return ["Bandit fingerprint baseline is invalid"]
    if report.get("errors") or not isinstance(report.get("results"), list):
        return ["Bandit scan is incomplete or contains analyzer errors"]
    if report.get("metrics", {}).get("_totals", {}).get("loc", 0) <= 0:
        return ["Bandit did not analyze source lines"]
    medium = collections.Counter()
    for result in report["results"]:
        severity = result.get("issue_severity")
        if severity == "HIGH":
            errors.append(
                f"HIGH {result.get('test_id')} {result.get('filename')}:"
                f"{result.get('line_number')}"
            )
        elif severity == "MEDIUM":
            medium[fingerprint(result, root)] += 1
        elif severity != "LOW":
            errors.append(f"unknown Bandit severity: {severity!r}")
    for identity, count in medium.items():
        maximum = baseline["known_medium_findings"].get(identity, 0)
        if not isinstance(maximum, int) or count > maximum:
            errors.append(f"new or duplicated medium finding: {identity}")
    return errors


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    baseline_path = root / "docs/audit/bandit-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    expected_version = baseline.get("bandit_version")
    version = subprocess.run(
        ["bandit", "--version"], capture_output=True, text=True, check=True
    ).stdout
    if not isinstance(expected_version, str) or expected_version not in version:
        print(
            f"Bandit version mismatch: expected {expected_version!r}, got {version!r}"
        )
        return 1
    files = sorted(
        path
        for path in source_files(root, "*.py")
        if not EXCLUDED_PARTS.intersection(path.relative_to(root).parts)
        and not path.name.startswith("test_")
        and not path.name.endswith("_sandbox.py")
    )
    with tempfile.TemporaryDirectory(prefix="drp-bandit-") as directory:
        report = Path(directory) / "bandit.json"
        completed = subprocess.run(
            ["bandit", "-f", "json", "-o", str(report), *(str(path) for path in files)],
            check=False,
        )
        if completed.returncode not in {0, 1}:
            raise SystemExit(f"Bandit scan failed: {completed.returncode}")
        document = json.loads(report.read_text(encoding="utf-8"))
    results = document.get("results", [])
    medium = collections.Counter(
        str(result.get("test_id"))
        for result in results
        if result.get("issue_severity") == "MEDIUM"
    )
    errors = validate_report(document, baseline, root)
    if errors:
        print("Bandit regression gate failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    rendered = ", ".join(f"{key}={value}" for key, value in sorted(medium.items()))
    print(f"Bandit regression gate passed for {len(files)} files: {rendered}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
