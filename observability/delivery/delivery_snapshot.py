"""Verify a stopped sender snapshot on a disposable copy, without any delivery."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from delivery import Journal, digest_of

FILES = ("delivery.sqlite3", "delivery.sqlite3-wal", "delivery.sqlite3-shm")


def fingerprint(path: Path) -> dict[str, Any]:
    """Check every immutable packet plus retry state without emitting payloads."""
    digest = hashlib.sha256()
    count = pending = size = failed = maximum = 0
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        # Large archives must not thrash the default 2 MiB pager during the
        # full index check. Bound this offline-only cache and close it promptly.
        db.execute("PRAGMA cache_size=-65536")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SENDER_SNAPSHOT_INTEGRITY_INVALID")
        for row in db.execute(
            "SELECT sequence,id,kind,headers,body,digest,created_ns,projected,attempts,error "
            "FROM records ORDER BY sequence"
        ):
            (
                sequence,
                identifier,
                kind,
                headers,
                body,
                packet_digest,
                created,
                projected,
                attempts,
                error,
            ) = row
            if digest_of(kind, json.loads(headers), body) != packet_digest:
                raise ValueError("SENDER_SNAPSHOT_PACKET_DIGEST_INVALID")
            metadata = json.dumps(
                [
                    sequence,
                    identifier,
                    kind,
                    headers,
                    packet_digest,
                    created,
                    projected,
                    attempts,
                    error,
                ],
                separators=(",", ":"),
            ).encode()
            digest.update(len(metadata).to_bytes(8) + metadata)
            digest.update(len(body).to_bytes(8) + body)
            count += 1
            pending += int(projected == 0)
            size += len(body) + 1024
            failed += int(bool(error))
            maximum = max(maximum, sequence)
        row = db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='records'"
        ).fetchone()
        highwater = row[0] if row is not None else 0
    return {
        "records": count,
        "pending": pending,
        "bytes": size,
        "failed": failed,
        "maximum_sequence": maximum,
        "sequence_highwater": highwater,
        "sha256": digest.hexdigest(),
    }


def verify(source: Path, *, capacity: int) -> dict[str, Any]:
    """Caller must stop all source writers and mount the snapshot read-only."""
    if capacity <= 0 or source.is_symlink() or not source.is_dir():
        raise ValueError("SENDER_SNAPSHOT_SOURCE_INVALID")
    before_files: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="sender-snapshot-") as temporary:
        checked = Path(temporary)
        for name in FILES:
            path = source / name
            if not path.exists() and not path.is_symlink() and name != FILES[0]:
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("SENDER_SNAPSHOT_FILE_INVALID")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    raise ValueError("SENDER_SNAPSHOT_FILE_INVALID")
                before_files[name] = hashlib.file_digest(stream, "sha256").hexdigest()
                stream.seek(0)
                with (checked / name).open("xb") as destination:
                    shutil.copyfileobj(stream, destination)
                if os.fstat(stream.fileno()).st_mtime_ns != opened.st_mtime_ns:
                    raise ValueError("SENDER_SNAPSHOT_SOURCE_CHANGED")
            (checked / name).chmod(0o600)
        before = fingerprint(checked / FILES[0])
        journal = Journal(checked, capacity)
        after = fingerprint(checked / FILES[0])
        if before != after or any(
            journal.status()[key] != before[key]
            for key in ("records", "pending", "bytes", "failed")
        ):
            raise ValueError("SENDER_SNAPSHOT_OPEN_CHANGED_RECORDS")
        if {path.name for path in source.iterdir() if path.name in FILES} != set(
            before_files
        ):
            raise ValueError("SENDER_SNAPSHOT_SOURCE_CHANGED")
        for name, expected in before_files.items():
            path = source / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("SENDER_SNAPSHOT_SOURCE_CHANGED")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("SENDER_SNAPSHOT_SOURCE_CHANGED")
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise ValueError("SENDER_SNAPSHOT_SOURCE_CHANGED")
    return {
        "code": "SENDER_SNAPSHOT_VERIFIED",
        "sqlite": sqlite3.sqlite_version,
        "snapshot": before,
        "source_modified": False,
        "delivery_started": False,
    }


def main() -> None:
    try:
        if sqlite3.sqlite_version_info != (3, 53, 4):
            raise ValueError("SENDER_SNAPSHOT_SQLITE_VERSION_MISMATCH")
        result = verify(Path("/snapshot"), capacity=1024**4)
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    except (OSError, ValueError, TypeError, sqlite3.Error):
        sys.stdout.write(json.dumps({"code": "SENDER_SNAPSHOT_NOT_VERIFIED"}) + "\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
