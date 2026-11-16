"""Test-only first-controller SSH barrier, before the native node dispatcher."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

ROOT = Path("/tmp/aelev-return-barrier")
COMMAND = "aelev-postgresql-fencing-node-v1"


def process_identity(pid: int, proc: Path = Path("/proc")) -> tuple[str, int]:
    fields = (proc / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
    return fields[0], int(fields[19])


def arm(root: Path = ROOT) -> None:
    root.mkdir(mode=0o700)
    (root / "armed").write_text("first-return-only\n")


def hold(body: bytes, root: Path = ROOT) -> None:
    """Never dispatch a claimed request, even if somebody sends SIGCONT."""
    if (root / "released").exists():
        return
    try:
        request = json.loads(body)
    except (ValueError, UnicodeError):
        return
    if not isinstance(request, dict) or request.get("action") != "return-once":
        return
    try:
        (root / "armed").rename(root / "claimed")
    except FileNotFoundError:
        if (root / "claimed").exists():
            raise RuntimeError("RETURN_BARRIER_ALREADY_CLAIMED")
        return
    pid = os.getpid()
    _, started = process_identity(pid)
    value = {
        "pid": pid,
        "start_ticks": started,
        "request_sha256": hashlib.sha256(body).hexdigest(),
    }
    with (root / "held.next").open("x") as output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    (root / "held.next").replace(root / "held.json")
    os.kill(pid, signal.SIGSTOP)
    raise RuntimeError("RETURN_BARRIER_RESUMPTION_REFUSED")


def snapshot(root: Path = ROOT, proc: Path = Path("/proc")) -> dict[str, Any]:
    try:
        value = json.loads((root / "held.json").read_bytes())
    except FileNotFoundError:
        return {"held": False}
    if (
        not isinstance(value, dict)
        or set(value) != {"pid", "start_ticks", "request_sha256"}
        or type(value["pid"]) is not int
        or value["pid"] <= 1
        or type(value["start_ticks"]) is not int
        or value["start_ticks"] <= 0
        or not isinstance(value["request_sha256"], str)
        or len(value["request_sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in value["request_sha256"])
    ):
        raise ValueError("RETURN_BARRIER_MARKER_INVALID")
    state, started = process_identity(value["pid"], proc)
    if started != value["start_ticks"]:
        raise ValueError("RETURN_BARRIER_PROCESS_CHANGED")
    return {"held": state == "T", "kernel_state": state, **value}


def discard(root: Path = ROOT) -> None:
    value = snapshot(root)
    if value.get("held") is not True:
        raise ValueError("RETURN_BARRIER_STOP_UNCONFIRMED")
    handle = os.pidfd_open(value["pid"])
    try:
        if snapshot(root) != value:
            raise ValueError("RETURN_BARRIER_PROCESS_CHANGED")
        signal.pidfd_send_signal(handle, signal.SIGKILL)
    finally:
        os.close(handle)
    end = time.monotonic() + 3
    while time.monotonic() < end:
        try:
            state, started = process_identity(value["pid"])
        except FileNotFoundError:
            (root / "released").touch(exist_ok=False)
            return
        if started != value["start_ticks"] or state == "Z":
            (root / "released").touch(exist_ok=False)
            return
        time.sleep(0.02)
    raise RuntimeError("RETURN_BARRIER_DISCARD_UNCONFIRMED")


def main() -> None:
    os.umask(0o077)
    if sys.argv[1:] == ["arm"]:
        arm()
    elif sys.argv[1:] == ["status"]:
        print(json.dumps(snapshot()))
    elif sys.argv[1:] == ["discard"]:
        discard()
        print(json.dumps({"discarded": True}))
    elif not sys.argv[1:]:
        if os.environ.get("SSH_ORIGINAL_COMMAND") != COMMAND:
            raise ValueError("RETURN_BARRIER_COMMAND_REFUSED")
        body = sys.stdin.buffer.read(4097)
        if len(body) > 4096:
            raise ValueError("RETURN_BARRIER_REQUEST_TOO_LARGE")
        hold(body)
        result = subprocess.run(
            [
                sys.executable,
                "/operator/fencing-node.py",
                "--config",
                "/node-inputs/node.json",
            ],
            input=body,
            check=False,
        )
        raise SystemExit(result.returncode)
    else:
        raise ValueError("RETURN_BARRIER_ARGUMENTS_REFUSED")


if __name__ == "__main__":
    main()
