"""Reviewed legacy handoffs retain counters and never issue start/restart."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import recovery
import test_recovery

enrolled = test_recovery.enrolled


def receipt(case: dict[str, Any], count: int = 3) -> tuple[Path, str]:
    root, row = case["root"], case["row"]
    state = json.loads((root / "state.json").read_bytes())
    state["targets"].pop("worker")
    state["targets"]["other"] = {
        "expected": {"id": "b" * 64, "project": "other", "service": "other"},
        "attempts": 71,
    }
    recovery.commit(root, state)
    row["Name"] = "/worker"
    row["Config"]["Labels"]["com.docker.compose.config-hash"] = "f" * 64
    row["Config"]["Healthcheck"]["Test"] = ["CMD", "true"]
    path = root / "receipt.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "daemon_id": state["daemon"],
                "name": "worker",
                "expected": recovery.fingerprint(row),
                "prior_attempts": count,
                "evidence_sha256": "c" * 64,
            }
        )
    )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("stopped", [False, True])
def test_import_preserves_history_without_start(
    enrolled: dict[str, Any], stopped: bool
) -> None:
    path, proof = receipt(enrolled)
    row, root = enrolled["row"], enrolled["root"]
    row["RestartCount"] = 2
    row["HostConfig"]["RestartPolicy"] = recovery.NO_RESTART
    enrolled["calls"].clear()
    if stopped:
        row["State"].update(Running=False, Status="exited", ExitCode=1)
    recovery.import_history(root, path, proof)
    state = json.loads((root / "state.json").read_bytes())
    assert state["targets"]["worker"]["attempts"] == 5
    assert state["targets"]["worker"]["blocked"] is True
    before = (root / "state.json").read_bytes()
    recovery.import_history(root, path, proof)
    assert (root / "state.json").read_bytes() == before
    assert state["targets"]["other"]["attempts"] == 71
    assert not any(
        call[0] in {"update", "start", "restart"} for call in enrolled["calls"]
    )


@pytest.mark.parametrize(
    "change", ["digest", "daemon", "counter", "identity", "existing", "unhealthy"]
)
def test_invalid_import_never_changes_state(
    enrolled: dict[str, Any], change: str
) -> None:
    path, proof = receipt(enrolled)
    value = json.loads(path.read_bytes())
    if change == "daemon":
        value["daemon_id"] = "another"
    elif change == "counter":
        value["prior_attempts"] = -1
    elif change == "identity":
        value["expected"]["id"] = "b" * 64
    elif change == "existing":
        state = json.loads((enrolled["root"] / "state.json").read_bytes())
        state["targets"]["worker"] = {"expected": value["expected"], "attempts": 194}
        recovery.commit(enrolled["root"], state)
    elif change == "unhealthy":
        enrolled["row"]["State"]["Health"]["Status"] = "unhealthy"
    path.write_text(json.dumps(value))
    proof = (
        "0" * 64
        if change == "digest"
        else hashlib.sha256(path.read_bytes()).hexdigest()
    )
    before = (enrolled["root"] / "state.json").read_bytes()
    with pytest.raises(ValueError):
        recovery.import_history(enrolled["root"], path, proof)
    assert (enrolled["root"] / "state.json").read_bytes() == before


def blocked(case: dict[str, Any]) -> tuple[str, str]:
    state = json.loads((case["root"] / "state.json").read_bytes())
    target = state["targets"]["worker"]
    target.update(
        attempts=194,
        policy_limit=0,
        blocked=True,
        code="CIRCUIT_OPEN",
        replacement_from={"id": "c" * 64, "native_count": 0},
        replacement_block={"code": "TARGET_REPLACED"},
    )
    case["row"]["HostConfig"]["RestartPolicy"] = recovery.NO_RESTART
    recovery.commit(case["root"], state)
    return target["expected"]["id"], recovery.replacement_release_proof(target)


def test_release_then_enable_preserves_194_and_later_hold(
    enrolled: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    identity, proof = blocked(enrolled)
    original = recovery.docker
    monkeypatch.setattr(
        recovery,
        "docker",
        lambda state, *args: b"" if args[0] == "ps" else original(state, *args),
    )
    recovery.release_replacement(enrolled["root"], "worker", identity, proof)
    recovery.automatic_enable(enrolled["root"], "worker")
    recovery.suspend(enrolled["root"], "worker")
    before = (enrolled["root"] / "state.json").read_bytes()
    recovery.release_replacement(enrolled["root"], "worker", identity, proof)
    assert (enrolled["root"] / "state.json").read_bytes() == before
    state = json.loads(before)
    assert state["targets"]["worker"]["attempts"] == 194
    assert state["targets"]["worker"]["code"] == "OPERATOR_SUSPENDED"


@pytest.mark.parametrize(
    "change",
    ["identity", "proof", "reason", "pending", "stopped", "unhealthy", "predecessor"],
)
def test_release_refuses_any_other_scope(
    enrolled: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    identity, proof = blocked(enrolled)
    state = json.loads((enrolled["root"] / "state.json").read_bytes())
    target = state["targets"]["worker"]
    if change == "identity":
        identity = "b" * 64
    elif change == "proof":
        proof = "f" * 64
    elif change == "reason":
        target["replacement_block"]["code"] = "RUNTIME_CHECK_FAILED"
    elif change == "pending":
        target["replacement_policy_pending"] = True
    elif change == "stopped":
        enrolled["row"]["State"]["Running"] = False
    elif change == "unhealthy":
        enrolled["row"]["State"]["Health"]["Status"] = "unhealthy"
    original = recovery.docker
    monkeypatch.setattr(
        recovery,
        "docker",
        lambda current, *args: (
            (b"existing" if change == "predecessor" else b"")
            if args[0] == "ps"
            else original(current, *args)
        ),
    )
    recovery.commit(enrolled["root"], state)
    before = (enrolled["root"] / "state.json").read_bytes()
    with pytest.raises(ValueError):
        recovery.release_replacement(enrolled["root"], "worker", identity, proof)
    assert (enrolled["root"] / "state.json").read_bytes() == before
