"""Terminal diagnostics contain locations, never secrets or unrelated threads."""

import json
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from aelev_spire.observation import pending_files
from aelev_spire.observation_diagnostics import PHASES, Diagnostics, locations


def test_supervisor_persists_the_real_stalled_worker_phase_before_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aelev_spire import observation

    clock = [100.0]
    entered, release, finished = (threading.Event() for _ in range(3))
    handlers: list[Any] = []

    class Outgoing:
        def __init__(
            self,
            _root: Path,
            _endpoint: str,
            _secrets: Path,
            phase: Any,
            _completed: Any,
        ):
            self.phase = phase

        def capture(self, *_args: Any) -> None:
            return

        def flush_one(self) -> None:
            self.phase("HTTP_BODY")
            entered.set()
            release.wait(5)
            finished.set()

    class Server:
        def __init__(self, *_args: Any) -> None:
            return

        def __enter__(self) -> "Server":
            return self

        def __exit__(self, *_args: Any) -> None:
            handlers[-1](15, None)
            release.set()
            assert finished.wait(2)

        def handle_request(self) -> None:
            assert entered.wait(3)
            clock[0] = 120.0

    monkeypatch.setattr(observation, "Outgoing", Outgoing)
    monkeypatch.setattr(observation, "Server", Server)
    monkeypatch.setattr(observation.time, "monotonic", lambda: clock[0])

    def signal(_number: int, handler: Any) -> None:
        handlers.append(handler)

    monkeypatch.setattr(observation.signal, "signal", signal)
    root = tmp_path / "queue"
    root.mkdir()
    with pytest.raises(ValueError, match="SPIRE_OBSERVATION_WORKER_STALLED"):
        observation.serve(
            root, tmp_path / "source", tmp_path / "secrets", "https://fixture"
        )
    reports = list((root / "diagnostics").glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_bytes())
    assert report["code"] == "SPIRE_OBSERVATION_WORKER_STALLED"
    assert report["phase"] == "HTTP_BODY"
    assert report["progress_age_seconds"] == 20
    assert any(
        frame["function"] == "flush_one" for frame in report["threads"]["worker"]
    )


def test_report_preserves_pending_packets_and_excludes_private_data(
    tmp_path: Path,
) -> None:
    secret = "never-export-credentials-or-payload-9371"
    pending = tmp_path / "packet.json"
    pending.write_text(secret)
    release = threading.Event()
    entered = threading.Event()

    def blocked() -> None:
        private_payload = secret
        entered.set()
        release.wait(5)
        assert private_payload == secret

    worker = threading.Thread(target=blocked)
    worker.start()
    try:
        assert entered.wait(2)
        diagnostics = Diagnostics(tmp_path)
        diagnostics.mark("HTTP_BODY")
        assert diagnostics.record(
            "SPIRE_OBSERVATION_WORKER_STALLED",
            worker,
            progress_age=16,
            supervisor_cycle=0.5,
            error=OSError(secret),
        )
        path = tmp_path / "diagnostics" / (diagnostics.run_id + ".json")
        raw = path.read_bytes()
        assert secret.encode() not in raw and len(raw) <= 8192
        report = json.loads(raw)
        assert report["phase"] == "HTTP_BODY"
        assert report["progress_age_seconds"] == 16
        assert report["exception_type"] == "OSError"
        assert set(report["threads"]) == {"supervisor", "worker"}
        assert report["threads"]["worker"]
        for stack in report["threads"].values():
            assert len(stack) <= 16
            for frame in stack:
                assert set(frame) == {"file", "function", "line"}
                assert "/" not in frame["file"] and "\\" not in frame["file"]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert pending_files(tmp_path) == [pending]
        assert pending.read_text() == secret
    finally:
        release.set()
        worker.join(3)


def test_reports_survive_subsequent_runs_without_overwriting_history(
    tmp_path: Path,
) -> None:
    for _ in range(2):
        diagnostics = Diagnostics(tmp_path)
        diagnostics.mark("JOURNAL_ACK")
        assert diagnostics.record(
            "SPIRE_OBSERVATION_WORKER_STOPPED",
            threading.Thread(),
            progress_age=0,
            supervisor_cycle=0,
        )
    assert len(list((tmp_path / "diagnostics").glob("*.json"))) == 2
    assert not pending_files(tmp_path)
    assert locations(None) == []


def test_all_phase_markers_are_bounded_and_cannot_renew_health(tmp_path: Path) -> None:
    diagnostics = Diagnostics(tmp_path)
    for phase in PHASES:
        diagnostics.mark(phase)
        assert diagnostics.observation[0] == phase
    with pytest.raises(ValueError, match="PHASE_INVALID"):
        diagnostics.mark("untrusted site or credentials")
    assert not (tmp_path / "heartbeat.json").exists()


@pytest.mark.parametrize("cause", ["symlink", "code", "nonfinite"])
def test_diagnostic_failure_cannot_escape_or_overwrite_original_reason(
    tmp_path: Path,
    cause: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    if cause == "symlink":
        (tmp_path / "diagnostics").symlink_to(outside, target_is_directory=True)
    diagnostics = Diagnostics(tmp_path)
    assert not diagnostics.record(
        (
            "secret-remote-error"
            if cause == "code"
            else "SPIRE_OBSERVATION_WORKER_STOPPED"
        ),
        threading.Thread(),
        progress_age=float("inf") if cause == "nonfinite" else 1,
        supervisor_cycle=0,
    )
    assert not list(outside.iterdir())
    captured = capsys.readouterr()
    assert not captured.out and "secret-remote-error" not in captured.err
    assert (
        json.loads(captured.err)["code"] == "SPIRE_OBSERVATION_DIAGNOSTIC_WRITE_FAILED"
    )
