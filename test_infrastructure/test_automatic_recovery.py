"""Finite restart frequency, automatic recovery and preserved operator intent."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery
import test_recovery

enrolled = test_recovery.enrolled


@pytest.fixture
def automatic(
    enrolled: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    original = recovery.docker
    events: list[dict[str, Any]] = []
    observed: list[dict[str, Any]] = []
    failures = [False]
    coverage = [1.0]

    def docker(state: dict[str, Any], *args: str) -> bytes:
        if args[0] == "events":
            enrolled["calls"].append(args)
            if args[-1] == "{{.TimeNano}}":
                return str(int(coverage[0] * 1e9)).encode()
            since, until = float(args[args.index("--since") + 1]), float(
                args[args.index("--until") + 1]
            )
            return b"\n".join(
                json.dumps(v).encode()
                for v in events
                if since <= v["timeNano"] / 1e9 <= until
            )
        if args[0] in {"start", "restart"}:
            targets = json.loads((enrolled["root"] / "state.json").read_bytes())[
                "targets"
            ]
            persisted = next(
                value
                for value in targets.values()
                if value.get("automatic", {}).get("intent", {}).get("phase")
                == "DISPATCH_INTENT"
            )
            observed.append(persisted)
            assert persisted["automatic"]["intent"]["phase"] == "DISPATCH_INTENT"
            assert persisted["attempts"] >= 1
            if failures[0]:
                raise subprocess.TimeoutExpired(["private-argument"], 20)
            if args[0] == "start":
                enrolled["calls"].append(args)
                row = enrolled["row"]
                row["State"].update(
                    Running=True,
                    Status="running",
                    ExitCode=0,
                    StartedAt=datetime.fromtimestamp(
                        enrolled["clock"][0] + 0.001, UTC
                    ).isoformat(),
                )
                row["State"]["Health"]["Status"] = "starting"
                return b""
        return original(state, *args)

    monkeypatch.setattr(recovery, "docker", docker)
    recovery.automatic_enable(enrolled["root"], "worker")
    enrolled.update(
        events=events, observed=observed, failures=failures, coverage=coverage
    )
    return enrolled


def step(
    value: dict[str, Any], now: float, health: str = "unhealthy"
) -> dict[str, Any]:
    value["clock"][0] = now
    value["row"]["State"]["Health"].update(
        Status=health,
        Log=[
            {
                "Start": datetime.fromtimestamp(now, UTC).isoformat(),
                "End": datetime.fromtimestamp(now, UTC).isoformat(),
                "ExitCode": 0 if health == "healthy" else 1,
            }
        ],
    )
    return cast(dict[str, Any], recovery.once(value["root"])["targets"]["worker"])


def event(value: dict[str, Any], when: float, action: str, code: str = "1") -> None:
    value["events"].append(
        {
            "Type": "container",
            "Action": action,
            "timeNano": int(when * 1e9),
            "Actor": {
                "ID": value["row"]["Id"],
                "Attributes": {"exitCode": code, "private": "do-not-save"},
            },
        }
    )


def crash(value: dict[str, Any], when: float = 1010, code: int = 1) -> None:
    value["row"]["State"].update(
        Running=False,
        Status="exited",
        ExitCode=code,
        FinishedAt=datetime.fromtimestamp(when, UTC).isoformat(),
    )
    event(value, when, "die", str(code))


def test_temporary_cooldown_and_recovery_do_not_reset_history(
    automatic: dict[str, Any],
) -> None:
    for now in range(1000, 1451, 10):
        result = step(automatic, now)
    assert result["attempts"] == 3 and not result["blocked"]
    assert result["next_due"] == 2350
    for now in range(1460, 2351, 10):
        result = step(automatic, now)
    assert result["attempts"] == 4 and not result["blocked"]
    assert result["next_due"] == 3250
    for now in range(2360, 4161, 30):
        result = step(automatic, now, "healthy")
    assert result["attempts"] == 4 and result["automatic"]["burst_attempts"] == 0
    assert any(
        e["code"] == "AUTOMATIC_STABLE_RECOVERY"
        for e in recovery.once(automatic["root"])["events"]
    )
    assert automatic["row"]["HostConfig"]["RestartPolicy"] == recovery.NO_RESTART


def test_proven_crash_starts_automatically(automatic: dict[str, Any]) -> None:
    crash(automatic)
    assert step(automatic, 1011)["code"] == "AUTOMATIC_BACKOFF"
    assert not automatic["observed"]
    result = step(automatic, 1041)
    assert result["attempts"] == 1 and result["code"] == "AUTOMATIC_RESTARTED"
    assert automatic["row"]["State"]["Running"]
    assert automatic["observed"][0]["automatic"]["intent"]["verb"] == "start"
    assert "do-not-save" not in (automatic["root"] / "state.json").read_text()


@pytest.mark.parametrize("action", ["stop", "kill", "pause", "restart"])
def test_manual_action_is_not_reversed(automatic: dict[str, Any], action: str) -> None:
    event(automatic, 1009, action)
    crash(automatic)
    result = step(automatic, 1011)
    assert result["blocked"] and not automatic["observed"]
    assert step(automatic, 99999)["blocked"] and not automatic["observed"]


def test_truncated_event_history_does_not_hide_manual_kill(
    automatic: dict[str, Any],
) -> None:
    crash(automatic)
    automatic["coverage"][0] = 1010
    result = step(automatic, 1011)
    assert result["code"] == "AUTOMATIC_EXIT_UNCONFIRMED" and not automatic["observed"]


def test_manual_stop_during_cooldown_stays_stopped(automatic: dict[str, Any]) -> None:
    crash(automatic)
    step(automatic, 1011)
    event(automatic, 1020, "stop")
    result = step(automatic, 1041)
    assert result["blocked"] and not automatic["observed"]


def test_external_start_rearms_observation_not_the_stopped_process(
    automatic: dict[str, Any],
) -> None:
    event(automatic, 1009, "stop")
    crash(automatic)
    assert step(automatic, 1011)["blocked"]
    automatic["row"]["State"].update(
        Running=True,
        Status="running",
        ExitCode=0,
        StartedAt=datetime.fromtimestamp(1100, UTC).isoformat(),
    )
    result = step(automatic, 1101, "healthy")
    assert not result["blocked"] and not automatic["observed"]


@pytest.mark.parametrize(
    "damage", ["fingerprint", "policy", "suspension", "clean-exit"]
)
def test_protective_barriers_survive_new_mode(
    automatic: dict[str, Any], damage: str
) -> None:
    if damage == "fingerprint":
        automatic["row"]["Image"] = "sha256:foreign"
    elif damage == "policy":
        automatic["row"]["HostConfig"]["RestartPolicy"]["Name"] = "always"
    elif damage == "suspension":
        recovery.suspend(automatic["root"], "worker")
    else:
        crash(automatic, code=0)
    for now in (1000, 1030, 99999):
        result = step(automatic, now)
    assert result["blocked"] and not automatic["observed"]


def test_lost_dispatch_is_not_repeated_and_late_result_is_reconciled(
    automatic: dict[str, Any],
) -> None:
    automatic["failures"][0] = True
    step(automatic, 1000)
    step(automatic, 1030)
    assert len(automatic["observed"]) == 1
    assert step(automatic, 1060)["code"] == "AUTOMATIC_RESULT_UNCONFIRMED"
    automatic["row"]["State"]["StartedAt"] = datetime.fromtimestamp(
        1070, UTC
    ).isoformat()
    result = step(automatic, 1071)
    assert result["code"] == "AUTOMATIC_RESTARTED" and not result["blocked"]
    assert len(automatic["observed"]) == 1


def test_unconfirmed_reconciliation_does_not_consume_action_slot(
    automatic: dict[str, Any],
) -> None:
    automatic["failures"][0] = True
    step(automatic, 1000)
    step(automatic, 1030)
    state = json.loads((automatic["root"] / "state.json").read_bytes())
    target = state["targets"]["worker"]
    assert target["automatic"]["intent"]["phase"] == "DISPATCH_INTENT"

    acted = recovery.automatic_tick(
        automatic["root"],
        state,
        "worker",
        copy.deepcopy(automatic["row"]),
        1060,
        False,
    )

    assert acted is False
    assert state["targets"]["worker"]["code"] == "AUTOMATIC_RESULT_UNCONFIRMED"
    assert len(automatic["observed"]) == 1


def test_sleep_does_not_replenish_series(automatic: dict[str, Any]) -> None:
    step(automatic, 1000)
    step(automatic, 1030)
    step(automatic, 1040, "healthy")
    result = step(automatic, 10000, "healthy")
    assert result["automatic"]["burst_attempts"] == 1 and result["attempts"] == 1


def test_sleep_defers_actions_until_resume_grace_expires(
    automatic: dict[str, Any],
) -> None:
    step(automatic, 1000)
    result = step(automatic, 10000)
    assert result["code"] == "AUTOMATIC_RESUME_GRACE"
    assert result["attempts"] == 0 and not automatic["observed"]
    result = step(automatic, 10119)
    assert result["attempts"] == 0 and not automatic["observed"]
    result = step(automatic, 10120)
    assert result["code"] == "AUTOMATIC_BACKOFF"


def test_compose_dependency_wait_does_not_spend_attempt(
    automatic: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    automatic["row"]["Config"]["Labels"]["com.docker.compose.depends_on"] = (
        "database:service_healthy:false"
    )
    dependency = copy.deepcopy(automatic["row"])
    dependency.update(Id="b" * 64, Name="/database")
    dependency["Config"]["Labels"].update(
        {
            "com.docker.compose.service": "database",
            "com.docker.compose.depends_on": "",
        }
    )
    dependency["State"]["Health"]["Status"] = "unhealthy"
    original = recovery.docker

    def docker(state: dict[str, Any], *args: str) -> bytes:
        if args[0] == "ps":
            return (dependency["Id"] + "\n").encode()
        if args[0] == "inspect" and args[1] == dependency["Id"]:
            return json.dumps([copy.deepcopy(dependency)]).encode()
        return original(state, *args)

    monkeypatch.setattr(recovery, "docker", docker)
    step(automatic, 1000)
    result = step(automatic, 1030)
    assert result["code"] == "AUTOMATIC_DEPENDENCY_WAIT"
    assert result["attempts"] == 0 and not automatic["observed"]
    dependency["State"]["Health"]["Status"] = "healthy"
    dependency["State"]["Health"]["Log"] = [
        {
            "Start": datetime.fromtimestamp(1150, UTC).isoformat(),
            "End": datetime.fromtimestamp(1150, UTC).isoformat(),
            "ExitCode": 0,
        }
    ]
    result = step(automatic, 1150)
    assert result["attempts"] == 1 and automatic["observed"]


def test_healthy_compose_replacement_is_handed_off_automatically(
    automatic: dict[str, Any],
) -> None:
    row = automatic["row"]
    row.update(Id="b" * 64, Image="sha256:" + "d" * 64, Name="/worker")
    row["Config"]["Labels"]["com.docker.compose.config-hash"] = "e" * 64
    first = step(automatic, 1000, "healthy")
    assert first["code"] == "AUTOMATIC_HANDOFF_WAIT"
    assert first["expected"]["id"] == "a" * 64
    assert step(automatic, 1029, "healthy")["expected"]["id"] == "a" * 64
    current = step(automatic, 1030, "healthy")
    assert current["expected"]["id"] == "b" * 64
    assert not current["blocked"] and current["policy_limit"] == 0
    assert any(
        event["code"] == "AUTOMATIC_HANDOFF_COMPLETE"
        for event in recovery.once(automatic["root"])["events"]
    )


def test_compose_handoff_preserves_an_unrelated_protective_block(
    automatic: dict[str, Any],
) -> None:
    row = automatic["row"]
    row.update(Id="b" * 64, Image="sha256:" + "d" * 64, Name="/worker")
    row["Config"]["Labels"]["com.docker.compose.config-hash"] = "e" * 64
    state = json.loads((automatic["root"] / "state.json").read_bytes())
    target = state["targets"]["worker"]
    target.update(blocked=True, code="AUTOMATIC_EXIT_UNCONFIRMED")
    (automatic["root"] / "state.json").write_text(json.dumps(state))

    result = step(automatic, 1000, "healthy")

    assert result["blocked"] is True
    assert result["code"] == "AUTOMATIC_EXIT_UNCONFIRMED"
    assert result["last_failure_code"] == "TARGET_REPLACED"
    assert "automatic_handoff" not in result


def test_inspect_failure_preserves_lost_result_for_late_reconciliation(
    automatic: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    automatic["failures"][0] = True
    step(automatic, 1000)
    step(automatic, 1030)
    assert step(automatic, 1060)["code"] == "AUTOMATIC_RESULT_UNCONFIRMED"

    def unavailable(*_args: Any) -> dict[str, Any]:
        raise RuntimeError("temporary read failure")

    with monkeypatch.context() as context:
        context.setattr(recovery, "inspect", unavailable)
        result = step(automatic, 1070)
    assert result["code"] == "AUTOMATIC_RESULT_UNCONFIRMED"
    assert result["last_failure_code"] == "RUNTIME_CHECK_FAILED"
    automatic["row"]["State"]["StartedAt"] = datetime.fromtimestamp(
        1080, UTC
    ).isoformat()
    result = step(automatic, 1081)
    assert result["code"] == "AUTOMATIC_RESTARTED" and not result["blocked"]
    assert result["attempts"] == 1 and len(automatic["observed"]) == 1


def test_inspect_failure_does_not_overwrite_operator_hold(
    automatic: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery.suspend(automatic["root"], "worker")

    def unavailable(*_args: Any) -> dict[str, Any]:
        raise OSError("temporary read failure")

    with monkeypatch.context() as context:
        context.setattr(recovery, "inspect", unavailable)
        result = step(automatic, 1001)
    assert result["code"] == "OPERATOR_SUSPENDED" and result["blocked"]
    assert result["last_failure_code"] == "RUNTIME_CHECK_FAILED"
    result = step(automatic, 1002, "healthy")
    assert result["code"] == "OPERATOR_SUSPENDED" and result["blocked"]
    assert not automatic["observed"]


def test_temporary_read_failure_does_not_disable_active_recovery(
    automatic: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_args: Any) -> dict[str, Any]:
        raise OSError("temporary read failure")

    with monkeypatch.context() as context:
        context.setattr(recovery, "inspect", unavailable)
        assert not step(automatic, 1001)["blocked"]
    result = step(automatic, 1002, "healthy")
    assert result["code"] == "HEALTHY" and not result["blocked"]
    assert result["attempts"] == 0 and not automatic["observed"]


def test_legacy_history_is_preserved_and_stopped_target_not_adopted(
    enrolled: dict[str, Any],
) -> None:
    root = enrolled["root"]
    state = json.loads((root / "state.json").read_bytes())
    state["targets"]["worker"].update(
        attempts=194, blocked=True, code="CIRCUIT_OPEN", policy_limit=0
    )
    enrolled["row"]["HostConfig"]["RestartPolicy"] = dict(recovery.NO_RESTART)
    recovery.commit(root, state)
    recovery.automatic_enable(root, "worker")
    current = json.loads((root / "state.json").read_bytes())
    assert current["schema_version"] == 2
    target = current["targets"]["worker"]
    assert target["attempts"] == 194 and target["automatic"]["burst_attempts"] == 3
    assert not target["blocked"] and target["next_due"] == 1900
    enrolled["row"]["State"]["Running"] = False
    with pytest.raises(ValueError, match="LIVE_EXACT"):
        recovery.automatic_enable(root, "worker")


def test_masked_protective_block_is_not_a_runtime_circuit(
    enrolled: dict[str, Any],
) -> None:
    root = enrolled["root"]
    state = json.loads((root / "state.json").read_bytes())
    state["targets"]["worker"].update(
        blocked=True, code="CIRCUIT_OPEN", replacement_block={"code": "TARGET_REPLACED"}
    )
    recovery.commit(root, state)
    with pytest.raises(ValueError, match="PROTECTIVE_BLOCK"):
        recovery.automatic_enable(root, "worker")
    assert (
        "automatic"
        not in json.loads((root / "state.json").read_bytes())["targets"]["worker"]
    )


def test_existing_automatic_hold_is_not_cancelled_by_enable(
    automatic: dict[str, Any],
) -> None:
    recovery.suspend(automatic["root"], "worker")
    with pytest.raises(ValueError, match="PROTECTIVE_BLOCK"):
        recovery.automatic_enable(automatic["root"], "worker")
    assert not automatic["observed"]


def test_replacement_keeps_mode_and_series(automatic: dict[str, Any]) -> None:
    root, row = automatic["root"], automatic["row"]
    old = copy.deepcopy(row)
    row.update(Id="b" * 64, Image="sha256:" + "d" * 64)
    recovery.replace(root, "worker", old["Id"], 0, row["Image"])
    target = recovery.once(root)["targets"]["worker"]
    assert (
        "automatic" in target and not target["blocked"] and target["policy_limit"] == 0
    )
    assert row["HostConfig"]["RestartPolicy"] == recovery.NO_RESTART


def test_cancel_exact_hold_does_not_restore_native_loop(
    automatic: dict[str, Any],
) -> None:
    root = automatic["root"]
    recovery.suspend(root, "worker")
    target = json.loads((root / "state.json").read_bytes())["targets"]["worker"]
    recovery.cancel_suspend(
        root, "worker", automatic["row"]["Id"], recovery.suspension_proof(target)
    )
    target = recovery.once(root)["targets"]["worker"]
    assert not target["blocked"] and target["policy_limit"] == 0
    assert automatic["row"]["HostConfig"]["RestartPolicy"] == recovery.NO_RESTART


def test_negative_guard_retries_but_changed_guard_stays_closed(
    automatic: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = automatic["root"]
    path = root / "guard.py"
    path.write_text("# owner guard\n")
    state = json.loads((root / "state.json").read_bytes())
    state["targets"]["worker"]["guard"] = {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    recovery.commit(root, state)

    def reject(*_args: Any, **_kwargs: Any) -> bytes:
        return b'{"allowed":false}'

    monkeypatch.setattr(recovery, "execute", reject)
    step(automatic, 1000)
    result = step(automatic, 1030)
    assert not result["blocked"] and not automatic["observed"]
    assert result["next_due"] == 1150
    path.write_text("# changed\n")
    for now in range(1060, 1151, 30):
        result = step(automatic, now)
    assert result["blocked"] and result["code"] == "GUARD_CHANGED"


def test_two_targets_never_dispatch_twice_in_one_tick(
    automatic: dict[str, Any],
) -> None:
    root = automatic["root"]
    state = json.loads((root / "state.json").read_bytes())
    state["targets"]["second"] = copy.deepcopy(state["targets"]["worker"])
    recovery.commit(root, state)
    step(automatic, 1000)
    step(automatic, 1030)
    assert len(automatic["observed"]) == 1
