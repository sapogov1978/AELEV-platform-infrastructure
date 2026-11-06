"""Durable read-only event evidence never authorizes an operator stop."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery
import test_automatic_recovery

automatic = test_automatic_recovery.automatic
enrolled = test_automatic_recovery.enrolled
crash = test_automatic_recovery.crash
step = test_automatic_recovery.step


def frame(
    when: float, action: str = "die", identity: str = "a" * 64, code: str = "1"
) -> dict[str, Any]:
    return recovery.event_frame(
        f"{int(when * 1e9)}|container|{action}|{identity}|{code}".encode()
    )


def prepare(
    value: dict[str, Any], monkeypatch: pytest.MonkeyPatch, start: float = 990
) -> dict[str, Any]:
    root = value["root"]
    state = json.loads((root / "state.json").read_bytes())
    recovery.atomic(root / "event-observation.json", b"{}")
    evidence = recovery.event_evidence(root, state["daemon"])
    recovery.event_record(evidence, frame(start, "exec_die"), {"a" * 64}, first=True)
    recovery.event_record(evidence, frame(1010), {"a" * 64}, first=False)
    recovery.event_record(evidence, frame(1040, "exec_die"), {"a" * 64}, first=False)
    recovery.atomic(root / "docker-events.json", json.dumps(evidence).encode())
    original = recovery.docker

    def docker(current: dict[str, Any], *args: str) -> bytes:
        if args[0] == "events":
            # More than 256 global health events have evicted the actual die.
            return b"\n".join(
                f"{int((1039 + i / 1000) * 1e9)}|container|exec_die|{'b' * 64}|-".encode()
                for i in range(256)
            )
        return original(current, *args)

    monkeypatch.setattr(recovery, "docker", docker)
    return evidence


def test_evicted_crash_recovers_without_reset(
    automatic: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(automatic, monkeypatch)
    crash(automatic)
    assert step(automatic, 1041)["code"] == "AUTOMATIC_BACKOFF"
    result = step(automatic, 1071)
    assert result["attempts"] == 1 and not result["blocked"]
    assert len(automatic["observed"]) == 1


def test_delayed_death_in_overlapping_journal_and_tail_is_unique(
    automatic: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = prepare(automatic, monkeypatch)
    evidence["events"][0]["stamp"] = int(1013.956e9)
    recovery.atomic(
        automatic["root"] / "docker-events.json", json.dumps(evidence).encode()
    )
    original = recovery.docker

    def docker(current: dict[str, Any], *args: str) -> bytes:
        if args[0] == "events":
            return (
                f"{int(1013.956e9)}|container|die|{'a' * 64}|1\n"
                f"{int(1039e9)}|container|exec_die|{'b' * 64}|-"
            ).encode()
        return original(current, *args)

    monkeypatch.setattr(recovery, "docker", docker)
    crash(automatic)
    assert step(automatic, 1041)["code"] == "AUTOMATIC_BACKOFF"
    assert step(automatic, 1071)["code"] == "AUTOMATIC_RESTARTED"
    assert len(automatic["observed"]) == 1


@pytest.mark.parametrize(
    "action", ["stop", "kill", "pause", "restart", "destroy", "rename", "update"]
)
def test_persisted_manual_action_prevents_start(
    automatic: dict[str, Any], monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    evidence = prepare(automatic, monkeypatch)
    recovery.event_record(evidence, frame(1009, action), {"a" * 64}, first=False)
    recovery.atomic(
        automatic["root"] / "docker-events.json", json.dumps(evidence).encode()
    )
    crash(automatic)
    result = step(automatic, 1041)
    assert result["blocked"] and result["code"] == "AUTOMATIC_EXIT_UNCONFIRMED"
    assert not automatic["observed"]


def test_reconnect_gap_cannot_supply_old_evidence(
    automatic: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(automatic, monkeypatch, start=1005)
    crash(automatic)
    assert step(automatic, 1041)["blocked"]


def test_reader_lag_waits_without_spending_or_blocking(
    automatic: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = prepare(automatic, monkeypatch)
    evidence["epochs"][-1]["end"] = int(1020e9)
    recovery.atomic(
        automatic["root"] / "docker-events.json", json.dumps(evidence).encode()
    )
    crash(automatic)
    result = step(automatic, 1041)
    assert result["code"] == "AUTOMATIC_OBSERVATION_WAIT"
    assert not result["blocked"] and result["attempts"] == 0
    evidence["epochs"][-1]["end"] = int(1040e9)
    recovery.atomic(
        automatic["root"] / "docker-events.json", json.dumps(evidence).encode()
    )
    assert step(automatic, 1042)["code"] == "AUTOMATIC_BACKOFF"


def test_zero_exit_is_not_an_automatic_failure(
    automatic: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(automatic, monkeypatch)
    crash(automatic, code=0)
    assert step(automatic, 1041)["blocked"]
    assert not automatic["observed"]


def test_only_registered_control_fields_persist(tmp_path: Path) -> None:
    value = recovery.event_evidence(tmp_path, "daemon")
    recovery.event_record(
        value, frame(1000, "health_status: healthy", "foreign"), {"a" * 64}, first=True
    )
    recovery.event_record(value, frame(1001, "die", "foreign"), {"a" * 64}, first=False)
    recovery.event_record(value, frame(1002), {"a" * 64}, first=False)
    recovery.event_record(value, frame(1002), {"a" * 64}, first=True)
    assert len(value["epochs"]) == 1 and len(value["events"]) == 1
    assert "foreign" not in json.dumps(value)
    recovery.event_record(value, frame(1005, "exec_die"), {"a" * 64}, first=True)
    assert len(value["epochs"]) == 2
    recovery.atomic(tmp_path / "docker-events.json", json.dumps(value).encode())
    assert recovery.event_evidence(tmp_path, "daemon") == value
    with pytest.raises(ValueError, match="SCOPE_CHANGED"):
        recovery.event_evidence(tmp_path, "other")


@pytest.mark.parametrize(
    "raw",
    [
        b"1|container|die",
        b"-1|container|die|id|-",
        b"1|container|die|id|secret",
        b"\xff",
    ],
)
def test_invalid_frames_are_closed(raw: bytes) -> None:
    with pytest.raises(ValueError):
        recovery.event_frame(raw)
