"""Owner-local ephemeral probe; executable with python -S and only os/time."""

from __future__ import annotations

import os
import time

ROOT = "/tmp/aelev-translator-health"
HEADER = b"aelev:translator:health:1"
ROLES = {"api"}
MAX_AGE = 30.0
PROGRESS_HEADER = b"aelev:translator:progress:1"
PROGRESS_WINDOWS = {
    "consumer": {"loop": 30.0, "kafka": 120.0},
    "outbox": {"loop": 30.0},
    "worker": {"loop": 30.0, "provider": 300.0},
}


def read(path: str, *, owned: bool = False, limit: int = 4096) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            info.st_mode & 0o170000 != 0o100000
            or owned
            and (
                info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o777 != 0o600
            )
        ):
            raise ValueError("TRANSLATOR_HEALTH_FILE_INVALID")
        raw = os.read(descriptor, limit + 1)
        if len(raw) > limit:
            raise ValueError("TRANSLATOR_HEALTH_FILE_INVALID")
        return raw
    finally:
        os.close(descriptor)


def process_identity(pid: int) -> str:
    fields = read(f"/proc/{pid}/stat").rsplit(b")", 1)[1].split()
    if fields[0] in {b"Z", b"X", b"x"}:
        raise ValueError("TRANSLATOR_HEALTH_PROCESS_DEAD")
    boot = read("/proc/sys/kernel/random/boot_id", limit=128).strip().decode("ascii")
    return f"{boot}:{pid}:{fields[19].decode('ascii')}"


def frame(role: str) -> bytes:
    if role not in ROLES:
        raise ValueError("TRANSLATOR_HEALTH_ROLE_INVALID")
    pid = os.getpid()
    return b"\n".join(
        (
            HEADER,
            role.encode("ascii"),
            str(pid).encode(),
            process_identity(pid).encode("ascii"),
            b"0",
            b"-",
            repr(time.monotonic()).encode(),
            b"",
        )
    )


def progress_frame(role: str, phase: str) -> bytes:
    if phase not in PROGRESS_WINDOWS.get(role, {}):
        raise ValueError("TRANSLATOR_HEALTH_PHASE_INVALID")
    pid = os.getpid()
    return b"\n".join(
        (
            PROGRESS_HEADER,
            role.encode("ascii"),
            str(pid).encode(),
            process_identity(pid).encode("ascii"),
            phase.encode("ascii"),
            repr(time.monotonic()).encode(),
            b"",
        )
    )


def progress_healthy(role: str, raw: bytes) -> bool:
    fields = raw.split(b"\n")
    if (
        len(fields) != 7
        or fields[0] != PROGRESS_HEADER
        or fields[1] != role.encode("ascii")
        or fields[-1] != b""
        or any(b"\0" in part or b"\r" in part for part in fields)
    ):
        return False
    window = PROGRESS_WINDOWS[role].get(fields[4].decode("ascii"))
    pid = int(fields[2])
    return (
        window is not None
        and 0 <= time.monotonic() - float(fields[5]) <= window
        and pid > 0
        and str(pid).encode() == fields[2]
        and fields[3].decode("ascii") == process_identity(pid)
    )


def healthy(*, root: str = ROOT) -> bool:
    try:
        role = os.environ.get("AELEV_TRANSLATOR_HEALTH_ROLE", "")
        if role not in ROLES and role not in PROGRESS_WINDOWS:
            return False
        raw = read(root + "/" + role + ".signal", owned=True)
        if role in PROGRESS_WINDOWS:
            return progress_healthy(role, raw)
        fields = raw.split(b"\n")
        if (
            len(fields) != 8
            or fields[0] != HEADER
            or fields[1] != role.encode("ascii")
            or fields[-1] != b""
            or any(b"\0" in part or b"\r" in part for part in fields)
            or not 0 <= time.monotonic() - float(fields[6]) <= MAX_AGE
        ):
            return False
        pid, child = int(fields[2]), int(fields[4])
        if (
            pid <= 0
            or str(pid).encode() != fields[2]
            or str(child).encode() != fields[4]
            or fields[3].decode("ascii") != process_identity(pid)
        ):
            return False
        return child == 0 and fields[5] == b"-"
    except (OSError, ValueError, TypeError, IndexError, OverflowError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if healthy() else 1)
