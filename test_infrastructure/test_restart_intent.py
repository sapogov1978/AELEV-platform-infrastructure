"""Each lost Docker acknowledgement is reconciled without another dispatch."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery
from test_recovery import enrolled as base_enrolled_fixture  # noqa: F401
from test_recovery import restarts, tick


@pytest.fixture
def fixture(request: pytest.FixtureRequest) -> dict[str, Any]:
    return cast(dict[str, Any], request.getfixturevalue("base_enrolled_fixture"))


def stored(test: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = json.loads((test["root"] / "state.json").read_text())
    return cast(dict[str, Any], value["targets"]["worker"])


@pytest.mark.parametrize("boundary", ["disable", "restart", "restore"])
def test_lost_ack_reconciles_exact_phase_without_spending_again(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    original = recovery.docker
    interrupted = [False]

    def interrupted_docker(state: dict[str, Any], *args: str) -> bytes:
        raw = original(state, *args)
        selected = (
            boundary == "restart"
            and args[0] == "restart"
            or boundary == "disable"
            and args[:3] == ("update", "--restart", "no")
            or boundary == "restore"
            and args[:3] == ("update", "--restart", "on-failure:2")
        )
        if selected and not interrupted[0]:
            interrupted[0] = True
            raise RuntimeError("lost acknowledgement")
        return raw

    monkeypatch.setattr(recovery, "docker", interrupted_docker)
    tick(fixture, 1000)
    assert tick(fixture, 1030)["attempts"] == 1
    pending = stored(fixture)
    assert (
        pending["restart_intent"]["phase"]
        == {
            "disable": "PREPARED",
            "restart": "DISPATCH_INTENT",
            "restore": "POLICY_PENDING",
        }[boundary]
    )
    before = restarts(fixture)
    result = tick(fixture, 1040, "healthy")
    assert restarts(fixture) == before
    assert result["attempts"] == 1 and result["policy_limit"] == 2
    assert not result["blocked"] and "restart_intent" not in result
    assert result["code"] != "RESTART_POLICY_CHANGED"
    assert before == (0 if boundary == "disable" else 1)
    assert fixture["row"]["HostConfig"]["RestartPolicy"] == {
        "Name": "on-failure",
        "MaximumRetryCount": 2,
    }


def test_unknown_restart_never_dispatches_again_and_manual_suspend_wins(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = recovery.docker
    dispatched = [0]

    def unknown(state: dict[str, Any], *args: str) -> bytes:
        if args[0] == "restart":
            dispatched[0] += 1
            assert stored(fixture)["restart_intent"]["phase"] == "DISPATCH_INTENT"
            raise RuntimeError("unknown")
        return original(state, *args)

    monkeypatch.setattr(recovery, "docker", unknown)
    tick(fixture, 1000)
    tick(fixture, 1030)
    assert tick(fixture, 1050)["code"] == "RESTART_RESULT_UNCONFIRMED"
    fixture["row"]["State"]["Running"] = False
    assert tick(fixture, 1100)["attempts"] == 1
    recovery.suspend(fixture["root"], "worker")
    fixture["row"]["State"]["StartedAt"] = "1970-01-01T00:30:00Z"
    result = tick(fixture, 1200)
    assert result["code"] == "OPERATOR_SUSPENDED" and result["blocked"]
    assert dispatched[0] == 1
    assert fixture["row"]["HostConfig"]["RestartPolicy"]["Name"] == "no"


def test_late_new_process_is_observed_not_restarted(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = recovery.docker
    dispatched = [0]

    def unknown(state: dict[str, Any], *args: str) -> bytes:
        if args[0] == "restart":
            dispatched[0] += 1
            raise RuntimeError("unknown")
        return original(state, *args)

    monkeypatch.setattr(recovery, "docker", unknown)
    tick(fixture, 1000)
    tick(fixture, 1030)
    assert tick(fixture, 1040)["blocked"]
    fixture["row"]["State"]["StartedAt"] = "1970-01-01T00:30:00Z"
    result = tick(fixture, 1050, "healthy")
    assert result["attempts"] == 1 and result["policy_limit"] == 2
    assert not result["blocked"] and dispatched[0] == 1


def test_restart_that_already_exited_is_never_started(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = recovery.docker

    def exited(state: dict[str, Any], *args: str) -> bytes:
        raw = original(state, *args)
        if args[0] == "restart":
            fixture["row"]["State"]["Running"] = False
            raise RuntimeError("lost response after process exited")
        return raw

    monkeypatch.setattr(recovery, "docker", exited)
    tick(fixture, 1000)
    tick(fixture, 1030)
    result = tick(fixture, 1050)
    assert result["code"] == "STOPPED_NO_AUTOSTART"
    assert result["attempts"] == 1 and restarts(fixture) == 1
    tick(fixture, 2000)
    assert restarts(fixture) == 1


def test_last_lost_ack_does_not_replenish_exhausted_budget(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = json.loads((fixture["root"] / "state.json").read_text())
    state["targets"]["worker"].update(attempts=2, policy_limit=1)
    recovery.commit(fixture["root"], state)
    fixture["row"]["HostConfig"]["RestartPolicy"]["MaximumRetryCount"] = 1
    original = recovery.docker

    def lost(state: dict[str, Any], *args: str) -> bytes:
        raw = original(state, *args)
        if args[0] == "restart":
            raise RuntimeError("lost last response")
        return raw

    monkeypatch.setattr(recovery, "docker", lost)
    tick(fixture, 1000)
    tick(fixture, 1030)
    result = tick(fixture, 1040)
    assert result["attempts"] == 3 and result["policy_limit"] == 0
    assert result["blocked"] and result["code"] == "CIRCUIT_OPEN"
    assert restarts(fixture) == 1


def test_native_retry_during_pending_policy_uses_same_budget(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = recovery.docker
    interrupted = [False]

    def lost(state: dict[str, Any], *args: str) -> bytes:
        raw = original(state, *args)
        if args[:3] == ("update", "--restart", "on-failure:2") and not interrupted[0]:
            interrupted[0] = True
            raise RuntimeError("lost policy response")
        return raw

    monkeypatch.setattr(recovery, "docker", lost)
    tick(fixture, 1000)
    tick(fixture, 1030)
    fixture["row"]["RestartCount"] = 1
    fixture["row"]["State"]["StartedAt"] = "1970-01-01T00:30:00Z"
    result = tick(fixture, 1040)
    assert result["attempts"] == 2 and result["policy_limit"] == 1
    assert result["native_count"] == 1 and restarts(fixture) == 1


def test_legacy_policy_ambiguity_is_not_reinterpreted(fixture: dict[str, Any]) -> None:
    state = json.loads((fixture["root"] / "state.json").read_text())
    state["targets"]["worker"].update(
        attempts=1, blocked=True, code="RESTART_POLICY_CHANGED"
    )
    recovery.commit(fixture["root"], state)
    fixture["row"]["HostConfig"]["RestartPolicy"] = {
        "Name": "no",
        "MaximumRetryCount": 0,
    }
    result = tick(fixture, 1000)
    assert result["blocked"] and result["code"] == "RESTART_POLICY_CHANGED"
    assert result["attempts"] == 1 and restarts(fixture) == 0


@pytest.mark.parametrize("predecessor_exists", [True, False])
def test_replacement_fences_a_pending_restart_before_adopting_new_process(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch, predecessor_exists: bool
) -> None:
    root = fixture["root"]
    state = json.loads((root / "state.json").read_text())
    state["targets"]["worker"].update(
        attempts=1,
        blocked=True,
        code="OPERATOR_SUSPENDED",
        restart_intent={"id": "d" * 64, "phase": "DISPATCH_INTENT"},
    )
    recovery.commit(root, state)
    before = (root / "state.json").read_bytes()
    fixture["row"].update(Id="b" * 64, Image="sha256:" + "c" * 64)
    original = recovery.docker

    def listing(state: dict[str, Any], *args: str) -> bytes:
        if args[0] == "ps":
            return (("a" * 64 + "\n") if predecessor_exists else "").encode()
        return original(state, *args)

    monkeypatch.setattr(recovery, "docker", listing)
    if predecessor_exists:
        with pytest.raises(ValueError, match="PENDING_RESTART_UNCONFIRMED"):
            recovery.replace(root, "worker", "a" * 64, 0, fixture["row"]["Image"])
        assert (root / "state.json").read_bytes() == before
    else:
        recovery.replace(root, "worker", "a" * 64, 0, fixture["row"]["Image"])
        result = stored(fixture)
        assert "restart_intent" not in result
        assert result["attempts"] == 1 and result["policy_limit"] == 2
    assert restarts(fixture) == 0
