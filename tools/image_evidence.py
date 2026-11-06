#!/usr/bin/env python3
"""Scan one immutable local image and emit a digest-bound SBOM/evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def run(arguments: list[str]) -> str:
    return subprocess.run(
        arguments, check=True, capture_output=True, text=True, timeout=900
    ).stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--trivy", default="trivy")
    arguments = parser.parse_args()
    image_id = run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", arguments.image]
    ).strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise ValueError("IMAGE_EVIDENCE_IMMUTABLE_ID_REQUIRED")
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    scan, sbom = output / "vulnerabilities.json", output / "sbom.cdx.json"
    common = [
        arguments.trivy,
        "image",
        "--no-progress",
        "--image-src",
        "docker",
        "--scanners",
        "vuln",
        "--exit-code",
        "0",
    ]
    run(
        [
            *common,
            "--format",
            "json",
            "--list-all-pkgs",
            "--output",
            str(scan),
            image_id,
        ]
    )
    run([*common, "--format", "cyclonedx", "--output", str(sbom), image_id])
    report = json.loads(scan.read_text(encoding="utf-8"))
    bill = json.loads(sbom.read_text(encoding="utf-8"))
    if (
        report.get("Metadata", {}).get("ImageID") != image_id
        or not report.get("Results")
        or not any(result.get("Packages") for result in report["Results"])
        or bill.get("bomFormat") != "CycloneDX"
        or not bill.get("components")
    ):
        raise ValueError("IMAGE_EVIDENCE_SCAN_OR_SBOM_INCOMPLETE")
    counts = Counter(
        vulnerability["Severity"]
        for result in report["Results"]
        for vulnerability in result.get("Vulnerabilities", [])
    )
    blocked = counts["HIGH"] + counts["CRITICAL"] > 0
    manifest = {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "image_id": image_id,
        "source_commit": run(["git", "rev-parse", "HEAD"]).strip(),
        "working_tree_dirty": bool(run(["git", "status", "--porcelain"])),
        "scanner": run([arguments.trivy, "--version"]).strip(),
        "severity_counts": dict(sorted(counts.items())),
        "vulnerability_gate": "BLOCKED" if blocked else "PASS",
        "production_admission": "NOT_EVALUATED",
        "files": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (scan, sbom)
        },
    }
    (output / "evidence.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
