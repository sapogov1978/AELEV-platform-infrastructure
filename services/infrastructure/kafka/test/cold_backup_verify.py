"""Restore a real cold copy only into private tmpfs; compare committed boundaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


def offsets(raw: bytes) -> dict[tuple[str, int], int]:
    if len(raw) > 1024 * 1024:
        raise ValueError("KAFKA_BACKUP_OFFSETS_TOO_LARGE")
    result: dict[tuple[str, int], int] = {}
    for row in raw.decode("ascii").splitlines():
        name, partition, end = row.rsplit(":", 2)
        key = (name, int(partition))
        if (
            re.fullmatch(r"[A-Za-z0-9._-]+", name) is None
            or key in result
            or key[1] < 0
            or int(end) < 0
        ):
            raise ValueError("KAFKA_BACKUP_OFFSETS_INVALID")
        result[key] = int(end)
    if not result:
        raise ValueError("KAFKA_BACKUP_OFFSETS_MISSING")
    return result


def verify(
    root: Path, role: str, expected_sha: str, destination: Path
) -> dict[str, Any]:
    if re.fullmatch(r"[a-f0-9]{64}", expected_sha) is None:
        raise ValueError("KAFKA_BACKUP_DIGEST_REQUIRED")
    path = root / (role + ".tar")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as raw:
        info = os.fstat(raw.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > 1024**3
        ):
            raise ValueError("KAFKA_BACKUP_ARCHIVE_INVALID")
        if hashlib.file_digest(raw, "sha256").hexdigest() != expected_sha:
            raise ValueError("KAFKA_BACKUP_ARCHIVE_CHANGED")
        raw.seek(0)
        with tarfile.open(fileobj=raw, mode="r:") as archive:
            expected: dict[str, dict[str, Any]] = {}
            members = archive.getmembers()
            if not 1 <= len(members) <= 100000:
                raise ValueError("KAFKA_BACKUP_ARCHIVE_CAPACITY")
            total = 0
            for item in members:
                relative = PurePosixPath(item.name)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or str(relative) in expected
                    or not (item.isfile() or item.isdir())
                ):
                    raise ValueError("KAFKA_BACKUP_UNSAFE_MEMBER")
                total += item.size
                if total > 1024**3:
                    raise ValueError("KAFKA_BACKUP_ARCHIVE_CAPACITY")
                entry: dict[str, Any] = {"directory": item.isdir(), "bytes": item.size}
                if item.isfile():
                    stream = archive.extractfile(item)
                    if stream is None:
                        raise ValueError("KAFKA_BACKUP_UNREADABLE_MEMBER")
                    with stream:
                        hash_state = hashlib.sha256()
                        while chunk := stream.read(1024 * 1024):
                            hash_state.update(chunk)
                        entry["sha256"] = hash_state.hexdigest()
                expected[str(relative)] = entry
            archive.extractall(destination, filter="data")
    for name, entry in expected.items():
        restored = destination / name
        if restored.is_symlink() or restored.is_dir() != entry["directory"]:
            raise ValueError("KAFKA_BACKUP_RESTORE_MISMATCH")
        if not entry["directory"]:
            with restored.open("rb") as raw:
                if (
                    restored.stat().st_size != entry["bytes"]
                    or hashlib.file_digest(raw, "sha256").hexdigest() != entry["sha256"]
                ):
                    raise ValueError("KAFKA_BACKUP_RESTORE_MISMATCH")
    digest = hashlib.sha256(
        json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "files": sum(not v["directory"] for v in expected.values()),
        "bytes": total,
        "tree_sha256": digest,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-sha256", required=True)
    parser.add_argument("--secrets-sha256", required=True)
    parser.add_argument("--config-sha256", required=True)
    args = parser.parse_args()
    ends = offsets(sys.stdin.buffer.read(1024 * 1024 + 1))
    with tempfile.TemporaryDirectory(prefix="kafka-cold-restore-") as temporary:
        root = Path(temporary)
        proofs = {}
        for role in ("data", "secrets", "config"):
            destination = root / role
            destination.mkdir()
            proofs[role] = verify(
                Path("/backup"), role, getattr(args, role + "_sha256"), destination
            )
        checkpoint = (
            (root / "data" / "replication-offset-checkpoint").read_text().splitlines()
        )
        if (
            len(checkpoint) < 2
            or checkpoint[0] != "0"
            or int(checkpoint[1]) != len(checkpoint) - 2
        ):
            raise ValueError("KAFKA_BACKUP_CHECKPOINT_INVALID")
        checked, metadata = 0, 0
        for line in checkpoint[2:]:
            name, partition, boundary = line.split()
            if name == "__cluster_metadata":
                metadata += 1
                continue  # KRaft metadata is not an application topic in GetOffsets.
            if ends.get((name, int(partition)), -1) < int(boundary):
                raise ValueError("KAFKA_BACKUP_COMMITTED_BOUNDARY_REGRESSED")
            checked += 1
        if checked == 0:
            raise ValueError("KAFKA_BACKUP_CHECKPOINT_EMPTY")
        meta = root / "data" / "meta.properties"
        print(
            json.dumps(
                {
                    "code": "KAFKA_COLD_BACKUP_CONTENT_RESTORED",
                    "archives": proofs,
                    "checkpoint_partitions_verified": checked,
                    "metadata_checkpoint_excluded": metadata,
                    "meta_properties_sha256": hashlib.sha256(
                        meta.read_bytes()
                    ).hexdigest(),
                    "working_storage_changed": False,
                    "restore_target": "private-tmpfs",
                    "ownership_restored": False,
                }
            )
        )


if __name__ == "__main__":
    main()
