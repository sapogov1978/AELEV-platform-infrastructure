#!/usr/bin/env python3
"""Verify complete, immutable evidence from a clean-room functional run."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMMIT = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")


class EvidenceError(RuntimeError):
    """A stable, secret-free evidence validation failure."""


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise EvidenceError("EVIDENCE_OBJECT_REQUIRED")
    return value


def parse_time(value: object, code: str) -> datetime:
    if not isinstance(value, str):
        raise EvidenceError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceError(code) from error
    if parsed.tzinfo is None:
        raise EvidenceError(code)
    return parsed


def verify(evidence_path: Path, bom_path: Path, root: Path = ROOT) -> dict[str, Any]:
    manifest = load_object(root / "full-local.json")
    release = load_object(root / "release.json")
    evidence = load_object(evidence_path)
    bom_bytes = bom_path.read_bytes()
    bom = json.loads(bom_bytes)
    errors: list[str] = []

    if evidence.get("schema_version") != 1:
        errors.append("EVIDENCE_SCHEMA_UNSUPPORTED")
    if evidence.get("profile") != "full-local":
        errors.append("EVIDENCE_PROFILE_INVALID")
    if evidence.get("release") != release.get("version"):
        errors.append("EVIDENCE_RELEASE_MISMATCH")
    source_commit = evidence.get("source_commit")
    if not isinstance(source_commit, str) or not COMMIT.fullmatch(source_commit):
        errors.append("EVIDENCE_SOURCE_COMMIT_INVALID")
    if source_commit != bom.get("source_commit"):
        errors.append("EVIDENCE_BOM_SOURCE_MISMATCH")
    bom_digest = hashlib.sha256(bom_bytes).hexdigest()
    if evidence.get("bom_sha256") != bom_digest:
        errors.append("EVIDENCE_BOM_DIGEST_MISMATCH")
    if evidence.get("clean_baseline") is not True:
        errors.append("CLEAN_BASELINE_NOT_PROVED")
    if evidence.get("cleanup_preserved") is not True:
        errors.append("CLEANUP_PRESERVATION_NOT_PROVED")
    if evidence.get("result") != "PASS":
        errors.append("RUN_NOT_PASSING")
    try:
        started = parse_time(evidence.get("started_at"), "START_TIME_INVALID")
        finished = parse_time(evidence.get("finished_at"), "FINISH_TIME_INVALID")
        if finished < started:
            errors.append("EVIDENCE_TIME_ORDER_INVALID")
    except EvidenceError as error:
        errors.append(str(error))

    required = manifest.get("acceptance", {}).get("required_functions", [])
    rows = evidence.get("functions")
    if not isinstance(rows, list):
        rows = []
        errors.append("FUNCTION_EVIDENCE_MISSING")
    actual: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            errors.append("FUNCTION_EVIDENCE_INVALID")
            continue
        identity = row["id"]
        if identity in actual:
            errors.append(f"FUNCTION_EVIDENCE_DUPLICATE:{identity}")
            continue
        actual[identity] = row
        if row.get("status") != "PASS":
            errors.append(f"FUNCTION_NOT_PASSING:{identity}")
        artifacts = row.get("evidence")
        if not isinstance(artifacts, list) or not artifacts:
            errors.append(f"FUNCTION_ARTIFACT_MISSING:{identity}")
            continue
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                errors.append(f"FUNCTION_ARTIFACT_INVALID:{identity}")
                continue
            relative = artifact.get("path")
            expected = artifact.get("sha256")
            if (
                not isinstance(relative, str)
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or not isinstance(expected, str)
                or not DIGEST.fullmatch(expected)
            ):
                errors.append(f"FUNCTION_ARTIFACT_INVALID:{identity}")
                continue
            path = evidence_path.parent / relative
            if not path.is_file():
                errors.append(f"FUNCTION_ARTIFACT_MISSING:{identity}")
                continue
            actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual_digest != expected:
                errors.append(f"FUNCTION_ARTIFACT_DIGEST_MISMATCH:{identity}")
    if sorted(actual) != sorted(required):
        errors.append("FUNCTION_SET_MISMATCH")

    return {
        "schema_version": 1,
        "profile": "full-local",
        "release": release.get("version"),
        "status": "PASS" if not errors else "BLOCKED",
        "verified_functions": len(set(actual).intersection(required)),
        "required_functions": len(required),
        "blocking_codes": sorted(set(errors)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--bom", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        result = verify(arguments.evidence, arguments.bom)
    except (EvidenceError, OSError, json.JSONDecodeError) as error:
        result = {
            "schema_version": 1,
            "profile": "full-local",
            "status": "BLOCKED",
            "blocking_codes": [str(error)],
        }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
