"""Minimal health rejects stale, foreign, dead and malformed signals."""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path

import pytest

from translator_service import health_heartbeat, local_health


@pytest.fixture
def signal_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AELEV_TRANSLATOR_HEALTH_ROLE", "model")

    def identity(pid: int) -> str:
        return f"boot:{pid}:1"

    monkeypatch.setattr(local_health, "process_identity", identity)
    health_heartbeat.publish("model", 12, root=str(tmp_path))
    return tmp_path


def test_probe_has_only_os_time_imports() -> None:
    tree = ast.parse(Path(local_health.__file__).read_text())
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imports == {"os", "time"}
    assert all(
        node.module == "__future__"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )


def test_atomic_owned_signal(signal_root: Path) -> None:
    assert local_health.healthy(root=str(signal_root))
    assert (signal_root / "model.signal").stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in signal_root.iterdir()) == ["model.signal"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (0, b"foreign"),
        (1, b"api"),
        (2, b"0"),
        (2, b"0001"),
        (3, b"old-incarnation"),
        (4, b"0"),
        (4, b"-1"),
        (4, b"0012"),
        (5, b"old-native-incarnation"),
        (6, b"0"),
        (6, b"nan"),
        (6, b"inf"),
        (6, b"-inf"),
        (6, b"999999999999999"),
        (7, b"trailing"),
    ],
)
def test_invalid_frame(signal_root: Path, field: int, value: bytes) -> None:
    path = signal_root / "model.signal"
    fields = path.read_bytes().split(b"\n")
    fields[field] = value
    path.write_bytes(b"\n".join(fields))
    assert not local_health.healthy(root=str(signal_root))


@pytest.mark.parametrize("kind", ["missing", "symlink", "hardlink", "fifo", "mode"])
def test_unsafe_files_fail_closed(signal_root: Path, kind: str) -> None:
    path = signal_root / "model.signal"
    if kind == "mode":
        path.chmod(0o644)
    elif kind == "hardlink":
        os.link(path, signal_root / "second")
    else:
        raw = path.read_bytes()
        path.unlink()
        if kind == "symlink":
            other = signal_root / "other"
            other.write_bytes(raw)
            path.symlink_to(other)
        elif kind == "fifo":
            os.mkfifo(path)
    assert not local_health.healthy(root=str(signal_root))


def test_process_death_invalidates_signal(
    signal_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def dead(_pid: int) -> str:
        raise ProcessLookupError

    monkeypatch.setattr(local_health, "process_identity", dead)
    assert not local_health.healthy(root=str(signal_root))


def test_api_signal_ignores_model(
    signal_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AELEV_TRANSLATOR_HEALTH_ROLE", "api")
    health_heartbeat.publish("api", None, root=str(signal_root))
    assert local_health.healthy(root=str(signal_root))
    with pytest.raises(ValueError):
        health_heartbeat.publish("api", 12, root=str(signal_root))


@pytest.mark.parametrize("ready", [True, False])
def test_loop_heartbeat_and_cancellation(signal_root: Path, ready: bool) -> None:
    async def exercise() -> None:
        task = asyncio.create_task(
            health_heartbeat.run(
                "model", lambda: ready, lambda: 12, root=str(signal_root)
            )
        )
        await asyncio.sleep(0)
        assert local_health.healthy(root=str(signal_root)) is ready
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not local_health.healthy(root=str(signal_root))

    asyncio.run(exercise())


def test_native_restart_race_does_not_kill_heartbeat(signal_root: Path) -> None:
    async def exercise() -> None:
        task = asyncio.create_task(
            health_heartbeat.run("model", lambda: True, root=str(signal_root))
        )
        await asyncio.sleep(0)
        assert not task.done()
        assert not local_health.healthy(root=str(signal_root))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("role", "phase", "window"),
    [
        ("consumer", "loop", 30),
        ("consumer", "kafka", 120),
        ("outbox", "loop", 30),
        ("worker", "loop", 30),
        ("worker", "model", 300),
    ],
)
def test_progress_expires_without_main_loop_refresh(
    signal_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    phase: str,
    window: int,
) -> None:
    monkeypatch.setenv("AELEV_TRANSLATOR_HEALTH_ROLE", role)
    clock = [1000.0]
    monkeypatch.setattr(local_health.time, "monotonic", lambda: clock[0])
    health_heartbeat.progress(role, phase, root=str(signal_root))
    clock[0] += window
    assert local_health.healthy(root=str(signal_root))
    clock[0] += 0.01
    assert not local_health.healthy(root=str(signal_root))
    health_heartbeat.progress(role, root=str(signal_root))
    assert local_health.healthy(root=str(signal_root))
    clock[0] += 31
    assert not local_health.healthy(root=str(signal_root))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (0, b"foreign"),
        (1, b"outbox"),
        (2, b"0"),
        (2, b"01"),
        (3, b"reused-pid"),
        (4, b"model"),
        (5, b"nan"),
        (5, b"inf"),
        (5, b"999999999999999"),
        (6, b"trailing"),
    ],
)
def test_progress_frame_fails_closed(
    signal_root: Path, monkeypatch: pytest.MonkeyPatch, field: int, value: bytes
) -> None:
    monkeypatch.setenv("AELEV_TRANSLATOR_HEALTH_ROLE", "consumer")
    health_heartbeat.progress("consumer", root=str(signal_root))
    path = signal_root / "consumer.signal"
    fields = path.read_bytes().split(b"\n")
    fields[field] = value
    path.write_bytes(b"\n".join(fields))
    assert not local_health.healthy(root=str(signal_root))


def test_progress_is_opt_in_and_phase_is_not_arbitrary(
    signal_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    health_heartbeat.progress("worker", root=str(signal_root))
    assert not (signal_root / "worker.signal").exists()
    monkeypatch.setenv("AELEV_TRANSLATOR_HEALTH_ROLE", "worker")
    with pytest.raises(ValueError, match="PHASE_INVALID"):
        health_heartbeat.progress("worker", "forever", root=str(signal_root))
