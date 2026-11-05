"""Standalone liveness; only frozen/builtin os and time on the probe path."""

from __future__ import annotations

import os
import time

HEARTBEAT = "/tmp/aelev-delivery-health.signal"


def process_start(pid: int, proc: str | os.PathLike[str] = "/proc") -> str:
    """Distinguish PID reuse, including after a container restart."""
    with open(os.path.join(proc, str(pid), "stat"), "rb") as source:
        raw = source.read(4097)
    if len(raw) > 4096:
        raise ValueError("process identity too large")
    return raw.rsplit(b")", 1)[1].split()[19].decode("ascii")


def clear(path: str | os.PathLike[str] = HEARTBEAT) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def publish(
    mode: str,
    path: str | os.PathLike[str] = HEARTBEAT,
    proc: str | os.PathLike[str] = "/proc",
) -> None:
    """Called only by the advancing critical-loop supervisor."""
    import tempfile

    pid = os.getpid()
    raw = "\n".join(
        (
            "telemetry-delivery",
            "1",
            mode,
            str(pid),
            process_start(pid, proc),
            str(time.monotonic_ns()),
        )
    ).encode("ascii")
    descriptor, temporary = tempfile.mkstemp(
        prefix=".delivery-health-", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(raw)
            output.flush()
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def healthy(
    path: str | os.PathLike[str] = HEARTBEAT, proc: str | os.PathLike[str] = "/proc"
) -> bool:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if (
                metadata.st_mode & 0o170000 != 0o100000
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o777 != 0o600
            ):
                return False
            raw = source.read(4097)
        if len(raw) > 4096:
            return False
        fields = raw.split(b"\n")
        return (
            len(fields) == 6
            and fields[0] == b"telemetry-delivery"
            and fields[1] == b"1"
            and fields[2] in {b"sender", b"archive"}
            and fields[3] == b"1"
            and fields[4] == process_start(1, proc).encode("ascii")
            and fields[5].isdigit()
            and len(fields[5]) <= 20
            and 0 <= time.monotonic_ns() - int(fields[5]) < 15_000_000_000
        )
    except (OSError, ValueError, TypeError, IndexError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if healthy() else 1)
