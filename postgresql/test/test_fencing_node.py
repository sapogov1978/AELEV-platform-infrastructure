"""Target-owned fencing history survives coordinator replicas and lost ACKs."""

from __future__ import annotations

import copy
import json
import threading
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from aelev_postgresql import fencing, fencing_intent, fencing_node, fencing_return
from aelev_postgresql.configuration import PREFIX


@pytest.fixture
def node(monkeypatch: Any, tmp_path: Path) -> Any:
    root = tmp_path / "node-ledger"
    monkeypatch.setattr(fencing_intent, "ROOT", root)
    for key, value in {
        "CLUSTER": "fencing-test",
        "FENCING_DOCKER_LOCAL_NODES": "postgresql-2",
        "FENCING_DOCKER_DAEMON_ID": "owned-daemon-id",
        "FENCING_TIMEOUT_SECONDS": "20",
    }.items():
        monkeypatch.setenv(PREFIX + key, value)
    claim = fencing.Claim("postgresql-1", "7", "8")
    monkeypatch.setattr(fencing.Claim, "current", classmethod(lambda cls: claim))
    monkeypatch.setattr(fencing_return, "confirmed_claim", lambda: claim)
    state = {
        "Id": "a" * 64,
        "Config": {
            "Labels": {
                fencing.CLUSTER_LABEL: "fencing-test",
                fencing.NODE_LABEL: "postgresql-2",
            }
        },
        "State": {"Running": True, "Restarting": False, "Pid": 17, "StartedAt": "old"},
    }
    calls: list[tuple[str, str]] = []
    failures = {"start": False, "stop": False}

    def call(self: Any, method: str, path: str) -> Any:
        calls.append((method, path))
        if path == "/info":
            return {"ID": "owned-daemon-id"}
        if path.startswith("/containers/json?"):
            return [{"Id": state["Id"]}]
        if path.endswith("/json"):
            return copy.deepcopy(state)
        if "/stop?" in path:
            state["State"].update(Running=False, Pid=0)
            if failures["stop"]:
                raise OSError("Stop ACK lost")
        elif path.endswith("/start"):
            if failures["start"]:
                raise OSError("Start outcome unknown")
            state["State"].update(Running=True, Pid=29, StartedAt="new")
        else:
            raise AssertionError("Unexpected Docker operation")

    monkeypatch.setattr(fencing.Docker, "call", call)

    def request(action: str = "fence") -> dict[str, Any]:
        result = {
            **fencing_node.binding(),
            "action": action,
            "request_id": str(uuid.uuid4()),
        }
        if action == "fence":
            result["claim"] = asdict(claim)
        return result

    return {
        "root": root,
        "backend": fencing.Docker(),
        "request": request,
        "state": state,
        "calls": calls,
        "failures": failures,
        "claim": claim,
    }


def run(node: Any, action: str = "fence") -> dict[str, Any]:
    return fencing_node.execute(node["request"](action), fencing.Docker())


def test_no_implicit_empty_history(node: Any) -> None:
    with pytest.raises(fencing.FencingError, match="not admitted"):
        run(node)
    assert not node["calls"]


def test_initialize_repeat_preserves_binding(node: Any) -> None:
    fencing_node.initialize(node["backend"])
    before = (node["root"] / "node-binding.json").read_bytes()
    fencing_node.initialize(fencing.Docker())
    assert (node["root"] / "node-binding.json").read_bytes() == before


def test_existing_history_requires_handoff(node: Any) -> None:
    node["root"].mkdir(mode=0o700)
    historical = node["root"] / "docker-intent-postgresql-2.json"
    historical.write_text('{"start_attempts":3}')
    with pytest.raises(fencing.FencingError, match="handoff"):
        fencing_node.initialize(fencing.Docker())
    assert historical.read_text() == '{"start_attempts":3}'
    assert not (node["root"] / "node-binding.json").exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("node", "postgresql-3"),
        ("cluster", "foreign"),
        ("daemon_id", "foreign-daemon"),
        ("schema", True),
        ("command", "docker rm"),
        ("request_id", "bad"),
        ("action", "initialize"),
    ],
)
def test_request_cannot_change_target_or_execute_commands(
    node: Any, key: str, value: Any
) -> None:
    fencing_node.initialize(fencing.Docker())
    before = list(node["calls"])
    request = node["request"]()
    request[key] = value
    with pytest.raises(ValueError):
        fencing_node.execute(request, fencing.Docker())
    assert node["calls"] == before


def test_daemon_change_blocks_before_stop(node: Any, monkeypatch: Any) -> None:
    fencing_node.initialize(fencing.Docker())
    monkeypatch.setenv(PREFIX + "FENCING_DOCKER_DAEMON_ID", "different-daemon")
    with pytest.raises(fencing.FencingError, match="not admitted"):
        run(node)
    assert not any(method == "POST" for method, _ in node["calls"])


def test_lost_stop_ack_and_second_coordinator_use_same_intent(node: Any) -> None:
    fencing_node.initialize(fencing.Docker())
    node["failures"]["stop"] = True
    first = run(node)
    second = run(node)
    assert first["identities"] == second["identities"] == ["a" * 64]
    assert first["intent"] == second["intent"]
    assert second["intent"]["phase"] == "STOP_CONFIRMED"
    assert second["intent"]["start_attempts"] == 0
    assert sum("/stop?" in path for _, path in node["calls"]) == 1


def test_manual_stop_is_not_returned_by_other_coordinator(node: Any) -> None:
    fencing_node.initialize(fencing.Docker())
    node["state"]["State"].update(Running=False, Pid=0)
    run(node)
    for _ in range(4):
        assert run(node, "return-once")["dispatched"] is False
    assert not any(path.endswith("/start") for _, path in node["calls"])


def test_fresh_coordinators_cannot_reset_three_attempts(
    node: Any, monkeypatch: Any
) -> None:
    fencing_node.initialize(fencing.Docker())
    run(node)
    node["failures"]["start"] = True
    now = [1000.0]
    monkeypatch.setattr(fencing_return.time, "time", lambda: now[0])
    for attempt in range(1, 4):
        response = run(node, "return-once")
        assert response["dispatched"] is True
        assert response["intent"]["start_attempts"] == attempt
        now[0] += 301
    # Each call constructs a fresh Returns instance, as different fencers do.
    for _ in range(4):
        response = run(node, "return-once")
        assert response["dispatched"] is False
        assert response["intent"]["start_attempts"] == 3
        assert response["intent"]["last_return_code"] == "RETURN_ATTEMPTS_EXHAUSTED"
    assert sum(path.endswith("/start") for _, path in node["calls"]) == 3


def test_dcs_uncertainty_preserves_budget(node: Any, monkeypatch: Any) -> None:
    fencing_node.initialize(fencing.Docker())
    run(node)

    def unavailable() -> Any:
        raise fencing.FencingError("Unavailable", "DCS_UNAVAILABLE")

    monkeypatch.setattr(fencing_return, "confirmed_claim", unavailable)
    response = run(node, "return-once")
    assert response["intent"]["start_attempts"] == 0
    assert response["intent"]["last_return_code"] == "DCS_UNAVAILABLE"
    assert response["dispatched"] is False


def test_changed_native_lease_blocks_stop(node: Any, monkeypatch: Any) -> None:
    fencing_node.initialize(fencing.Docker())
    monkeypatch.setattr(
        fencing.Claim,
        "current",
        classmethod(lambda cls: fencing.Claim("postgresql-3", "11", "12")),
    )
    with pytest.raises(fencing.FencingError, match="lease"):
        run(node)
    assert not any(method == "POST" for method, _ in node["calls"])


def test_concurrent_operation_does_not_compete(node: Any) -> None:
    fencing_node.initialize(fencing.Docker())
    errors: list[Exception] = []

    def contender() -> None:
        try:
            run(node)
        except Exception as error:
            errors.append(error)

    with fencing_node.exclusive():
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], BlockingIOError)
    assert not any(method == "POST" for method, _ in node["calls"])


def test_successful_return_retains_audit_and_attempt(node: Any) -> None:
    fencing_node.initialize(fencing.Docker())
    run(node)
    response = run(node, "return-once")
    assert response["intent"]["phase"] == "COMPLETED"
    assert response["intent"]["start_attempts"] == 1
    audit = [
        json.loads(line)
        for line in (node["root"] / "node-audit.jsonl").read_text().splitlines()
    ]
    assert {row["phase"] for row in audit} >= {
        "intent",
        "confirmed",
        "return_requested",
        "returned",
    }
