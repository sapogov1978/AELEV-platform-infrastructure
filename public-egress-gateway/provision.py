"""Offline operator tool; workload clients never receive this filesystem access."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from authorization import MAX_GRANT_BYTES, Grant, identity_filename, strict_json


def _record_digest(record: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def audit(
    directory: Path,
    operation: str,
    identity: str,
    grant_digest: str,
    actor: str,
    event_id: str,
    phase: str,
) -> None:
    """Append a hash-chained owner record before and after an operator mutation."""
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("prepare the audit directory explicitly")
    journal = directory / "operator-audit.jsonl"
    lock = directory / ".operator-audit.lock"
    with lock.open("a+b") as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        sequence, previous = verify_audit(journal)
        record: dict[str, object] = {
            "schema_version": 1,
            "event_id": event_id,
            "sequence": sequence + 1,
            "recorded_at": datetime.now(UTC).isoformat(),
            "operation": operation,
            "phase": phase,
            "actor_hash": hashlib.sha256(actor.encode()).hexdigest(),
            "identity_hash": hashlib.sha256(identity.encode()).hexdigest(),
            "grant_sha256": grant_digest,
            "previous_sha256": previous,
        }
        record["record_sha256"] = _record_digest(record)
        content = (
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
        with journal.open("ab") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(directory)


def verify_audit(journal: Path) -> tuple[int, str]:
    """Verify the complete chain and return its last sequence and digest."""
    if not journal.exists():
        return 0, "0" * 64
    if (
        journal.is_symlink()
        or not journal.is_file()
        or journal.stat().st_size > 64 << 20
    ):
        raise ValueError("invalid operator audit journal")
    previous = "0" * 64
    sequence = 0
    with journal.open("rb") as stream:
        for raw in stream:
            if len(raw) > 16_384 or not raw.endswith(b"\n"):
                raise ValueError("invalid operator audit record")
            value = strict_json(raw.decode())
            if not isinstance(value, dict):
                raise ValueError("invalid operator audit record")
            record = cast(dict[str, object], value).copy()
            claimed = record.pop("record_sha256", None)
            sequence += 1
            if (
                record.get("schema_version") != 1
                or record.get("sequence") != sequence
                or record.get("previous_sha256") != previous
                or claimed != _record_digest(record)
            ):
                raise ValueError("operator audit chain verification failed")
            previous = str(claimed)
    return sequence, previous


@contextmanager
def mutation_lock(audit_directory: Path) -> Iterator[None]:
    """Serialize renewals with apply/revoke, including their audit records."""
    if not audit_directory.is_dir() or audit_directory.is_symlink():
        raise ValueError("prepare the audit directory explicitly")
    with (audit_directory / ".operator-mutation.lock").open("a+b") as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        yield


def apply(source: Path, directory: Path, audit_directory: Path, actor: str) -> None:
    with mutation_lock(audit_directory):
        apply_locked(source, directory, audit_directory, actor)


def apply_locked(
    source: Path, directory: Path, audit_directory: Path, actor: str
) -> None:
    """Apply under an already held operator mutation lock."""
    with source.open("rb") as stream:
        content = stream.read(MAX_GRANT_BYTES + 1)
    if len(content) > MAX_GRANT_BYTES:
        raise ValueError("grant exceeds size limit")
    grant = Grant.parse(strict_json(content.decode()))
    if not directory.is_dir():
        raise ValueError("prepare the grant directory explicitly")
    destination = directory / identity_filename(grant.identity)
    grant_digest = hashlib.sha256(content).hexdigest()
    event_id = str(uuid.uuid4())
    audit(
        audit_directory,
        "apply",
        grant.identity,
        grant_digest,
        actor,
        event_id,
        "INTENT_RECORDED",
    )
    descriptor, temporary = tempfile.mkstemp(prefix=".grant-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, destination)
        sync_directory(directory)
        audit(
            audit_directory,
            "apply",
            grant.identity,
            grant_digest,
            actor,
            event_id,
            "MUTATION_COMMITTED",
        )
    finally:
        Path(temporary).unlink(missing_ok=True)


def revoke(identity: str, directory: Path, audit_directory: Path, actor: str) -> None:
    with mutation_lock(audit_directory):
        revoke_locked(identity, directory, audit_directory, actor)


def revoke_locked(
    identity: str, directory: Path, audit_directory: Path, actor: str
) -> None:
    destination = directory / identity_filename(identity)
    event_id = str(uuid.uuid4())
    audit(
        audit_directory,
        "revoke",
        identity,
        "",
        actor,
        event_id,
        "INTENT_RECORDED",
    )
    destination.unlink(missing_ok=True)
    sync_directory(directory)
    audit(
        audit_directory,
        "revoke",
        identity,
        "",
        actor,
        event_id,
        "MUTATION_COMMITTED",
    )


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=("check", "apply", "revoke", "verify-audit")
    )
    parser.add_argument("input", help="grant JSON file, or exact identity for revoke")
    parser.add_argument("--directory", type=Path, default=Path("grants"))
    parser.add_argument("--audit-directory", type=Path)
    parser.add_argument("--actor")
    args = parser.parse_args()
    if args.operation == "check":
        Grant.parse(strict_json(Path(args.input).read_text()))
    elif args.operation == "verify-audit":
        verify_audit(Path(args.input))
    elif args.operation == "apply":
        if args.audit_directory is None or not args.actor:
            parser.error("apply requires --audit-directory and --actor")
        apply(Path(args.input), args.directory, args.audit_directory, args.actor)
    else:
        if args.audit_directory is None or not args.actor:
            parser.error("revoke requires --audit-directory and --actor")
        revoke(args.input, args.directory, args.audit_directory, args.actor)


if __name__ == "__main__":
    main()
