"""A delayed Docker die is proof only inside one complete process interval."""

from __future__ import annotations

import copy
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
event = test_automatic_recovery.event
step = test_automatic_recovery.step


@pytest.fixture(params=["tail", "durable"])
def proof(
    automatic: dict[str, Any],
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    if request.param == "durable":
        recovery.atomic(automatic["root"] / "event-observation.json", b"{}")

        def records(
            root: Path, state: dict[str, Any], since: float, boundary: float
        ) -> list[dict[str, Any]]:
            return [
                {
                    "id": item["Actor"]["ID"],
                    "stamp": item["timeNano"],
                    "action": item["Action"],
                    "exit_code": item["Actor"]["Attributes"]["exitCode"],
                }
                for item in automatic["events"]
                if since <= item["timeNano"] / 1e9 <= boundary
            ]

        monkeypatch.setattr(recovery, "durable_events", records)
    return automatic


def delayed_crash(value: dict[str, Any], delay: float = 3.956) -> None:
    crash(value)
    value["events"][-1]["timeNano"] = int((1010 + delay) * 1e9)


@pytest.mark.parametrize("delay", [0, 1.9, 2.1, 3.045, 3.132, 3.956, 20])
def test_delayed_event_proves_same_crash_without_time_tolerance(
    proof: dict[str, Any], delay: float
) -> None:
    delayed_crash(proof, delay)
    assert step(proof, 1041)["code"] == "AUTOMATIC_BACKOFF"
    result = step(proof, 1071)
    assert result["code"] == "AUTOMATIC_RESTARTED"
    assert result["attempts"] == 1 and len(proof["observed"]) == 1


def test_event_arrival_wait_retains_complete_unproven_window(
    proof: dict[str, Any],
) -> None:
    delayed_crash(proof)
    result = step(proof, 1011)
    assert result["code"] == "AUTOMATIC_OBSERVATION_WAIT"
    assert not result["blocked"] and result["attempts"] == 0
    original_since = result["automatic"]["exit"]["since"]
    assert original_since == 1000
    assert step(proof, 1015)["code"] == "AUTOMATIC_BACKOFF"
    state = json.loads((proof["root"] / "state.json").read_bytes())
    assert state["targets"]["worker"]["automatic"]["exit"]["since"] == original_since
    assert step(proof, 1045)["code"] == "AUTOMATIC_RESTARTED"


def test_identical_persisted_and_tail_event_counts_once(proof: dict[str, Any]) -> None:
    delayed_crash(proof)
    proof["events"].append(copy.deepcopy(proof["events"][-1]))
    assert step(proof, 1041)["code"] == "AUTOMATIC_BACKOFF"
    assert step(proof, 1071)["code"] == "AUTOMATIC_RESTARTED"


@pytest.mark.parametrize("damage", ["second-death", "wrong-code", "before-finish"])
def test_inconsistent_death_never_authorizes_start(
    proof: dict[str, Any], damage: str
) -> None:
    delayed_crash(proof)
    if damage == "second-death":
        event(proof, 1015, "die")
    elif damage == "wrong-code":
        proof["events"][-1]["Actor"]["Attributes"]["exitCode"] = "137"
    else:
        proof["events"][-1]["timeNano"] = int(1009e9)
    result = step(proof, 1041)
    assert result["blocked"] and result["code"] == "AUTOMATIC_EXIT_UNCONFIRMED"
    assert result["attempts"] == 0 and not proof["observed"]


@pytest.mark.parametrize(
    "action", ["stop", "kill", "restart", "pause", "destroy", "rename", "update"]
)
def test_delayed_event_cannot_hide_operator_control(
    proof: dict[str, Any], action: str
) -> None:
    delayed_crash(proof)
    event(proof, 1012, action)
    assert step(proof, 1041)["blocked"]
    assert not proof["observed"]


def test_control_after_proof_still_prevents_dispatch(proof: dict[str, Any]) -> None:
    delayed_crash(proof)
    assert step(proof, 1041)["code"] == "AUTOMATIC_BACKOFF"
    event(proof, 1050, "stop")
    assert step(proof, 1071)["blocked"]
    assert not proof["observed"]


def test_new_process_cannot_inherit_old_death(proof: dict[str, Any]) -> None:
    delayed_crash(proof)
    proof["row"]["State"]["StartedAt"] = "1970-01-01T00:16:41+00:00"
    assert step(proof, 1041)["blocked"]
    assert not proof["observed"]


def test_older_advanced_or_blocked_window_is_not_repaired_implicitly(
    proof: dict[str, Any],
) -> None:
    delayed_crash(proof)
    state = json.loads((proof["root"] / "state.json").read_bytes())
    target = state["targets"]["worker"]
    target["automatic"]["exit"] = {
        "since": 1020,
        "finished_at": proof["row"]["State"]["FinishedAt"],
        "exit_code": 1,
        "proven": False,
    }
    recovery.commit(proof["root"], state)
    assert step(proof, 1041)["blocked"]
    assert step(proof, 99999)["blocked"]
    assert not proof["observed"]
