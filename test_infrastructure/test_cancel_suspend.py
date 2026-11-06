"""Cancel an exact hold without replenishing attempts or restarting a process."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery
from test_recovery import enrolled as base_enrolled_fixture  # noqa: F401


@pytest.fixture
def held(request: pytest.FixtureRequest) -> dict[str, Any]:
    data = cast(dict[str, Any], request.getfixturevalue("base_enrolled_fixture"))
    root = data["root"]
    state = json.loads((root / "state.json").read_bytes())
    state["targets"]["worker"]["attempts"] = 2
    recovery.commit(root, state)
    recovery.suspend(root, "worker")
    data["row"]["HostConfig"]["RestartPolicy"] = {"Name": "no", "MaximumRetryCount": 0}
    data["calls"].clear()
    data["proof"] = recovery.suspension_proof(target(data))
    return data


def target(data: dict[str, Any]) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((data["root"] / "state.json").read_bytes())["targets"]["worker"],
    )


def cancel(data: dict[str, Any]) -> None:
    recovery.cancel_suspend(data["root"], "worker", "a" * 64, data["proof"])


@pytest.mark.parametrize("attempts", [0, 1, 2, 3, 7])
def test_budget_and_identity_preserved(held: dict[str, Any], attempts: int) -> None:
    state = json.loads((held["root"] / "state.json").read_bytes())
    state["targets"]["worker"]["attempts"] = attempts
    recovery.commit(held["root"], state)
    held["proof"] = recovery.suspension_proof(target(held))
    before = copy.deepcopy(target(held))
    cancel(held)
    after = target(held)
    assert after["attempts"] == attempts
    assert after["expected"] == before["expected"]
    assert after["guard"] == before["guard"]
    assert after["policy_limit"] == max(0, 3 - attempts)
    assert after["blocked"] is (attempts >= 3)
    assert after["cancel_suspend"]["phase"] == "COMPLETE"
    assert all(call[0] in {"info", "inspect", "update"} for call in held["calls"])
    count = len(held["calls"])
    cancel(held)
    assert not any(call[0] == "update" for call in held["calls"][count:])


@pytest.mark.parametrize(
    "failure",
    [
        "id",
        "image",
        "stopped",
        "unhealthy",
        "stale",
        "restart",
        "replacement",
        "proof",
        "daemon",
    ],
)
def test_unsafe_cancellation_never_changes_policy(
    held: dict[str, Any], failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = json.loads((held["root"] / "state.json").read_bytes())
    if failure in {"id", "image"}:
        held["row"]["Id" if failure == "id" else "Image"] = "b" * 64
    elif failure == "stopped":
        held["row"]["State"]["Running"] = False
    elif failure == "unhealthy":
        held["row"]["State"]["Health"]["Status"] = "unhealthy"
    elif failure == "stale":
        held["clock"][0] += 100
    elif failure == "restart":
        state["targets"]["worker"]["restart_intent"] = {}
    elif failure == "replacement":
        state["targets"]["worker"]["replacement_policy_pending"] = True
    elif failure == "proof":
        held["proof"] = "b" * 64
    elif failure == "daemon":
        state["daemon"] = "different"
    recovery.commit(held["root"], state)
    with pytest.raises(ValueError):
        cancel(held)
    assert not any(call[0] == "update" for call in held["calls"])


@pytest.mark.parametrize("applied", [False, True])
def test_lost_docker_ack_never_repeats_update(
    held: dict[str, Any], monkeypatch: pytest.MonkeyPatch, applied: bool
) -> None:
    original = recovery.docker
    updates = []

    def lost(state: dict[str, Any], *args: str) -> bytes:
        if args[0] == "update":
            updates.append(args)
            assert target(held)["cancel_suspend"]["phase"] == "DISPATCH_INTENT"
            if applied:
                original(state, *args)
            raise RuntimeError("lost acknowledgement")
        return original(state, *args)

    monkeypatch.setattr(recovery, "docker", lost)
    with pytest.raises(RuntimeError):
        cancel(held)
    assert target(held)["blocked"]
    if applied:
        cancel(held)
        assert not target(held)["blocked"]
    else:
        with pytest.raises(ValueError, match="RESULT_UNCONFIRMED"):
            cancel(held)
    assert len(updates) == 1


def test_new_hold_rejects_old_cancellation(held: dict[str, Any]) -> None:
    cancel(held)
    recovery.suspend(held["root"], "worker")
    with pytest.raises(ValueError, match="SUSPENSION_CHANGED"):
        cancel(held)
    assert target(held)["blocked"]


def test_manual_stop_after_completion_is_not_undone(held: dict[str, Any]) -> None:
    cancel(held)
    held["row"]["State"]["Running"] = False
    before = len(held["calls"])
    with pytest.raises(ValueError):
        cancel(held)
    assert all(call[0] in {"info", "inspect"} for call in held["calls"][before:])


def test_stop_after_prepared_commit_prevents_policy_change(
    held: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = recovery.commit

    def stopped(root: Path, state: dict[str, Any]) -> None:
        original(root, state)
        held["row"]["State"]["Running"] = False

    monkeypatch.setattr(recovery, "commit", stopped)
    with pytest.raises(ValueError):
        cancel(held)
    assert not any(call[0] == "update" for call in held["calls"])


def test_prepared_commit_loss_resumes_once(
    held: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = recovery.commit

    def lost(root: Path, state: dict[str, Any]) -> None:
        original(root, state)
        raise RuntimeError("lost prepared commit acknowledgement")

    monkeypatch.setattr(recovery, "commit", lost)
    with pytest.raises(RuntimeError):
        cancel(held)
    assert target(held)["cancel_suspend"]["phase"] == "PREPARED"
    monkeypatch.setattr(recovery, "commit", original)
    cancel(held)
    assert sum(call[0] == "update" for call in held["calls"]) == 1


def test_native_exit_spends_remaining_budget(held: dict[str, Any]) -> None:
    held["row"]["RestartCount"] = 1
    cancel(held)
    assert target(held)["attempts"] == 3
    assert target(held)["blocked"]
    assert held["row"]["HostConfig"]["RestartPolicy"]["Name"] == "no"


@pytest.mark.parametrize("allowed", [False, True])
def test_owner_guard_must_allow(
    held: dict[str, Any], monkeypatch: pytest.MonkeyPatch, allowed: bool
) -> None:
    import hashlib

    path = held["root"] / "guard.py"
    path.write_text("# exact owner guard\n")
    state = json.loads((held["root"] / "state.json").read_bytes())
    state["targets"]["worker"]["guard"] = {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    recovery.commit(held["root"], state)
    held["proof"] = recovery.suspension_proof(target(held))
    monkeypatch.setattr(
        recovery, "execute", lambda *a, **k: json.dumps({"allowed": allowed}).encode()
    )
    if allowed:
        cancel(held)
        assert not target(held)["blocked"]
    else:
        with pytest.raises(ValueError, match="GUARD_BLOCKED"):
            cancel(held)
        assert not any(call[0] == "update" for call in held["calls"])
