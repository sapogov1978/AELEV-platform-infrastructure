"""Read the owner's immutable predecessor chain without creating a new budget."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from .files import read


def historical_attempts(root: Path, role: str) -> tuple[int, str]:
    """Return native attempts of unique predecessors, not repeated exhaustion flags."""
    if role not in {"sender", "observer"}:
        raise ValueError("SPIRE_RECOVERY_HISTORY_ROLE_INVALID")
    path = root / (role + "-recovery.json")
    if not path.is_file():
        raise ValueError("SPIRE_RECOVERY_HISTORY_INCOMPLETE")
    attempts = 0
    evidence: list[str] = []
    seen: set[str] = set()
    sources: list[tuple[Path, str]] = []
    while path.exists():
        value = read(path)
        raw = path.read_bytes()
        identity, count = value.get("old_container"), value.get("old_restart_count")
        if (
            value.get("owner") != "infrastructure-spire"
            or value.get("state") != "COMPLETE"
            or value.get("restart") != "no"
            or not isinstance(identity, str)
            or re.fullmatch(r"[a-f0-9]{64}", identity) is None
            or identity in seen
            or type(count) is not int
            or count < 0
            or len(seen) >= 128
            or json.loads(raw) != value
        ):
            raise ValueError("SPIRE_RECOVERY_HISTORY_INVALID")
        seen.add(identity)
        attempts += count
        evidence.append(hashlib.sha256(raw).hexdigest())
        sources.append((path, evidence[-1]))
        predecessor = value.get("predecessor")
        if predecessor is None:
            if count < value.get("exhausted_restart_count", 3):
                raise ValueError("SPIRE_RECOVERY_HISTORY_INCOMPLETE")
            break
        if (
            not isinstance(predecessor, str)
            or re.fullmatch(r"[a-f0-9]{64}", predecessor) is None
        ):
            raise ValueError("SPIRE_RECOVERY_HISTORY_INVALID")
        path = root / (role + "-recovery-" + predecessor + ".json")
        if not path.exists() or read(path).get("old_container") != predecessor:
            raise ValueError("SPIRE_RECOVERY_HISTORY_INCOMPLETE")
    if any(
        path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        for path, digest in sources
    ):
        raise ValueError("SPIRE_RECOVERY_HISTORY_CHANGED")
    return attempts, hashlib.sha256(json.dumps(evidence).encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--role", choices=("sender", "observer"), required=True)
    args = parser.parse_args()
    count, evidence = historical_attempts(args.state, args.role)
    print(
        json.dumps(
            {"prior_attempts": count, "evidence_sha256": evidence}, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
