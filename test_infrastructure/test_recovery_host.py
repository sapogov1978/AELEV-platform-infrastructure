"""Interrupted host windows never resume a timer beside an unknown writer."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery
import recovery_host as host

IMAGE = "sha256:" + "f" * 64


def test_native_status_cli_has_no_timer_or_maintenance_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    value: dict[str, Any] = {
        "schema_version": 1,
        "daemon_id": "daemon",
        "action": "status",
        "targets": [
            {
                "name": "worker",
                "expected": {
                    "id": "a" * 64,
                    "image": IMAGE,
                    "project": "project",
                    "service": "worker",
                    "config_hash": "b" * 64,
                },
            }
        ],
    }
    current = {
        "schema_version": 1,
        "daemon": "daemon",
        "targets": {
            "worker": {
                "expected": value["targets"][0]["expected"],
                "attempts": 3,
                "native_count": 0,
                "policy_limit": 0,
                "blocked": True,
                "code": "CIRCUIT_OPEN",
            }
        },
    }
    monkeypatch.setattr(host, "state", lambda _: current)
    monkeypatch.setattr(recovery, "docker", lambda *a: b"daemon")
    for name in ("maintain", "timer", "save", "apply", "switch"):
        monkeypatch.setattr(
            host, name, Mock(side_effect=AssertionError("status must not write"))
        )
    monkeypatch.setattr(
        sys, "argv", ["recovery-host", "--state", str(tmp_path), "--image", IMAGE]
    )
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(value).encode()))
    )
    before = list(tmp_path.iterdir())
    host.main()
    answer = json.loads(capsys.readouterr().out)
    assert answer["targets"]["worker"]["attempts"] == 3
    assert list(tmp_path.iterdir()) == before


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    tmp_path.chmod(0o700)
    recovery.commit(
        tmp_path,
        {
            "schema_version": 1,
            "daemon": "daemon",
            "docker": "/host/docker",
            "endpoint": "unix:///host/socket",
            "targets": {},
        },
    )
    plist = tmp_path / "timer.plist"
    plist.write_bytes(b"fixed timer")
    monkeypatch.setattr(
        host,
        "timer",
        lambda _: (
            "gui/501/exact",
            plist,
            hashlib.sha256(plist.read_bytes()).hexdigest(),
        ),
    )
    loaded = [True]
    calls: list[Any] = []
    container: dict[str, Any] = {}
    value: dict[str, Any] = {
        "schema_version": 1,
        "daemon_id": "daemon",
        "action": "suspend",
        "targets": [
            {
                "name": "worker",
                "expected": {
                    "id": "a" * 64,
                    "image": IMAGE,
                    "project": "project",
                    "service": "worker",
                    "config_hash": "b" * 64,
                },
            }
        ],
    }
    monkeypatch.setattr(host, "loaded", lambda _: loaded[0])

    def execute(arguments: list[str]) -> bytes:
        calls.append(arguments)
        assert arguments[0] == "launchctl"
        loaded[0] = arguments[1] == "bootstrap"
        return b""

    def docker(current: dict[str, Any], *arguments: str) -> bytes:
        calls.append(arguments)
        if arguments[0] == "info":
            return b"daemon"
        if arguments[0] == "run":
            return b"0\n"
        if arguments[:2] == ("image", "inspect"):
            return json.dumps(
                [
                    {
                        "Id": IMAGE,
                        "Config": {"Labels": {"io.aelev.recovery.operator": "1"}},
                    }
                ]
            ).encode()
        if arguments[0] == "ps":
            return b"a" * 64 if container else b""
        if arguments[0] == "inspect":
            return json.dumps([container]).encode()
        if arguments[0] == "create":
            label = arguments[arguments.index("--label") + 1].partition("=")[2]
            container.update(
                Id="a" * 64,
                Image=IMAGE,
                Config={"Labels": {"io.aelev.recovery.request": label}},
                State={"Status": "created", "Running": False},
            )
            return b"a" * 64
        if arguments[0] == "logs":
            return json.dumps(
                {
                    "schema_version": 1,
                    "daemon_id": "daemon",
                    "targets": {
                        "worker": {"attempts": 2, "target_digest": "d" * 64}
                    },
                }
            ).encode()
        raise AssertionError(arguments)

    def start(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((args, kwargs))
        assert not loaded[0]
        container["State"].update(Status="exited", Running=False, ExitCode=0)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(recovery, "docker", docker)
    monkeypatch.setattr(recovery, "execute", execute)
    monkeypatch.setattr(subprocess, "run", start)
    return {
        "root": tmp_path,
        "request": json.dumps(value).encode(),
        "loaded": loaded,
        "calls": calls,
        "container": container,
    }


def test_timer_round_trip_and_exact_retry_without_dispatch(
    fixture: dict[str, Any],
) -> None:
    first = host.maintain(fixture["root"], IMAGE, fixture["request"])
    assert first["targets"]["worker"]["attempts"] == 2
    assert fixture["loaded"][0]
    before = len(fixture["calls"])
    assert host.maintain(fixture["root"], IMAGE, fixture["request"]) == first
    assert not any(
        isinstance(c, tuple) and c and isinstance(c[0], tuple)
        for c in fixture["calls"][before:]
    )


def test_failed_relocation_repair_preserves_block_and_restores_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    old_image = "sha256:" + "e" * 64
    old_expected = {
        "id": "a" * 64,
        "image": old_image,
        "project": "old-project",
        "service": "worker",
        "config_hash": "b" * 64,
    }
    replacement_name = "new-project-worker-1"
    replacement = {
        "id": "c" * 64,
        "image": "sha256:" + "d" * 64,
        "project": "new-project",
        "service": "worker",
        "config_hash": "f" * 64,
    }
    recovery.commit(
        tmp_path,
        {
            "schema_version": 1,
            "daemon": "daemon",
            "docker": "/host/docker",
            "endpoint": "unix:///host/socket",
            "targets": {
                "old-project-worker-1": {
                    "expected": old_expected,
                    "attempts": 11,
                    "native_count": 0,
                    "policy_limit": 0,
                    "blocked": True,
                    "code": "AUTOMATIC_EXIT_UNCONFIRMED",
                    "bad_since": None,
                    "guard": None,
                    "automatic": {"version": 1, "phase": "ACTIVE"},
                }
            },
        },
    )
    (tmp_path / ".lock").touch(mode=0o600)
    (tmp_path / ".container-operation.lock").touch(mode=0o600)
    value = {
        "schema_version": 1,
        "daemon_id": "daemon",
        "action": "relocate",
        "targets": [
            {
                "name": "old-project-worker-1",
                "expected": old_expected,
                "previous_native_count": 0,
                "replacement_name": replacement_name,
                "replacement": replacement,
            }
        ],
    }
    raw = json.dumps(value, sort_keys=True).encode()
    request_digest = hashlib.sha256(raw + old_image.encode()).hexdigest()
    helper_name = "aelev-recovery-operator-" + request_digest[:24]
    timer_path = tmp_path / "timer.plist"
    timer_path.write_bytes(b"fixed")
    timer_digest = hashlib.sha256(timer_path.read_bytes()).hexdigest()
    marker = {
        "schema_version": 1,
        "request_digest": request_digest,
        "image": old_image,
        "daemon_id": "daemon",
        "timer": "gui/501/exact",
        "timer_digest": timer_digest,
        "container_name": helper_name,
        "container_id": "9" * 64,
        "phase": "DISPATCH_INTENT",
    }
    host.save(tmp_path, marker)
    replacement_row = {
        "Id": replacement["id"],
        "Image": replacement["image"],
        "Name": "/" + replacement_name,
        "RestartCount": 0,
        "Config": {
            "Labels": {
                "com.docker.compose.project": replacement["project"],
                "com.docker.compose.service": replacement["service"],
                "com.docker.compose.config-hash": replacement["config_hash"],
            },
            "Healthcheck": {"Test": ["CMD", "true"], "Interval": 1_000_000_000},
        },
        "State": {
            "Running": True,
            "Paused": False,
            "Restarting": False,
            "StartedAt": "2999-01-01T00:00:00Z",
            "Health": {"Status": "healthy", "Log": []},
        },
        "HostConfig": {"RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}},
    }
    helper_row = {
        "Id": marker["container_id"],
        "Image": old_image,
        "State": {
            "Status": "exited",
            "Running": False,
            "ExitCode": 1,
            "OOMKilled": False,
        },
    }
    loaded = [False]
    monkeypatch.setattr(
        host,
        "timer",
        lambda _: ("gui/501/exact", timer_path, timer_digest),
    )
    monkeypatch.setattr(host, "loaded", lambda _: loaded[0])
    monkeypatch.setattr(
        host, "switch", lambda _d, _p, *, enabled: loaded.__setitem__(0, enabled)
    )
    monkeypatch.setattr(
        host,
        "inspect_container",
        lambda _current, name: helper_row if name == helper_name else None,
    )
    monkeypatch.setattr(
        host,
        "operator_failure",
        lambda _current, _identity: {"code": "RELOCATION_UNCONFIRMED"},
    )

    def docker(_current: dict[str, Any], *arguments: str) -> bytes:
        if arguments[0] == "info":
            return b"daemon"
        if arguments[:2] == ("image", "inspect"):
            return json.dumps(
                [
                    {
                        "Id": IMAGE,
                        "Config": {"Labels": {"io.aelev.recovery.operator": "1"}},
                    }
                ]
            ).encode()
        if arguments[0] == "inspect":
            return json.dumps([replacement_row]).encode()
        if arguments[0] == "ps":
            return b""
        if arguments[0] == "update":
            return b""
        raise AssertionError(arguments)

    monkeypatch.setattr(recovery, "docker", docker)
    answer = host.repair_failed_relocation(tmp_path, IMAGE, raw)
    assert set(answer["targets"]) == {replacement_name}
    assert answer["targets"][replacement_name]["blocked"] is True
    assert loaded[0] is True
    completed = json.loads((tmp_path / "container-maintenance.json").read_bytes())
    assert completed["phase"] == "COMPLETE"
    assert completed["repair"]["phase"] == "COMPLETE"
    target = json.loads((tmp_path / "state.json").read_bytes())["targets"][
        replacement_name
    ]
    assert target["code"] == "AUTOMATIC_EXIT_UNCONFIRMED"
    assert target["relocation"]["preserve_block"] is True


def test_completed_window_survives_one_shot_helper_cleanup(
    fixture: dict[str, Any],
) -> None:
    first = host.maintain(fixture["root"], IMAGE, fixture["request"])
    assert first["targets"]["worker"]["attempts"] == 2
    fixture["container"].clear()
    other = json.loads(fixture["request"])
    other["targets"][0]["expected"]["config_hash"] = "c" * 64
    answer = host.maintain(fixture["root"], IMAGE, json.dumps(other).encode())
    assert answer["targets"]["worker"]["attempts"] == 2
    history = fixture["root"] / "container-maintenance-history"
    assert len(list(history.glob("*.json"))) == 1


def test_missing_helper_does_not_bypass_invalid_completed_receipt(
    fixture: dict[str, Any],
) -> None:
    host.maintain(fixture["root"], IMAGE, fixture["request"])
    fixture["container"].clear()
    marker_path = fixture["root"] / "container-maintenance.json"
    marker = json.loads(marker_path.read_bytes())
    marker["result"]["daemon_id"] = "other"
    marker_path.write_text(json.dumps(marker))
    other = json.loads(fixture["request"])
    other["targets"][0]["expected"]["config_hash"] = "c" * 64
    with pytest.raises(ValueError, match="PREVIOUS_OPERATOR_UNCONFIRMED"):
        host.maintain(fixture["root"], IMAGE, json.dumps(other).encode())
    assert (
        json.loads((fixture["root"] / "container-maintenance.json").read_text())[
            "phase"
        ]
        == "COMPLETE"
    )


def test_async_timer_stop_is_observed_before_container_dispatch(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = host.switch
    pending = [0]
    sleeps: list[float] = []

    def switch(domain: str, path: Path, *, enabled: bool) -> None:
        original(domain, path, enabled=enabled)
        if not enabled:
            pending[0] = 3

    def loaded(_domain: str) -> bool:
        if pending[0]:
            pending[0] -= 1
            return True
        return bool(fixture["loaded"][0])

    monkeypatch.setattr(host, "switch", switch)
    monkeypatch.setattr(host, "loaded", loaded)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    answer = host.maintain(fixture["root"], IMAGE, fixture["request"])
    assert answer["targets"]["worker"]["attempts"] == 2
    assert sleeps == [0.05] * 3
    assert fixture["loaded"][0]
    assert (
        sum(c == ["launchctl", "bootout", "gui/501/exact"] for c in fixture["calls"])
        == 1
    )


def test_timer_stop_deadline_retains_intent_and_exact_retry(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(host, "loaded", lambda _domain: True)
    with pytest.raises(ValueError, match="RECOVERY_TIMER_STOP_UNCONFIRMED"):
        host.maintain(fixture["root"], IMAGE, fixture["request"])
    path = fixture["root"] / "container-maintenance.json"
    before = json.loads(path.read_bytes())
    assert before["phase"] == "INTENT" and before["container_id"] is None
    assert not fixture["container"] and not fixture["loaded"][0]
    monkeypatch.setattr(host, "loaded", lambda _domain: fixture["loaded"][0])
    answer = host.maintain(fixture["root"], IMAGE, fixture["request"])
    after = json.loads(path.read_bytes())
    assert after["phase"] == "COMPLETE"
    assert after["request_digest"] == before["request_digest"]
    assert answer["targets"]["worker"]["attempts"] == 2
    assert fixture["loaded"][0]


def test_lost_start_ack_does_not_restore_timer_or_duplicate(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: Any, **kwargs: Any) -> None:
        fixture["container"]["State"].update(Status="running", Running=True)
        raise subprocess.TimeoutExpired("start", 120)

    start = Mock(side_effect=interrupted)
    monkeypatch.setattr(subprocess, "run", start)
    with pytest.raises(subprocess.TimeoutExpired):
        host.maintain(fixture["root"], IMAGE, fixture["request"])
    assert not fixture["loaded"][0]
    with pytest.raises(ValueError, match="RESULT_UNCONFIRMED"):
        host.maintain(fixture["root"], IMAGE, fixture["request"])
    assert start.call_count == 1 and not fixture["loaded"][0]
    fixture["container"]["State"].update(Status="exited", Running=False, ExitCode=0)
    host.maintain(fixture["root"], IMAGE, fixture["request"])
    assert start.call_count == 1 and fixture["loaded"][0]


def test_failed_operator_preserves_disabled_timer(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        fixture["container"]["State"].update(Status="exited", Running=False, ExitCode=1)
        return subprocess.CompletedProcess(args, 1, b"", b"safe error")

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(ValueError, match="RESULT_UNCONFIRMED"):
        host.maintain(fixture["root"], IMAGE, fixture["request"])
    assert not fixture["loaded"][0]


def test_foreign_request_cannot_replace_unfinished_window(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(side_effect=subprocess.TimeoutExpired("start", 120)),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        host.maintain(fixture["root"], IMAGE, fixture["request"])
    other = json.loads(fixture["request"])
    other["targets"][0]["name"] = "foreign"
    with pytest.raises(ValueError, match="PREVIOUS_MAINTENANCE_REQUIRES_REVIEW"):
        host.maintain(fixture["root"], IMAGE, json.dumps(other).encode())
    assert not fixture["loaded"][0]
