"""Explicit registration uses the old controller without erasing prior history."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery
import recovery_cli as cli
import recovery_host as host
from test_recovery_cli import fixture as enrolled_fixture  # noqa: F401
from test_recovery_cli import base_enrolled_fixture  # noqa: F401

IMAGE = "sha256:" + "f" * 64


@pytest.fixture
def fixture(request: pytest.FixtureRequest) -> dict[str, Any]:
    case = request.getfixturevalue("enrolled_fixture")
    root, row = case["root"], case["row"]
    current = cli.state(root)
    current["targets"].pop("worker")
    current["events"] = [{"code": "PRIOR_HISTORY", "attempts": 71}]
    recovery.commit(root, current)
    row["Name"] = "/worker"
    row["Config"]["Labels"]["io.aelev.recovery.registration"] = "unguarded-automatic-v1"
    row["Config"]["Healthcheck"]["Test"] = ["CMD", "true"]
    row["HostConfig"]["RestartPolicy"] = {"Name": "no", "MaximumRetryCount": 0}
    case["request"]["action"] = "register"
    return case


@pytest.mark.parametrize("count", [0, 2, 3, 19])
def test_registration_preserves_native_history_and_does_not_start(
    fixture: dict[str, Any],
    count: int,
) -> None:
    case = fixture
    case["row"]["RestartCount"] = count
    answer = cli.run(case["root"], case["request"], host_timer_quiesced=True)
    result = answer["targets"]["worker"]
    assert result["attempts"] == result["native_count"] == count
    assert result["policy_limit"] == 0
    assert result["registration_complete"] is True
    assert result["automatic_recovery"] is True
    after = cli.state(case["root"])
    assert after["targets"]["worker"]["automatic"]["burst_attempts"] == min(count, 3)
    assert after["events"][0] == {"code": "PRIOR_HISTORY", "attempts": 71}
    assert after["targets"]["unrelated"] == {"untouched": True}
    before = (case["root"] / "state.json").read_bytes()
    assert cli.run(case["root"], case["request"], host_timer_quiesced=True) == answer
    assert (case["root"] / "state.json").read_bytes() == before
    assert not any(
        args[0] in {"start", "restart", "stop", "kill"} for args in case["calls"]
    )


@pytest.mark.parametrize(
    "change", ["label", "unhealthy", "stopped", "foreign", "infinite", "no-probe"]
)
def test_unsafe_target_is_refused_before_any_state_write(
    fixture: dict[str, Any], change: str
) -> None:
    row = fixture["row"]
    if change == "label":
        row["Config"]["Labels"].pop("io.aelev.recovery.registration")
    elif change == "unhealthy":
        row["State"]["Health"]["Status"] = "unhealthy"
    elif change == "stopped":
        row["State"]["Running"] = False
    elif change == "foreign":
        row["Image"] = IMAGE
    elif change == "infinite":
        row["HostConfig"]["RestartPolicy"] = {
            "Name": "unless-stopped",
            "MaximumRetryCount": 0,
        }
    else:
        row["Config"]["Healthcheck"]["Test"] = ["NONE"]
    before = (fixture["root"] / "state.json").read_bytes()
    with pytest.raises(ValueError, match="REGISTRATION"):
        cli.run(fixture["root"], fixture["request"], host_timer_quiesced=True)
    assert (fixture["root"] / "state.json").read_bytes() == before


def test_repeat_cannot_cancel_suspend_or_manual_stop(fixture: dict[str, Any]) -> None:
    root, value = fixture["root"], fixture["request"]
    cli.run(root, value, host_timer_quiesced=True)
    recovery.suspend(root, "worker")
    fixture["row"]["State"]["Running"] = False
    before = (root / "state.json").read_bytes()
    answer = cli.run(root, value, host_timer_quiesced=True)
    assert answer["targets"]["worker"]["blocked"] is True
    assert answer["targets"]["worker"]["code"] == "OPERATOR_SUSPENDED"
    assert (root / "state.json").read_bytes() == before


def test_existing_unrelated_enrollment_is_not_migrated(fixture: dict[str, Any]) -> None:
    current = cli.state(fixture["root"])
    current["targets"]["worker"] = {
        "expected": fixture["request"]["targets"][0]["expected"],
        "attempts": 71,
    }
    recovery.commit(fixture["root"], current)
    before = (fixture["root"] / "state.json").read_bytes()
    with pytest.raises(ValueError, match="EXISTING_TARGET_PRESERVED"):
        cli.run(fixture["root"], fixture["request"], host_timer_quiesced=True)
    assert (fixture["root"] / "state.json").read_bytes() == before


def test_lost_enable_ack_resumes_the_same_intent(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cli.transport
    lost = [False]

    def transport(current: dict[str, Any], *args: str) -> bytes:
        result = original(current, *args)
        if args[0] == "update" and not lost[0]:
            lost[0] = True
            raise OSError("synthetic lost ACK")
        return result

    monkeypatch.setattr(cli, "transport", transport)
    with pytest.raises(OSError):
        cli.run(fixture["root"], fixture["request"], host_timer_quiesced=True)
    pending = cli.state(fixture["root"])
    assert pending["targets"]["worker"]["registration"]["phase"] == "PREPARED"
    answer = cli.run(fixture["root"], fixture["request"], host_timer_quiesced=True)
    assert answer["targets"]["worker"]["registration_complete"] is True
    assert answer["targets"]["worker"]["attempts"] == 0


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_fresh_host_installs_the_existing_task_and_repeat_does_not_reenable(
    fixture: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
) -> None:
    root = tmp_path / "fresh"
    original = recovery.docker

    def docker(current: dict[str, Any], *args: str) -> bytes:
        if args[:2] == ("image", "inspect"):
            return json.dumps(
                [
                    {
                        "Id": IMAGE,
                        "Config": {"Labels": {"io.aelev.recovery.registration": "1"}},
                    }
                ]
            ).encode()
        return original(current, *args)

    monkeypatch.setattr(recovery, "docker", docker)
    monkeypatch.setattr(host.platform, "system", lambda: system)
    install = Mock()
    status = Mock(return_value={"active": True, "code": "RECOVERY_TIMER_ACTIVE"})
    monkeypatch.setattr(recovery, "install", install)
    monkeypatch.setattr(recovery, "scheduler_status", status)
    raw = json.dumps(fixture["request"]).encode()
    result = host.register(root, IMAGE, "unix:///local.sock", raw)
    assert result["timer"]["active"] is True
    assert result["targets"]["worker"]["automatic_recovery"] is True
    install.assert_called_once_with(root)
    before = (root / "state.json").read_bytes()
    status.return_value = {"active": False, "code": "RECOVERY_TIMER_INACTIVE"}
    assert (
        host.register(root, IMAGE, "unix:///local.sock", raw)["timer"]["active"]
        is False
    )
    install.assert_called_once()
    assert (root / "state.json").read_bytes() == before


def test_batch_reuse_cannot_create_a_second_budget(fixture: dict[str, Any]) -> None:
    value = copy.deepcopy(fixture["request"])
    value["targets"].append({**value["targets"][0], "name": "alias"})
    with pytest.raises(ValueError, match="DUPLICATE_TARGET"):
        cli.request(json.dumps(value).encode())
