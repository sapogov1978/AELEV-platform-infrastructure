"""Finite operator recovery never discovers targets or erases restart budgets."""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery


@pytest.fixture
def enrolled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(shutil, "which", lambda _: "/bin/docker")
    monkeypatch.setattr(recovery, "execute", Mock(return_value=b"unix:///local.sock"))
    row: dict[str, Any] = {
        "Id": "a" * 64,
        "Image": "sha256:fixed",
        "RestartCount": 0,
        "Config": {
            "Labels": {
                "com.docker.compose.project": "test",
                "com.docker.compose.service": "worker",
            },
            "Healthcheck": {"Interval": 1_000_000_000},
        },
        "State": {"Running": True, "Health": {"Status": "healthy", "Log": []}},
        "HostConfig": {
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}
        },
    }
    calls: list[tuple[str, ...]] = []
    row["State"]["StartedAt"] = "1970-01-01T00:16:40Z"

    def docker(state: dict[str, Any], *args: str) -> bytes:
        calls.append(args)
        if args[0] == "info":
            return b"same-daemon"
        if args[0] == "inspect":
            return json.dumps([copy.deepcopy(row)]).encode()
        if args[0] == "update":
            policy = args[2].split(":")
            row["HostConfig"]["RestartPolicy"] = {
                "Name": policy[0],
                "MaximumRetryCount": int(policy[1]) if len(policy) == 2 else 0,
            }
        elif args[0] == "restart":
            row["RestartCount"] = 0
            row["State"]["Health"]["Status"] = "starting"
            row["State"]["StartedAt"] = datetime.fromtimestamp(
                clock[0] + len(calls) / 1000, UTC
            ).isoformat()
        return b""

    monkeypatch.setattr(recovery, "docker", docker)
    recovery.enroll(tmp_path, "worker", None)
    return {"root": tmp_path, "row": row, "calls": calls, "clock": clock}


def tick(test: dict[str, Any], now: float, health: str = "unhealthy") -> dict[str, Any]:
    test["clock"][0] = now
    test["row"]["State"]["Health"]["Status"] = health
    return cast(dict[str, Any], recovery.once(test["root"])["targets"]["worker"])


def restarts(test: dict[str, Any]) -> int:
    return sum(call[0] == "restart" for call in test["calls"])


def test_three_restarts_then_sticky_circuit(enrolled: dict[str, Any]) -> None:
    tick(enrolled, 1000)
    tick(enrolled, 1029)
    assert restarts(enrolled) == 0
    assert tick(enrolled, 1030)["attempts"] == 1
    tick(enrolled, 1149)
    assert restarts(enrolled) == 1
    assert tick(enrolled, 1150)["attempts"] == 2
    tick(enrolled, 1250)
    tick(enrolled, 1350)
    tick(enrolled, 1449)
    assert restarts(enrolled) == 2
    assert tick(enrolled, 1450)["code"] == "CIRCUIT_OPEN"
    assert tick(enrolled, 999999, "healthy")["blocked"] is True
    assert restarts(enrolled) == 3
    assert enrolled["row"]["HostConfig"]["RestartPolicy"]["Name"] == "no"


def test_native_crashes_consume_same_budget(enrolled: dict[str, Any]) -> None:
    enrolled["row"]["RestartCount"] = 3
    assert tick(enrolled, 1000)["attempts"] == 3
    assert restarts(enrolled) == 0
    assert enrolled["row"]["HostConfig"]["RestartPolicy"]["Name"] == "no"


@pytest.mark.parametrize("native_count", [1, 3, 8])
def test_enrollment_keeps_preexisting_native_history(
    enrolled: dict[str, Any], native_count: int
) -> None:
    root = enrolled["root"]
    state = json.loads((root / "state.json").read_bytes())
    state["targets"].pop("worker")
    recovery.commit(root, state)
    enrolled["row"]["RestartCount"] = native_count
    recovery.enroll(root, "worker", None)
    target = json.loads((root / "state.json").read_bytes())["targets"]["worker"]
    assert target["attempts"] == native_count
    assert target["blocked"] is (native_count >= 3)
    assert target["policy_limit"] == max(0, 3 - native_count)


@pytest.fixture
def stopped_predecessor(enrolled: dict[str, Any]) -> dict[str, Any]:
    root, row = enrolled["root"], enrolled["row"]
    state = json.loads((root / "state.json").read_bytes())
    state["targets"].pop("worker")
    recovery.commit(root, state)
    row.update(Name="/worker", Image="sha256:" + "d" * 64, RestartCount=194)
    row["Config"]["Labels"]["com.docker.compose.config-hash"] = "fixed-config"
    row["State"] = {
        "Status": "exited",
        "Running": False,
        "Paused": False,
        "Restarting": False,
        "Dead": False,
        "FinishedAt": "1970-01-01T00:16:40Z",
        "ExitCode": 137,
    }
    row["HostConfig"]["RestartPolicy"] = {"Name": "no", "MaximumRetryCount": 0}
    enrolled["calls"].clear()
    return enrolled


@pytest.mark.parametrize("native_count", [194, 674])
def test_stopped_adoption_preserves_exhausted_history_through_replacement(
    stopped_predecessor: dict[str, Any], native_count: int
) -> None:
    root, row = stopped_predecessor["root"], stopped_predecessor["row"]
    row["RestartCount"] = native_count
    recovery.adopt_stopped(root, "worker", row["Id"], native_count, row["Image"])
    before = (root / "state.json").read_bytes()
    target = json.loads(before)["targets"]["worker"]
    assert target["attempts"] == target["native_count"] == native_count
    assert target["blocked"] and target["code"] == "CIRCUIT_OPEN"
    assert target["policy_limit"] == 0
    assert all(call[0] in {"info", "inspect"} for call in stopped_predecessor["calls"])
    recovery.adopt_stopped(root, "worker", row["Id"], native_count, row["Image"])
    assert (root / "state.json").read_bytes() == before
    recovery.once(root)
    assert restarts(stopped_predecessor) == 0
    row.update(Id="b" * 64, Image="sha256:" + "c" * 64, RestartCount=0)
    row["State"].update(Running=True, Status="running", Health={"Status": "healthy"})
    recovery.replace(root, "worker", "a" * 64, native_count, row["Image"])
    target = json.loads((root / "state.json").read_bytes())["targets"]["worker"]
    assert target["attempts"] == native_count and target["policy_limit"] == 0
    assert target["blocked"] and target["code"] == "CIRCUIT_OPEN"
    assert target["stopped_adoption"]["native_count"] == native_count
    assert row["HostConfig"]["RestartPolicy"] == {"Name": "no", "MaximumRetryCount": 0}
    assert restarts(stopped_predecessor) == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "live",
        "created",
        "dead",
        "restarting",
        "paused",
        "policy",
        "id",
        "image",
        "count",
        "bool-count",
        "name",
        "config",
        "oneoff",
        "project",
        "daemon",
    ],
)
def test_stopped_adoption_rejects_unconfirmed_predecessor_without_writes(
    stopped_predecessor: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    root, row = stopped_predecessor["root"], stopped_predecessor["row"]
    image = row["Image"]
    if mutation == "live":
        row["State"].update(Status="running", Running=True)
    elif mutation == "created":
        row["State"]["Status"] = "created"
    elif mutation in {"dead", "restarting", "paused"}:
        row["State"][mutation.title()] = True
    elif mutation == "policy":
        row["HostConfig"]["RestartPolicy"]["Name"] = "on-failure"
    elif mutation in {"id", "image", "count", "bool-count", "name"}:
        key, value = {
            "id": ("Id", "b" * 64),
            "image": ("Image", "sha256:" + "e" * 64),
            "count": ("RestartCount", 195),
            "bool-count": ("RestartCount", True),
            "name": ("Name", "/other"),
        }[mutation]
        row[key] = value
    elif mutation == "daemon":
        original = recovery.docker
        monkeypatch.setattr(
            recovery,
            "docker",
            lambda state, *args: (
                b"other" if args[0] == "info" else original(state, *args)
            ),
        )
    else:
        key, value = {
            "config": ("config-hash", ""),
            "oneoff": ("oneoff", "true"),
            "project": ("project", ""),
        }[mutation]
        row["Config"]["Labels"]["com.docker.compose." + key] = value
    before = (root / "state.json").read_bytes()
    with pytest.raises(ValueError):
        recovery.adopt_stopped(root, "worker", "a" * 64, 194, image)
    assert (root / "state.json").read_bytes() == before
    assert all(call[0] in {"info", "inspect"} for call in stopped_predecessor["calls"])


@pytest.mark.parametrize("native_count", [0, 1, 2, -1, True])
def test_stopped_adoption_cannot_create_fresh_budget(
    stopped_predecessor: dict[str, Any], native_count: int
) -> None:
    root, row = stopped_predecessor["root"], stopped_predecessor["row"]
    before = (root / "state.json").read_bytes()
    with pytest.raises(ValueError, match="EXHAUSTED_PREDECESSOR_PROOF_REQUIRED"):
        recovery.adopt_stopped(root, "worker", row["Id"], native_count, row["Image"])
    assert (root / "state.json").read_bytes() == before
    assert not stopped_predecessor["calls"]


def test_stopped_adoption_cannot_replace_existing_record(
    stopped_predecessor: dict[str, Any],
) -> None:
    root, row = stopped_predecessor["root"], stopped_predecessor["row"]
    recovery.adopt_stopped(root, "worker", row["Id"], 194, row["Image"])
    state = json.loads((root / "state.json").read_bytes())
    state["targets"]["worker"]["code"] = "RUNTIME_CHECK_FAILED"
    recovery.commit(root, state)
    before = (root / "state.json").read_bytes()
    with pytest.raises(ValueError, match="PREDECESSOR_ALREADY_REGISTERED"):
        recovery.adopt_stopped(root, "worker", row["Id"], 194, row["Image"])
    assert (root / "state.json").read_bytes() == before


def test_stopped_adoption_cli_uses_the_same_held_registration(
    stopped_predecessor: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, row = stopped_predecessor["root"], stopped_predecessor["row"]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recovery.py",
            "adopt-stopped",
            "--state",
            str(root),
            "--container",
            "worker",
            "--expected-id",
            row["Id"],
            "--previous-native-count",
            "194",
            "--image",
            row["Image"],
        ],
    )
    recovery.main()
    target = json.loads((root / "state.json").read_bytes())["targets"]["worker"]
    assert target["attempts"] == 194 and target["blocked"]
    assert all(call[0] in {"info", "inspect"} for call in stopped_predecessor["calls"])


def test_stopped_predecessor_does_not_weaken_live_enrollment(
    stopped_predecessor: dict[str, Any],
) -> None:
    root = stopped_predecessor["root"]
    before = (root / "state.json").read_bytes()
    with pytest.raises(ValueError, match="LIVE_HEALTHCHECK_TARGET_REQUIRED"):
        recovery.enroll(root, "worker", None)
    assert (root / "state.json").read_bytes() == before


def test_stopped_adoption_rechecks_exact_id_before_commit(
    stopped_predecessor: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, row = stopped_predecessor["root"], stopped_predecessor["row"]
    original = recovery.docker

    def changed(state: dict[str, Any], *args: str) -> bytes:
        if args == ("inspect", row["Id"]):
            row["State"]["ExitCode"] = 0
        return original(state, *args)

    monkeypatch.setattr(recovery, "docker", changed)
    before = (root / "state.json").read_bytes()
    with pytest.raises(ValueError, match="STOPPED_PREDECESSOR_CHANGED"):
        recovery.adopt_stopped(root, "worker", row["Id"], 194, row["Image"])
    assert (root / "state.json").read_bytes() == before


@pytest.mark.parametrize("attempts", [1, 3, 8])
def test_release_replacement_never_replenishes_budget(
    enrolled: dict[str, Any], attempts: int
) -> None:
    root = enrolled["root"]
    state = json.loads((root / "state.json").read_bytes())
    target = state["targets"]["worker"]
    target.update(
        attempts=attempts,
        native_count=attempts,
        blocked=True,
        code="OPERATOR_SUSPENDED",
    )
    recovery.commit(root, state)
    row = enrolled["row"]
    row.update(Id="b" * 64, Image="sha256:" + "c" * 64, RestartCount=0)
    recovery.replace(root, "worker", "a" * 64, attempts, row["Image"])
    before = (root / "state.json").read_bytes()
    target = json.loads(before)["targets"]["worker"]
    assert target["attempts"] == attempts
    assert target["native_count"] == 0
    assert target["policy_limit"] == max(0, 3 - attempts)
    assert target["blocked"] is (attempts >= 3)
    assert restarts(enrolled) == 0
    recovery.replace(root, "worker", "a" * 64, attempts, row["Image"])
    assert (root / "state.json").read_bytes() == before


def test_replacement_recovers_lost_native_policy_ack(
    enrolled: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, row = enrolled["root"], enrolled["row"]
    row.update(Id="b" * 64, Image="sha256:" + "c" * 64, RestartCount=0)
    original = recovery.docker
    interrupted = [False]

    def execute(state: dict[str, Any], *args: str) -> bytes:
        raw = original(state, *args)
        if args[0] == "update" and not interrupted[0]:
            interrupted[0] = True
            raise RuntimeError("lost policy response")
        return raw

    monkeypatch.setattr(recovery, "docker", execute)
    with pytest.raises(RuntimeError):
        recovery.replace(root, "worker", "a" * 64, 0, row["Image"])
    target = json.loads((root / "state.json").read_bytes())["targets"]["worker"]
    assert target["replacement_policy_pending"] is True and target["blocked"] is True
    recovery.replace(root, "worker", "a" * 64, 0, row["Image"])
    target = json.loads((root / "state.json").read_bytes())["targets"]["worker"]
    assert target["attempts"] == 0
    assert target["replacement_policy_pending"] is False and target["blocked"] is False
    assert restarts(enrolled) == 0


@pytest.mark.parametrize("lost_ack", [False, True])
@pytest.mark.parametrize("nested_block", [False, True])
def test_held_replacement_preserves_cause_budget_and_native_no(
    enrolled: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    lost_ack: bool,
    nested_block: bool,
) -> None:
    root, row = enrolled["root"], enrolled["row"]
    state = json.loads((root / "state.json").read_bytes())
    target = state["targets"]["worker"]
    cause = "TARGET_REPLACED" if nested_block else "RUNTIME_CHECK_FAILED"
    attempts = 194 if nested_block else 2
    target.update(
        attempts=attempts,
        blocked=True,
        code="CIRCUIT_OPEN" if nested_block else cause,
        last_failure_code=cause,
        last_failure_at=900,
    )
    if nested_block:
        target["replacement_block"] = {"code": cause}
    recovery.commit(root, state)
    row.update(Id="b" * 64, Image="sha256:" + "c" * 64, RestartCount=0)
    row["HostConfig"]["RestartPolicy"] = {"Name": "no", "MaximumRetryCount": 0}
    original = recovery.docker
    interrupted = [False]

    def execute(state: dict[str, Any], *args: str) -> bytes:
        result = original(state, *args)
        if args[0] == "update" and lost_ack and not interrupted[0]:
            interrupted[0] = True
            raise RuntimeError("lost response")
        return result

    monkeypatch.setattr(recovery, "docker", execute)
    if lost_ack:
        with pytest.raises(RuntimeError, match="lost response"):
            recovery.replace(
                root, "worker", "a" * 64, 0, row["Image"], preserve_block=True
            )
    recovery.replace(root, "worker", "a" * 64, 0, row["Image"], preserve_block=True)
    before = (root / "state.json").read_bytes()
    result = json.loads(before)["targets"]["worker"]
    assert result["attempts"] == attempts and result["blocked"]
    assert result["code"] == result["last_failure_code"] == cause
    assert result["replacement_block"] == {"code": cause}
    events = json.loads(before)["events"]
    intents = [
        event for event in events if event["code"] == "RELEASE_REPLACEMENT_INTENT"
    ]
    assert len(intents) == 1 and intents[0]["previous_block_code"] == cause
    assert result["last_failure_at"] == 900
    assert row["HostConfig"]["RestartPolicy"] == {"Name": "no", "MaximumRetryCount": 0}
    recovery.replace(root, "worker", "a" * 64, 0, row["Image"], preserve_block=True)
    assert (root / "state.json").read_bytes() == before
    with pytest.raises(ValueError, match="BLOCK_MODE_MISMATCH"):
        recovery.replace(root, "worker", "a" * 64, 0, row["Image"])
    assert tick(enrolled, 2000)["code"] == ("CIRCUIT_OPEN" if nested_block else cause)
    assert restarts(enrolled) == 0
    row["State"]["Health"]["Status"] = "healthy"
    row["HostConfig"]["RestartPolicy"] = {"Name": "on-failure", "MaximumRetryCount": 1}
    with pytest.raises(ValueError, match="BLOCK_NOT_PRESERVED"):
        recovery.replace(root, "worker", "a" * 64, 0, row["Image"], preserve_block=True)


@pytest.mark.parametrize("defect", ["not_blocked", "native_enabled"])
def test_held_replacement_rejects_missing_hold_before_writing(
    enrolled: dict[str, Any], defect: str
) -> None:
    root, row = enrolled["root"], enrolled["row"]
    state = json.loads((root / "state.json").read_bytes())
    state["targets"]["worker"].update(
        blocked=defect != "not_blocked", code="RUNTIME_CHECK_FAILED"
    )
    recovery.commit(root, state)
    before = (root / "state.json").read_bytes()
    row.update(Id="b" * 64, Image="sha256:" + "c" * 64)
    if defect == "not_blocked":
        row["HostConfig"]["RestartPolicy"] = {"Name": "no", "MaximumRetryCount": 0}
    with pytest.raises(ValueError, match="BLOCK_NOT_PRESERVED"):
        recovery.replace(root, "worker", "a" * 64, 0, row["Image"], preserve_block=True)
    assert (root / "state.json").read_bytes() == before
    assert restarts(enrolled) == 0


@pytest.mark.parametrize(
    "defect",
    [
        "predecessor",
        "image",
        "service",
        "stopped",
        "unhealthy",
        "lower-count",
        "security-block",
    ],
)
def test_replacement_proof_mismatch_does_not_change_state(
    enrolled: dict[str, Any], defect: str
) -> None:
    root, row = enrolled["root"], enrolled["row"]
    row.update(Id="b" * 64, Image="sha256:" + "c" * 64, RestartCount=0)
    previous, image, count = "a" * 64, row["Image"], 0
    if defect == "predecessor":
        previous = "d" * 64
    elif defect == "image":
        image = "sha256:" + "d" * 64
    elif defect == "service":
        row["Config"]["Labels"]["com.docker.compose.service"] = "other"
    elif defect == "stopped":
        row["State"]["Running"] = False
    elif defect == "unhealthy":
        row["State"]["Health"]["Status"] = "unhealthy"
    else:
        state = json.loads((root / "state.json").read_bytes())
        target = state["targets"]["worker"]
        if defect == "lower-count":
            target["native_count"] = 1
        else:
            target.update(blocked=True, code="GUARD_CHANGED")
        recovery.commit(root, state)
    before = (root / "state.json").read_bytes()
    with pytest.raises(ValueError, match="REPLACEMENT"):
        recovery.replace(root, "worker", previous, count, image)
    assert (root / "state.json").read_bytes() == before and restarts(enrolled) == 0


def test_native_and_health_restarts_do_not_have_independent_limits(
    enrolled: dict[str, Any],
) -> None:
    enrolled["row"]["RestartCount"] = 1
    tick(enrolled, 1000)
    assert tick(enrolled, 1030)["attempts"] == 2
    enrolled["row"]["RestartCount"] = 1
    assert tick(enrolled, 1040)["code"] == "CIRCUIT_OPEN"
    assert restarts(enrolled) == 1


def test_observed_manual_stop_is_not_started(enrolled: dict[str, Any]) -> None:
    enrolled["row"]["State"]["Running"] = False
    assert tick(enrolled, 1000)["code"] == "STOPPED_NO_AUTOSTART"
    assert restarts(enrolled) == 0


def test_replacement_is_not_adopted(enrolled: dict[str, Any]) -> None:
    enrolled["row"]["Id"] = "b" * 64
    assert tick(enrolled, 1000)["code"] == "TARGET_REPLACED"
    assert restarts(enrolled) == 0


def test_recovery_does_not_reset_budget_or_failure_history(
    enrolled: dict[str, Any],
) -> None:
    tick(enrolled, 1000)
    tick(enrolled, 1030)
    target = tick(enrolled, 1040, "healthy")
    assert target["attempts"] == 1
    assert target["last_failure_code"] == "UNHEALTHY"
    report = json.loads((enrolled["root"] / "report.json").read_text())
    assert report["targets"]["worker"]["last_failure_code"] == "UNHEALTHY"


def test_clock_gap_reconfirms_failure_without_resetting_attempts(
    enrolled: dict[str, Any],
) -> None:
    tick(enrolled, 1000)
    tick(enrolled, 1030)
    assert tick(enrolled, 10000)["attempts"] == 1
    assert restarts(enrolled) == 1


def test_stale_probe_and_missing_first_check_are_bounded(
    enrolled: dict[str, Any],
) -> None:
    row = enrolled["row"]
    row["State"]["StartedAt"] = "1970-01-01T00:15:00Z"
    row["State"]["Health"]["Status"] = "starting"
    assert recovery.bad_probe(row, 959) is None
    assert recovery.bad_probe(row, 961) == "PROBE_STALE"
    row["State"]["Health"]["Log"] = [{"End": "1970-01-01T00:15:00Z"}]
    assert recovery.bad_probe(row, 944) is None
    assert recovery.bad_probe(row, 946) == "PROBE_STALE"


def test_recovery_during_guard_does_not_restart(
    enrolled: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    path = tmp_path / "guard.py"
    path.write_text("owner guard")
    state = json.loads((tmp_path / "state.json").read_text())
    state["targets"]["worker"]["guard"] = {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    recovery.commit(tmp_path, state)

    def allow(command: list[str], *, timeout: float) -> bytes:
        enrolled["row"]["State"]["Health"]["Status"] = "healthy"
        return b'{"allowed":true}'

    monkeypatch.setattr(recovery, "execute", allow)
    tick(enrolled, 1000)
    assert tick(enrolled, 1030)["attempts"] == 0
    assert restarts(enrolled) == 0


def test_guard_change_blocks_actions(enrolled: dict[str, Any], tmp_path: Path) -> None:
    path = tmp_path / "guard.py"
    path.write_text("changed")
    state = json.loads((tmp_path / "state.json").read_text())
    state["targets"]["worker"]["guard"] = {"path": str(path), "sha256": "wrong"}
    recovery.commit(tmp_path, state)
    tick(enrolled, 1000)
    assert tick(enrolled, 1030)["code"] == "GUARD_CHANGED"
    assert restarts(enrolled) == 0


def test_negative_owner_guard_does_not_spend_attempts(
    enrolled: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    path = tmp_path / "guard.py"
    path.write_text("owner guard")
    state = json.loads((tmp_path / "state.json").read_text())
    state["targets"]["worker"]["guard"] = {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    recovery.commit(tmp_path, state)
    monkeypatch.setattr(
        recovery,
        "execute",
        Mock(return_value=b'{"allowed":false,"code":"PRIMARY_REQUIRES_SWITCHOVER"}'),
    )
    tick(enrolled, 1000)
    target = tick(enrolled, 1030)
    assert target["code"] == "OWNER_GUARD_PRIMARY_REQUIRES_SWITCHOVER"
    assert target["attempts"] == restarts(enrolled) == 0


def test_external_policy_change_cuts_unbounded_native_loop(
    enrolled: dict[str, Any],
) -> None:
    enrolled["row"]["HostConfig"]["RestartPolicy"] = {
        "Name": "always",
        "MaximumRetryCount": 0,
    }
    assert tick(enrolled, 1000)["code"] == "RESTART_POLICY_CHANGED"
    assert enrolled["row"]["HostConfig"]["RestartPolicy"]["Name"] == "no"
    assert restarts(enrolled) == 0


def test_status_does_not_wait_for_a_runtime_operation(
    enrolled: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import fcntl

    with (enrolled["root"] / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(
            sys, "argv", ["recovery.py", "status", "--state", str(enrolled["root"])]
        )
        recovery.main()
        assert "worker" in json.loads(capsys.readouterr().out)["targets"]


def test_intent_survives_unknown_runtime_outcome(
    enrolled: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = recovery.docker

    def failed(state: dict[str, Any], *args: str) -> bytes:
        if args[0] == "restart":
            # The reservation is durable before the external call.
            stored = json.loads((enrolled["root"] / "state.json").read_text())
            assert stored["targets"]["worker"]["attempts"] == 1
            raise RuntimeError("uncertain")
        return original(state, *args)

    monkeypatch.setattr(recovery, "docker", failed)
    tick(enrolled, 1000)
    assert tick(enrolled, 1030)["attempts"] == 1
    assert tick(enrolled, 1050)["blocked"] is True


@pytest.mark.skipif(
    os.environ.get("AELEV_INFRASTRUCTURE_RECOVERY_DOCKER_TEST") != "1",
    reason="explicit Docker fault test required",
)
def test_real_unhealthy_recovery_is_finite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = "aelev-recovery-test-" + uuid4().hex[:12]
    name = project + "-worker"
    identity = (
        recovery.execute(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--label",
                "com.docker.compose.project=" + project,
                "--label",
                "com.docker.compose.service=worker",
                "--network",
                "none",
                "--read-only",
                "--memory",
                "64m",
                "--cpus",
                "0.1",
                "--health-cmd",
                "exit 1",
                "--health-interval",
                "1s",
                "--health-timeout",
                "1s",
                "--health-retries",
                "1",
                "python:3.13.7-alpine3.22@sha256:9ba6d8cbebf0fb6546ae71f2a1c14f6ffd2fdab83af7fa5669734ef30ad48844",
                "python",
                "-c",
                "import time; time.sleep(600)",
            ]
        )
        .decode()
        .strip()
    )
    try:
        recovery.enroll(tmp_path, name, None)
        real_time = time.time
        clock = [real_time()]
        monkeypatch.setattr(time, "time", lambda: clock[0])

        def unhealthy() -> None:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                row = json.loads(recovery.execute(["docker", "inspect", identity]))[0]
                if row["State"]["Health"]["Status"] == "unhealthy":
                    return
                time.sleep(0.2)
            raise TimeoutError("temporary fault container did not become unhealthy")

        unhealthy()
        recovery.once(tmp_path)
        for delay in (30, 120, 300):
            unhealthy()
            deadline = clock[0] + delay
            while clock[0] < deadline:
                clock[0] = min(deadline, clock[0] + 15)
                recovery.once(tmp_path)
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["targets"][name]["attempts"] == 3
        assert state["targets"][name]["blocked"] is True
        current = json.loads(recovery.execute(["docker", "inspect", identity]))[0]
        started = current["State"]["StartedAt"]
        assert current["HostConfig"]["RestartPolicy"]["Name"] == "no"
        clock[0] += 500
        recovery.once(tmp_path)
        assert (
            json.loads(recovery.execute(["docker", "inspect", identity]))[0]["State"][
                "StartedAt"
            ]
            == started
        )
        recovery.execute(["docker", "stop", "--time", "1", identity])
        recovery.once(tmp_path)
        assert (
            json.loads(recovery.execute(["docker", "inspect", identity]))[0]["State"][
                "Running"
            ]
            is False
        )
    finally:
        current = json.loads(recovery.execute(["docker", "inspect", identity]))[0]
        assert current["Config"]["Labels"]["com.docker.compose.project"] == project
        recovery.execute(["docker", "rm", "-f", identity])
