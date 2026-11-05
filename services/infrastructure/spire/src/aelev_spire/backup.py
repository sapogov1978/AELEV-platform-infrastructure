"""Offline encrypted recovery of exact owner state, never a live SQLite copy."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import tarfile
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .files import atomic, read

OWNER = "infrastructure-spire"
MAGIC = b"AELEV-SPIRE-BACKUP-1\n"
ROLES = {
    "server",
    "agent",
    "operator",
    "configuration",
    "bootstrap-output",
    "installation",
    "status",
    "telemetry",
    "observation",
    "observation-configuration",
    "observer-secrets",
    "sender-secrets",
    "status-secrets",
    "status-admin",
    "grafana-secrets",
    "grafana-admin",
}
MAX_BYTES = 2 * 1024**3
MAX_FILES = 100000


def key_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("SPIRE_BACKUP_PRIVATE_KEY_REQUIRED")
    raw = path.read_bytes()
    if len(raw) != 32:
        raise ValueError("SPIRE_BACKUP_256_BIT_KEY_REQUIRED")
    return raw


def roots_at(root: Path, roles: list[str]) -> dict[str, Path]:
    if not roles or len(set(roles)) != len(roles) or set(roles) - ROLES:
        raise ValueError("SPIRE_BACKUP_ROLES_INVALID")
    paths = {role: root / role for role in sorted(roles)}
    for path in paths.values():
        if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_dir():
            raise ValueError("SPIRE_BACKUP_ROOT_INVALID")
    return paths


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024**2):
            digest.update(block)
    return digest.hexdigest()


def check_owners(paths: dict[str, Path]) -> None:
    for role, path in paths.items():
        if role == "bootstrap-output":
            children = [p for p in path.iterdir() if p.is_dir()]
            if not children or any(
                read(p / "owner.json").get("owner") != OWNER for p in children
            ):
                raise ValueError("SPIRE_BACKUP_FOREIGN_STATE_REFUSED")
        elif read(path / "owner.json").get("owner") != OWNER:
            raise ValueError("SPIRE_BACKUP_FOREIGN_STATE_REFUSED")


def create(root: Path, roles: list[str], output: Path, key_path: Path) -> None:
    paths = roots_at(root, roles)
    check_owners(paths)
    key = key_bytes(key_path)
    receipt = output.with_name(output.name + ".receipt.json")
    if (
        output.exists()
        or receipt.exists()
        or any(
            output.resolve().is_relative_to(path.resolve()) for path in paths.values()
        )
    ):
        raise ValueError("SPIRE_BACKUP_OUTPUT_PRESERVED")
    manifest: dict[str, Any] = {
        "owner": OWNER,
        "schema_version": 1,
        "snapshot_id": str(uuid.uuid4()),
        "roles": sorted(roles),
        "files": {},
    }
    # Writers hold these same locks for their complete lifetimes. Taking a
    # lock is not permission to stop, kill or restart an active installation.
    with (
        ExitStack() as stack,
        tempfile.TemporaryDirectory(prefix="spire-backup-") as tmp,
    ):
        for role, path in paths.items():
            name = {
                "installation": "installation.lock",
                "telemetry": ".delivery.lock",
            }.get(role, ".writer.lock")
            lock = stack.enter_context((path / name).open("a+b"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("SPIRE_BACKUP_WRITER_ACTIVE") from None
        plain = Path(tmp).resolve() / "state.tar"
        total = 0
        with tarfile.open(plain, "w") as archive:
            for role, path in paths.items():
                for item in sorted(path.rglob("*")):
                    if item.is_symlink() or not (item.is_dir() or item.is_file()):
                        raise ValueError("SPIRE_BACKUP_SPECIAL_FILE_REFUSED")
                    if item.is_dir() or item.name in {
                        ".writer.lock",
                        "installation.lock",
                        ".delivery.lock",
                    }:
                        continue
                    total += item.stat().st_size
                    if total > MAX_BYTES or len(manifest["files"]) >= MAX_FILES:
                        raise ValueError("SPIRE_BACKUP_CAPACITY_EXCEEDED")
                    name = role + "/" + item.relative_to(path).as_posix()
                    manifest["files"][name] = file_hash(item)
                    archive.add(item, arcname=name, recursive=False)
            metadata = Path(tmp).resolve() / "manifest.json"
            atomic(metadata, manifest)
            archive.add(metadata, arcname="manifest.json")
        nonce = os.urandom(12)
        cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        cipher.authenticate_additional_data(MAGIC)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as target, plain.open("rb") as source:
            target.write(MAGIC + nonce)
            while block := source.read(1024**2):
                target.write(cipher.update(block))
            target.write(cipher.finalize() + cipher.tag)
            target.flush()
            os.fsync(target.fileno())
        proof = {
            "owner": OWNER,
            "schema_version": 1,
            "snapshot_id": manifest["snapshot_id"],
            "roles": sorted(roles),
            "sha256": file_hash(output),
        }
        proof["hmac_sha256"] = hmac.new(
            key, json.dumps(proof, sort_keys=True).encode(), hashlib.sha256
        ).hexdigest()
        atomic(receipt, proof)


def restore(
    root: Path, roles: list[str], source: Path, key_path: Path, trusted_receipt: Path
) -> None:
    paths, key = roots_at(root, roles), key_bytes(key_path)
    if (
        source.is_symlink()
        or not source.is_file()
        or source.stat().st_size > MAX_BYTES + MAX_FILES * 2048
    ):
        raise ValueError("SPIRE_RESTORE_ARCHIVE_INVALID")
    proof = read(trusted_receipt)
    signature = proof.pop("hmac_sha256", "")
    expected = hmac.new(key, json.dumps(proof, sort_keys=True).encode(), hashlib.sha256)
    if (
        not hmac.compare_digest(signature, expected.hexdigest())
        or proof.get("owner") != OWNER
        or proof.get("schema_version") != 1
        or proof.get("roles") != sorted(roles)
        or proof.get("sha256") != file_hash(source)
    ):
        raise ValueError("SPIRE_RESTORE_TRUSTED_RECEIPT_MISMATCH")
    if any(any(path.iterdir()) for path in paths.values()):
        raise ValueError("SPIRE_RESTORE_EMPTY_TARGET_REQUIRED")
    with tempfile.TemporaryDirectory(prefix="spire-restore-") as tmp:
        plain = Path(tmp).resolve() / "state.tar"
        with source.open("rb") as stream, plain.open("wb") as out:
            if stream.read(len(MAGIC)) != MAGIC:
                raise ValueError("SPIRE_RESTORE_FORMAT_INVALID")
            nonce = stream.read(12)
            stream.seek(-16, os.SEEK_END)
            tag, end = stream.read(16), stream.tell() - 16
            stream.seek(len(MAGIC) + 12)
            if end > MAX_BYTES + MAX_FILES * 2048:
                raise ValueError("SPIRE_RESTORE_CAPACITY_EXCEEDED")
            cipher = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            cipher.authenticate_additional_data(MAGIC)
            while stream.tell() < end:
                out.write(cipher.update(stream.read(min(1024**2, end - stream.tell()))))
            out.write(cipher.finalize())  # Authenticate before any target write.
        with tarfile.open(plain, "r:") as archive:
            members = archive.getmembers()
            if len(members) > MAX_FILES + 1 or len({m.name for m in members}) != len(
                members
            ):
                raise ValueError("SPIRE_RESTORE_MANIFEST_INVALID")
            for member in members:
                parts = Path(member.name).parts
                if (
                    not member.isfile()
                    or not parts
                    or Path(member.name).is_absolute()
                    or ".." in parts
                    or member.size > MAX_BYTES
                    or (member.name != "manifest.json" and parts[0] not in paths)
                ):
                    raise ValueError("SPIRE_RESTORE_ARCHIVE_PATH_INVALID")
            metadata = archive.extractfile("manifest.json")
            if (
                metadata is None
                or archive.getmember("manifest.json").size > 16 * 1024**2
            ):
                raise ValueError("SPIRE_RESTORE_MANIFEST_INVALID")
            manifest = json.load(metadata)
            expected_files = {m.name for m in members} - {"manifest.json"}
            if (
                manifest.get("owner") != OWNER
                or manifest.get("snapshot_id") != proof["snapshot_id"]
                or manifest.get("roles") != sorted(roles)
                or set(manifest.get("files", {})) != expected_files
            ):
                raise ValueError("SPIRE_RESTORE_MANIFEST_INVALID")
            for name, digest in manifest["files"].items():
                data = archive.extractfile(name)
                if data is None:
                    raise ValueError("SPIRE_RESTORE_MANIFEST_INVALID")
                checksum = hashlib.sha256()
                while block := data.read(1024**2):
                    checksum.update(block)
                actual = checksum.hexdigest()
                if actual != digest:
                    raise ValueError("SPIRE_RESTORE_MANIFEST_INVALID")
            # Interrupted restore cannot be started as a complete installation.
            for path in paths.values():
                atomic(path / ".restore-incomplete", b"reconciliation required")
            for name in sorted(expected_files):
                data = archive.extractfile(name)
                assert data is not None
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with destination.open("xb") as target:
                    os.chmod(destination, 0o600)
                    while block := data.read(1024**2):
                        target.write(block)
                    target.flush()
                    os.fsync(target.fileno())
            for base in paths.values():
                directories = [p for p in base.rglob("*") if p.is_dir()]
                for directory in [
                    *sorted(directories, key=lambda p: len(p.parts), reverse=True),
                    base,
                ]:
                    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            for path in paths.values():
                (path / ".restore-incomplete").unlink()
                descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "restore"))
    parser.add_argument("--root", type=Path, default=Path("/recovery"))
    parser.add_argument("--role", action="append", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--trusted-receipt", type=Path)
    args = parser.parse_args()
    if args.action == "create":
        create(args.root, args.role, args.archive, args.key)
    else:
        if args.trusted_receipt is None:
            parser.error("restore requires the independently retained latest receipt")
        restore(args.root, args.role, args.archive, args.key, args.trusted_receipt)


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        tarfile.TarError,
        InvalidTag,
    ) as error:
        code = str(error)
        print(
            json.dumps(
                {
                    "code": (
                        code
                        if re.fullmatch(r"SPIRE_[A-Z0-9_]{1,120}", code)
                        else "SPIRE_BACKUP_OR_RESTORE_FAILED"
                    )
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None
