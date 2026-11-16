"""Prove test crash injection cannot dispatch a held or subsequent request."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

import ha_return_barrier as barrier


def test_only_armed_return_is_intercepted(tmp_path: Path) -> None:
    root = tmp_path / "barrier"
    barrier.hold(b'{"action":"return-once"}', root)
    barrier.arm(root)
    for value in (b"invalid", b"[]", b'{"action":"status"}', b'{"action":"fence"}'):
        barrier.hold(value, root)
    assert (root / "armed").exists()
    assert barrier.snapshot(root) == {"held": False}
    with pytest.raises(FileExistsError):
        barrier.arm(root)


def test_later_return_never_passes_claimed_barrier(tmp_path: Path) -> None:
    (tmp_path / "claimed").write_text("already claimed")
    with pytest.raises(RuntimeError, match="RETURN_BARRIER_ALREADY_CLAIMED"):
        barrier.hold(b'{"action":"return-once"}', tmp_path)
    barrier.hold(b'{"action":"status"}', tmp_path)


def test_pid_incarnation_and_kernel_stop_are_checked(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    process = proc / "42"
    process.mkdir(parents=True)
    stat = "42 (test worker with spaces) " + " ".join(["T"] + ["0"] * 18 + ["123"])
    (process / "stat").write_text(stat)
    marker = {"pid": 42, "start_ticks": 123, "request_sha256": "a" * 64}
    (tmp_path / "held.json").write_text(json.dumps(marker))
    assert barrier.snapshot(tmp_path, proc)["held"] is True
    (process / "stat").write_text(stat.replace(") T ", ") R "))
    assert barrier.snapshot(tmp_path, proc)["held"] is False
    marker["start_ticks"] = 124
    (tmp_path / "held.json").write_text(json.dumps(marker))
    with pytest.raises(ValueError, match="RETURN_BARRIER_PROCESS_CHANGED"):
        barrier.snapshot(tmp_path, proc)


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", 1),
        ("pid", True),
        ("start_ticks", 0),
        ("request_sha256", "private payload"),
    ],
)
def test_invalid_marker_refuses_process_control(
    tmp_path: Path, field: str, value: object
) -> None:
    marker: dict[str, object] = {
        "pid": 42,
        "start_ticks": 123,
        "request_sha256": "a" * 64,
    }
    marker[field] = value
    (tmp_path / "held.json").write_text(json.dumps(marker))
    with pytest.raises(ValueError, match="RETURN_BARRIER_MARKER_INVALID"):
        barrier.snapshot(tmp_path)


@pytest.mark.skipif(
    sys.platform != "linux", reason="actual Linux /proc and signals required"
)
@pytest.mark.parametrize("resume", [False, True])
def test_real_stop_then_discard_or_refuse_resume(tmp_path: Path, resume: bool) -> None:
    root = tmp_path / "barrier"
    effect = tmp_path / "native-effect"
    barrier.arm(root)
    code = (
        "import sys;from pathlib import Path;"
        "sys.path.insert(0,sys.argv[1]);import ha_return_barrier as b;"
        'b.hold(b\'{"action":"return-once"}\',Path(sys.argv[2]));'
        "Path(sys.argv[3]).write_text('must never execute')"
    )
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            str(Path(barrier.__file__).parent),
            str(root),
            str(effect),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        end = time.monotonic() + 5
        value: dict[str, object] = {"held": False}
        while time.monotonic() < end:
            value = barrier.snapshot(root)
            if value.get("held") is True:
                break
            time.sleep(0.02)
        assert value["held"] is True and value["kernel_state"] == "T"
        assert not effect.exists()
        with pytest.raises(RuntimeError, match="RETURN_BARRIER_ALREADY_CLAIMED"):
            barrier.hold(b'{"action":"return-once"}', root)
        if resume:
            os.kill(child.pid, signal.SIGCONT)
            _, error = child.communicate(timeout=3)
            assert child.returncode != 0
            assert b"RETURN_BARRIER_RESUMPTION_REFUSED" in error
            assert not (root / "released").exists()
        else:
            barrier.discard(root)
            child.communicate(timeout=3)
            assert child.returncode == -signal.SIGKILL
            assert (root / "released").exists()
            barrier.hold(b'{"action":"return-once"}', root)
            assert (root / "held.json").exists()
        assert not effect.exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=3)
