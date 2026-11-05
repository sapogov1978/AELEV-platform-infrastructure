#!/usr/bin/env python3
"""Validate inherited ADR identity and the complete provider diagram surface."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    document = json.loads((root / "docs/audit/extraction.json").read_text())
    errors = []
    for name in document["inherited_adrs"]:
        path = root / name
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest()
            != document["exported_adr_sha256"][name]
        ):
            errors.append(f"Inherited ADR changed or missing: {name}")
    diagrams = sorted((root / "services/infrastructure").rglob("architecture.mmd"))
    if len(diagrams) != 15:
        errors.append(f"Expected 15 owner diagrams, found {len(diagrams)}")
    diagrams += [
        root / "services/infrastructure/infrastructure.mmd",
        root / "services/infrastructure/observability/observability.mmd",
        root / "docs/architecture/repositories.mmd",
    ]
    for path in diagrams:
        text = path.read_text()
        if not re.match(
            r"\s*(flowchart|sequenceDiagram|stateDiagram-v2|classDiagram|erDiagram)\b",
            text,
        ):
            errors.append(f"Invalid Mermaid header: {path.relative_to(root)}")
        for target in re.findall(r'^\s*click\s+\S+\s+"([^\"]+)"', text, re.MULTILINE):
            if "://" not in target and not (path.parent / target).is_file():
                errors.append(f"Broken diagram link: {target}")
    if errors:
        raise SystemExit("\n".join(errors))
    print(
        f"Infrastructure audit passed: {len(document['inherited_adrs'])} inherited ADRs, {len(diagrams)} diagrams"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
