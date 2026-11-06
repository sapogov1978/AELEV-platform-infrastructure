"""Bounded private installation state, never a general secret store."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast


def read(path: Path, limit: int = 65536) -> dict[str, Any]:
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError("SPIRE_STATE_FILE_INVALID")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("SPIRE_STATE_OBJECT_REQUIRED")
    return cast(dict[str, Any], value)


def atomic(path: Path, value: bytes | dict[str, Any], mode: int = 0o600) -> None:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("SPIRE_STATE_SYMLINK_FORBIDDEN")
    raw = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, sort_keys=True).encode()
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".spire-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def reserve(root: Path, marker: dict[str, Any]) -> None:
    if (root / ".restore-incomplete").exists():
        raise ValueError("SPIRE_RESTORE_INCOMPLETE")
    if any(
        parent.is_symlink() for parent in (root, *root.parents)
    ) or root.resolve() in {Path("/"), Path.home().resolve(), Path.cwd().resolve()}:
        raise ValueError("SPIRE_UNSAFE_STATE_ROOT")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "owner.json"
    if path.exists():
        if read(path) != marker:
            raise ValueError("SPIRE_STATE_OWNER_MISMATCH")
    else:
        if any(root.iterdir()):
            raise ValueError("SPIRE_UNKNOWN_STATE_PRESERVED")
        atomic(path, marker)


@contextmanager
def writer(root: Path, name: str = ".writer.lock") -> Generator[None]:
    if (root / ".restore-incomplete").exists():
        raise ValueError("SPIRE_RESTORE_INCOMPLETE")
    path = root / name
    if path.is_symlink():
        raise ValueError("SPIRE_STATE_SYMLINK_FORBIDDEN")
    with path.open("a+b") as lock:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("SPIRE_STATE_WRITER_ACTIVE") from None
        yield
