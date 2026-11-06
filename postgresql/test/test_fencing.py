"""Adversarial fencing checks; mocks do not claim a live Kubernetes exercise."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from aelev_postgresql import fencing, fencing_intent, fencing_return
from aelev_postgresql.__main__ import PREFIX


@pytest.fixture
def configured(monkeypatch: Any, tmp_path: Path) -> fencing.Claim:
    monkeypatch.setattr(fencing_intent, "ROOT", tmp_path / "fencing")
    monkeypatch.setenv(PREFIX + "CLUSTER", "unit-cluster")
    monkeypatch.setenv(PREFIX + "FENCING_MODE", "docker")
    monkeypatch.setenv(PREFIX + "FENCING_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv(
        PREFIX + "FENCING_DOCKER_LOCAL_NODES", "postgresql-2,postgresql-3"
    )
    claim = fencing.Claim("postgresql-1", "7", "8")
    monkeypatch.setattr(fencing.Claim, "current", classmethod(lambda cls: claim))
    return claim


@pytest.mark.parametrize("value", ["", "off", "automatic", "unknown"])
def test_no_unprotected_fallback(monkeypatch: Any, value: str) -> None:
    monkeypatch.setenv(PREFIX + "FENCING_MODE", value)
    with pytest.raises(ValueError):
        fencing.mode()


@pytest.mark.parametrize("value", ["docker", "kubernetes", "watchdog"])
def test_explicit_modes(monkeypatch: Any, value: str) -> None:
    monkeypatch.setenv(PREFIX + "FENCING_MODE", value)
    assert fencing.mode() == value


def test_kubernetes_rejects_docker_runtime(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv(PREFIX + "FENCING_MODE", "kubernetes")
    monkeypatch.setenv(PREFIX + "NODE_ID", "1")
    monkeypatch.setattr(fencing, "KUBERNETES_NAMESPACE", tmp_path / "missing")
    with pytest.raises(fencing.FencingError, match="native Pod"):
        fencing.validate_runtime()


def test_kubernetes_requires_exact_pod_and_namespace(
    monkeypatch: Any, tmp_path: Path
) -> None:
    path = tmp_path / "namespace"
    path.write_text("owned")
    monkeypatch.setenv(PREFIX + "FENCING_MODE", "kubernetes")
    monkeypatch.setenv(PREFIX + "NODE_ID", "1")
    monkeypatch.setenv(PREFIX + "FENCING_KUBERNETES_NAMESPACE", "owned")
    monkeypatch.setenv(PREFIX + "NODE_1_KUBERNETES_POD", "postgresql-1-0")
    monkeypatch.setenv("HOSTNAME", "postgresql-1-0")
    monkeypatch.setattr(fencing, "KUBERNETES_NAMESPACE", path)
    fencing.validate_runtime()
    monkeypatch.setenv("HOSTNAME", "different")
    with pytest.raises(fencing.FencingError):
        fencing.validate_runtime()


def docker_calls(
    monkeypatch: Any,
    *,
    running: bool = True,
    stopped: bool = True,
    labels: bool = True,
    duplicate: bool = False,
) -> list[str]:
    calls: list[str] = []
    identifier = "a" * 64

    def call(self: Any, method: str, path: str) -> Any:
        calls.append(method + " " + path)
        if "/containers/json?" in path:
            return [{"Id": identifier}] * (2 if duplicate else 1)
        if path.endswith("/json"):
            after = any("/stop?" in item for item in calls)
            resumed = any(item.endswith("/start") for item in calls)
            return {
                "Id": identifier,
                "Config": {
                    "Labels": {
                        fencing.CLUSTER_LABEL: "unit-cluster" if labels else "foreign",
                        fencing.NODE_LABEL: "postgresql-2",
                    }
                },
                "State": {
                    "Running": resumed or (running and not (after and stopped)),
                    "Pid": (
                        18
                        if resumed
                        else 0 if not running or (after and stopped) else 17
                    ),
                    "Restarting": False,
                    "StartedAt": "new-process" if resumed else "old-process",
                },
            }
        return None

    monkeypatch.setattr(fencing.Docker, "call", call)
    return calls


def return_once(claim: fencing.Claim, monkeypatch: Any) -> None:
    """Run the existing guarded return worker, not a promotion RPC."""
    monkeypatch.setattr(fencing_return, "confirmed_claim", lambda: claim)
    worker = fencing_return.Returns(
        fencing.Docker(),
        {node: threading.Lock() for node in fencing.NODES},
        lambda *args: None,
    )
    worker.once()


def test_docker_confirms_exit_and_defers_return(
    configured: fencing.Claim, monkeypatch: Any
) -> None:
    calls = docker_calls(monkeypatch)
    records: list[str] = []
    assert fencing.Docker().fence(
        "postgresql-2", configured, lambda phase, ids: records.append(phase)
    ) == ["a" * 64]
    assert records == ["intent", "terminated"]
    assert not any(c.endswith("/start") for c in calls)
    assert (
        fencing_intent.IntentStore("postgresql-2").load()["phase"] == "STOP_CONFIRMED"
    )
    return_once(configured, monkeypatch)
    assert calls.index("POST /containers/" + "a" * 64 + "/stop?t=5") < calls.index(
        "POST /containers/" + "a" * 64 + "/start"
    )


def test_docker_preserves_operator_stop(
    configured: fencing.Claim, monkeypatch: Any
) -> None:
    calls = docker_calls(monkeypatch, running=False)
    fencing.Docker().fence("postgresql-2", configured, lambda *args: None)
    assert not any(item.startswith("POST") for item in calls)


@pytest.mark.parametrize(
    "parameters", [{"labels": False}, {"duplicate": True}, {"stopped": False}]
)
def test_docker_uncertainty_never_restarts(
    configured: fencing.Claim, monkeypatch: Any, parameters: dict[str, bool]
) -> None:
    calls = docker_calls(monkeypatch, **parameters)
    with pytest.raises(fencing.FencingError):
        fencing.Docker().fence("postgresql-2", configured, lambda *args: None)
    assert not any(item.endswith("/start") for item in calls)


def test_docker_rejects_other_host(configured: fencing.Claim) -> None:
    with pytest.raises(fencing.FencingError, match="allowlist"):
        fencing.Docker().fence("postgresql-1", configured, lambda *args: None)


def test_changed_lease_prevents_action(
    configured: fencing.Claim, monkeypatch: Any
) -> None:
    calls = docker_calls(monkeypatch)
    monkeypatch.setattr(
        fencing.Claim,
        "current",
        classmethod(lambda cls: fencing.Claim("postgresql-1", "new", "new")),
    )
    with pytest.raises(fencing.FencingError, match="changed"):
        fencing.Docker().fence("postgresql-2", configured, lambda *args: None)
    assert not any(item.startswith("POST") for item in calls)


def test_interrupted_stop_retains_return_intent_across_executor_restart(
    configured: fencing.Claim,
    monkeypatch: Any,
) -> None:
    calls = docker_calls(monkeypatch)
    original = fencing.Docker.call
    fail = True

    def uncertain(self: Any, method: str, path: str) -> Any:
        nonlocal fail
        result = original(self, method, path)
        if fail and "/stop?" in path:
            raise TimeoutError("stop response lost")
        if fail and path.endswith("/json") and any("/stop?" in call for call in calls):
            fail = False
            raise TimeoutError("inspection unavailable")
        return result

    monkeypatch.setattr(fencing.Docker, "call", uncertain)
    with pytest.raises(TimeoutError):
        fencing.Docker().fence("postgresql-2", configured, lambda *args: None)
    pending = fencing_intent.IntentStore("postgresql-2").load()
    assert pending is not None and pending["restart_required"] is True
    assert not any(call.endswith("/start") for call in calls)
    # A fresh executor must recover the durable intent, not infer a manual stop.
    fencing.Docker().fence("postgresql-2", configured, lambda *args: None)
    assert not any(call.endswith("/start") for call in calls)
    return_once(configured, monkeypatch)
    assert sum(call.endswith("/start") for call in calls) == 1
    assert fencing_intent.IntentStore("postgresql-2").load()["phase"] == "COMPLETED"


def test_stop_timeout_is_reconciled_before_start(
    configured: fencing.Claim,
    monkeypatch: Any,
) -> None:
    calls = docker_calls(monkeypatch)
    original = fencing.Docker.call

    def lost_response(self: Any, method: str, path: str) -> Any:
        result = original(self, method, path)
        if "/stop?" in path:
            raise TimeoutError("response lost after Docker stopped the container")
        return result

    monkeypatch.setattr(fencing.Docker, "call", lost_response)
    fencing.Docker().fence("postgresql-2", configured, lambda *args: None)
    return_once(configured, monkeypatch)
    assert sum(call.endswith("/start") for call in calls) == 1


def test_lost_start_response_does_not_repeat_start(
    configured: fencing.Claim,
    monkeypatch: Any,
) -> None:
    calls = docker_calls(monkeypatch)
    original = fencing.Docker.call

    def lost_response(self: Any, method: str, path: str) -> Any:
        result = original(self, method, path)
        if path.endswith("/start"):
            raise TimeoutError("start response lost")
        return result

    monkeypatch.setattr(fencing.Docker, "call", lost_response)
    fencing.Docker().fence("postgresql-2", configured, lambda *args: None)
    return_once(configured, monkeypatch)
    assert sum(call.endswith("/start") for call in calls) == 1


def test_changed_lease_after_termination_preserves_intent_but_never_starts(
    configured: fencing.Claim,
    monkeypatch: Any,
) -> None:
    calls = docker_calls(monkeypatch)

    def lease(cls: Any) -> fencing.Claim:
        return (
            fencing.Claim("postgresql-1", "new", "new")
            if any("/stop?" in c for c in calls)
            else configured
        )

    monkeypatch.setattr(fencing.Claim, "current", classmethod(lease))
    with pytest.raises(fencing.FencingError, match="changed"):
        fencing.Docker().fence("postgresql-2", configured, lambda *args: None)
    assert not any(c.endswith("/start") for c in calls)
    assert fencing_intent.IntentStore("postgresql-2").load()["restart_required"] is True


@pytest.mark.parametrize("change_lease", [False, True])
def test_return_attempts_are_bounded_and_not_reset_on_retry(
    configured: fencing.Claim,
    monkeypatch: Any,
    change_lease: bool,
) -> None:
    calls = docker_calls(monkeypatch)
    original = fencing.Docker.call

    def never_started(self: Any, method: str, path: str) -> Any:
        if path.endswith("/start"):
            calls.append(method + " " + path + " failed")
            raise TimeoutError("Docker start unavailable")
        return original(self, method, path)

    monkeypatch.setattr(fencing.Docker, "call", never_started)
    for number in range(5):
        monkeypatch.setattr(fencing.time, "time", lambda: 1000.0 + number * 400)
        if change_lease:
            configured = fencing.Claim(
                "postgresql-1", str(10 + number), str(20 + number)
            )
            monkeypatch.setattr(
                fencing.Claim, "current", classmethod(lambda cls: configured)
            )
        fencing.Docker().fence("postgresql-2", configured, lambda *args: None)
        return_once(configured, monkeypatch)
    assert sum("/start failed" in c for c in calls) == 3
    assert fencing_intent.IntentStore("postgresql-2").load()["start_attempts"] == 3


def test_promotion_rpc_cannot_bypass_autonomous_return_cooldown(
    configured: fencing.Claim, monkeypatch: Any
) -> None:
    calls = docker_calls(monkeypatch, running=False)
    store = fencing_intent.IntentStore("postgresql-2")
    with store.locked():
        store.save(
            {
                "version": 1,
                "node": "postgresql-2",
                "cluster": "unit-cluster",
                "container_id": "a" * 64,
                "started_at": "old-process",
                "restart_required": True,
                "start_attempts": 1,
                "phase": "START_REQUESTED",
                "next_attempt_at": 1030.0,
                "claim": {"candidate": "postgresql-1", "lease": "7", "revision": "8"},
            }
        )
    monkeypatch.setattr(fencing.time, "time", lambda: 1000.0)
    fencing.Docker().fence("postgresql-2", configured, lambda *args: None)
    return_once(configured, monkeypatch)
    assert not any(call.startswith("POST") for call in calls)
    assert store.load()["start_attempts"] == 1


def test_completed_start_is_reconciled_after_state_commit_failure(
    configured: fencing.Claim,
    monkeypatch: Any,
) -> None:
    calls = docker_calls(monkeypatch)
    original = fencing_intent.IntentStore.save
    fail = True

    def save(self: Any, value: dict[str, Any]) -> None:
        nonlocal fail
        if fail and value["phase"] == "COMPLETED":
            fail = False
            raise OSError("state commit unavailable")
        original(self, value)

    monkeypatch.setattr(fencing_intent.IntentStore, "save", save)
    fencing.Docker().fence("postgresql-2", configured, lambda *args: None)
    return_once(configured, monkeypatch)
    assert (
        fencing_intent.IntentStore("postgresql-2").load()["phase"] == "START_REQUESTED"
    )
    records: list[str] = []
    fencing.Docker().fence(
        "postgresql-2", configured, lambda phase, ids: records.append(phase)
    )
    assert records == ["restart_reconciled"]
    assert sum("/stop?" in c for c in calls) == 1
    assert sum(c.endswith("/start") for c in calls) == 1


def test_pending_return_cannot_apply_to_replaced_container(
    configured: fencing.Claim,
    monkeypatch: Any,
) -> None:
    calls = docker_calls(monkeypatch, running=False)
    store = fencing_intent.IntentStore("postgresql-2")
    with store.locked():
        store.save(
            {
                "version": 1,
                "node": "postgresql-2",
                "cluster": "unit-cluster",
                "container_id": "b" * 64,
                "started_at": "old-process",
                "restart_required": True,
                "phase": "STOP_REQUESTED",
                "start_attempts": 0,
                "claim": {"candidate": "postgresql-1", "lease": "7", "revision": "8"},
            }
        )
    with pytest.raises(fencing.FencingError, match="identity changed"):
        fencing.Docker().fence("postgresql-2", configured, lambda *args: None)
    assert not any(c.startswith("POST") for c in calls)


def test_old_successful_start_is_refenced_under_new_lease(
    configured: fencing.Claim, monkeypatch: Any
) -> None:
    """A lost completion plus a new leader lease must not wedge all members."""
    store = fencing_intent.IntentStore("postgresql-2")
    with store.locked():
        store.save(
            {
                "version": 1,
                "node": "postgresql-2",
                "cluster": "unit-cluster",
                "container_id": "a" * 64,
                "started_at": "terminated-process",
                "restart_required": True,
                "phase": "START_REQUESTED",
                "start_attempts": 3,
                "claim": {
                    "candidate": "postgresql-3",
                    "lease": "old",
                    "revision": "old",
                },
            }
        )
    calls = docker_calls(monkeypatch)
    original = fencing.Docker.call

    def current_process(self: Any, method: str, path: str) -> Any:
        result = original(self, method, path)
        if path.endswith("/json"):
            result["State"]["StartedAt"] = (
                "current-restarted"
                if any(c.endswith("/start") for c in calls)
                else "current-process"
            )
        return result

    monkeypatch.setattr(fencing.Docker, "call", current_process)
    records: list[str] = []
    fencing.Docker().fence(
        "postgresql-2", configured, lambda phase, ids: records.append(phase)
    )
    assert records == ["restart_reconciled", "intent", "terminated"]
    assert sum("/stop?" in c for c in calls) == 1
    assert not any(c.endswith("/start") for c in calls)
    return_once(configured, monkeypatch)
    assert sum(c.endswith("/start") for c in calls) == 1
    intent = store.load()
    assert intent is not None
    assert intent["claim"] == {
        "candidate": "postgresql-1",
        "lease": "7",
        "revision": "8",
    }
    assert intent["started_at"] == "current-process"
    assert intent["phase"] == "COMPLETED"
    assert intent["start_attempts"] == 1  # New, positively fenced incarnation.


@pytest.mark.parametrize("phase", ["STOP_REQUESTED", "STOP_CONFIRMED"])
def test_unexpected_process_change_is_not_reconciled(
    configured: fencing.Claim, monkeypatch: Any, phase: str
) -> None:
    calls = docker_calls(monkeypatch)
    store = fencing_intent.IntentStore("postgresql-2")
    with store.locked():
        store.save(
            {
                "version": 1,
                "node": "postgresql-2",
                "cluster": "unit-cluster",
                "container_id": "a" * 64,
                "started_at": "unexpected-old-process",
                "restart_required": True,
                "phase": phase,
                "start_attempts": 1,
                "claim": {
                    "candidate": "postgresql-3",
                    "lease": "old",
                    "revision": "old",
                },
            }
        )
    with pytest.raises(fencing.FencingError, match="incarnation changed") as error:
        fencing.Docker().fence("postgresql-2", configured, lambda *args: None)
    assert error.value.code == "PROCESS_INCARNATION_CHANGED"
    assert not any(c.startswith("POST") for c in calls)


def test_explicit_cancellation_preserves_manual_stop(
    configured: fencing.Claim,
    monkeypatch: Any,
) -> None:
    calls = docker_calls(monkeypatch, running=False)
    store = fencing_intent.IntentStore("postgresql-2")
    with store.locked():
        store.save(
            {
                "version": 1,
                "node": "postgresql-2",
                "cluster": "unit-cluster",
                "container_id": "a" * 64,
                "started_at": "old-process",
                "restart_required": True,
                "phase": "STOP_REQUESTED",
                "start_attempts": 0,
                "claim": {"candidate": "postgresql-1", "lease": "7", "revision": "8"},
            }
        )
    monkeypatch.setattr(
        "sys.argv",
        ["fencing_intent", "cancel", "--node", "postgresql-2", "--container", "a" * 64],
    )
    fencing_intent.main()
    fencing.Docker().fence("postgresql-2", configured, lambda *args: None)
    assert not any(c.startswith("POST") for c in calls)
    audit = json.loads((fencing_intent.ROOT / "audit.jsonl").read_text())
    assert audit["phase"] == "return_cancelled"


def test_bad_durable_state_never_authorizes_docker_action(
    configured: fencing.Claim,
    monkeypatch: Any,
) -> None:
    calls = docker_calls(monkeypatch, running=False)
    store = fencing_intent.IntentStore("postgresql-2")
    with store.locked():
        store.save({"container_id": "a" * 64})
    with pytest.raises(ValueError, match="Invalid fencing intent"):
        fencing.Docker().fence("postgresql-2", configured, lambda *args: None)
    assert not any(c.startswith("POST") for c in calls)


def test_executor_persists_safe_failure_code_without_exception_payload(
    configured: fencing.Claim, monkeypatch: Any, tmp_path: Path, caplog: Any
) -> None:
    monkeypatch.setenv(PREFIX + "ADMIN_USER", "unit-admin")
    monkeypatch.setenv(PREFIX + "FENCING_PORT", "19443")
    journal = tmp_path / "audit.jsonl"
    monkeypatch.setattr(fencing, "Path", lambda value: journal)
    monkeypatch.setattr(
        fencing.ssl, "create_default_context", Mock(return_value=Mock())
    )
    constructor = Mock(return_value=Mock())
    monkeypatch.setattr(fencing, "ThreadingHTTPServer", constructor)
    monkeypatch.setattr(
        fencing.Docker,
        "fence",
        Mock(
            side_effect=fencing.FencingError("sensitive-error-payload", "LEASE_CHANGED")
        ),
    )
    fencing.serve()
    handler_type = constructor.call_args.args[1]
    handler = object.__new__(handler_type)
    body = json.dumps(
        {
            "candidate": configured.candidate,
            "lease": configured.lease,
            "revision": configured.revision,
            "node": "postgresql-2",
            "request_id": "00000000-0000-4000-8000-000000000001",
        }
    ).encode()
    handler.path = "/fence"
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.connection = Mock()
    handler.connection.getpeercert.return_value = {
        "subject": ((("commonName", "unit-admin"),),)
    }
    response = Mock()
    handler.respond = response
    handler.do_POST()
    assert response.call_args.args == (
        409,
        {"error": "termination_not_confirmed", "reason_code": "LEASE_CHANGED"},
    )
    record = json.loads(journal.read_text())
    assert record["phase"] == "failed" and record["reason_code"] == "LEASE_CHANGED"
    assert "sensitive-error-payload" not in journal.read_text() + caplog.text


def test_executor_health_detects_stopped_return_loop_without_dcs_or_docker(
    configured: fencing.Claim, monkeypatch: Any
) -> None:
    monkeypatch.setenv(PREFIX + "ADMIN_USER", "unit-admin")
    monkeypatch.setenv(PREFIX + "FENCING_PORT", "19443")
    monkeypatch.setattr(
        fencing.ssl, "create_default_context", Mock(return_value=Mock())
    )
    constructor = Mock(return_value=Mock())
    monkeypatch.setattr(fencing, "ThreadingHTTPServer", constructor)
    thread = Mock()
    thread.is_alive.return_value = False
    monkeypatch.setattr(fencing.threading, "Thread", Mock(return_value=thread))
    current = Mock(side_effect=AssertionError("Health must not read DCS"))
    docker = Mock(side_effect=AssertionError("Health must not call Docker"))
    monkeypatch.setattr(fencing.Claim, "current", current)
    monkeypatch.setattr(fencing.Docker, "call", docker)
    fencing.serve()
    handler = object.__new__(constructor.call_args.args[1])
    handler.path = "/health"
    handler.connection = Mock()
    handler.connection.getpeercert.return_value = {
        "subject": ((("commonName", "unit-admin"),),)
    }
    handler.respond = Mock()
    handler.do_GET()
    handler.respond.assert_called_once_with(503, {"error": "return_loop_stopped"})
    thread.start.assert_called_once()
    current.assert_not_called()
    docker.assert_not_called()


@pytest.mark.parametrize("at_headers", [True, False])
@pytest.mark.parametrize("backend_fails", [True, False])
def test_response_disconnect_never_changes_durable_operation_result(
    configured: fencing.Claim,
    monkeypatch: Any,
    tmp_path: Path,
    caplog: Any,
    at_headers: bool,
    backend_fails: bool,
) -> None:
    monkeypatch.setenv(PREFIX + "ADMIN_USER", "unit-admin")
    monkeypatch.setenv(PREFIX + "FENCING_PORT", "19443")
    journal = tmp_path / "audit.jsonl"
    monkeypatch.setattr(fencing, "Path", lambda value: journal)
    monkeypatch.setattr(
        fencing.ssl, "create_default_context", Mock(return_value=Mock())
    )
    monkeypatch.setattr(fencing.threading, "Thread", Mock(return_value=Mock()))
    constructor = Mock(return_value=Mock())
    monkeypatch.setattr(fencing, "ThreadingHTTPServer", constructor)
    operation = Mock(
        side_effect=(
            fencing.FencingError("private-backend-payload", "LEASE_CHANGED")
            if backend_fails
            else None
        ),
        return_value=["a" * 64],
    )
    monkeypatch.setattr(fencing.Docker, "fence", operation)
    fencing.serve()
    handler = object.__new__(constructor.call_args.args[1])
    body = json.dumps(
        {
            "candidate": configured.candidate,
            "lease": configured.lease,
            "revision": configured.revision,
            "node": "postgresql-2",
            "request_id": "00000000-0000-4000-8000-000000000001",
        }
    ).encode()
    handler.path = "/fence"
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.authorized = Mock(return_value=True)
    disconnect = BrokenPipeError("private-transport-payload")
    handler.send_response = Mock(side_effect=disconnect if at_headers else None)
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.wfile = Mock()
    handler.wfile.write.side_effect = disconnect
    handler.do_POST()
    operation.assert_called_once()
    handler.send_response.assert_called_once_with(409 if backend_fails else 200)
    records = [json.loads(row) for row in journal.read_text().splitlines()]
    assert [row["phase"] for row in records] == [
        "failed" if backend_fails else "confirmed"
    ]
    if backend_fails:
        assert records[0]["reason_code"] == "LEASE_CHANGED"
    assert "fencing_response_delivery_failed" in caplog.text
    assert "private-transport-payload" not in journal.read_text() + caplog.text
    assert "private-backend-payload" not in journal.read_text() + caplog.text


def pod(
    *, uid: str = "old", phase: str = "Failed", terminated: bool = True
) -> dict[str, Any]:
    return {
        "metadata": {
            "uid": uid,
            "resourceVersion": "12",
            "labels": {
                fencing.CLUSTER_LABEL: "unit-cluster",
                fencing.NODE_LABEL: "postgresql-2",
            },
        },
        "spec": {"containers": [{"name": "postgresql"}]},
        "status": {
            "phase": phase,
            "containerStatuses": [
                {
                    "name": "postgresql",
                    "state": {"terminated" if terminated else "running": {}},
                }
            ],
        },
    }


@pytest.mark.parametrize(
    "parameters",
    [{"uid": "new"}, {"phase": "Unknown"}, {"phase": "Running"}, {"terminated": False}],
)
def test_kubernetes_false_evidence_is_rejected(parameters: dict[str, Any]) -> None:
    assert not fencing.Kubernetes.terminated(pod(**parameters), "old")


def test_kubernetes_all_containers_must_terminate() -> None:
    data = pod()
    assert fencing.Kubernetes.terminated(data, "old")
    data["spec"]["containers"].append({"name": "another"})
    assert not fencing.Kubernetes.terminated(data, "old")


@pytest.mark.parametrize("confirmed", [True, False])
def test_kubernetes_uid_precondition_and_watch(
    configured: fencing.Claim, monkeypatch: Any, confirmed: bool
) -> None:
    monkeypatch.setenv(PREFIX + "NODE_2_KUBERNETES_POD", "postgresql-2-0")
    monkeypatch.setenv(PREFIX + "FENCING_KUBERNETES_NAMESPACE", "postgresql")
    calls: list[tuple[str, str, Any]] = []

    def request(self: Any, method: str, path: str, payload: Any = None) -> io.BytesIO:
        calls.append((method, path, payload))
        if method == "DELETE":
            assert payload["preconditions"] == {"uid": "old"}
            assert payload["gracePeriodSeconds"] > 0
            return io.BytesIO(b"{}")
        if "?" not in path:
            return io.BytesIO(
                json.dumps(pod(phase="Running", terminated=False)).encode()
            )
        assert "resourceVersion=12" in path
        event = {
            "type": "MODIFIED" if confirmed else "DELETED",
            "object": pod(
                phase="Failed" if confirmed else "Unknown", terminated=confirmed
            ),
        }
        return io.BytesIO(json.dumps(event).encode() + b"\n")

    monkeypatch.setattr(fencing.Kubernetes, "open", request)
    if confirmed:
        assert fencing.Kubernetes().fence(
            "postgresql-2", configured, lambda *args: None
        ) == ["old"]
    else:
        with pytest.raises(fencing.FencingError, match="kubelet"):
            fencing.Kubernetes().fence("postgresql-2", configured, lambda *args: None)
    assert [item[0] for item in calls] == ["GET", "DELETE", "GET"]


@pytest.mark.parametrize(
    "bad", ["request_id", "lease", "node", "mechanism", "confirmed", "identities"]
)
def test_pre_promote_rejects_incomplete_receipt(
    configured: fencing.Claim, monkeypatch: Any, bad: str
) -> None:
    monkeypatch.setenv(PREFIX + "NODE_ID", "1")
    for number in (2, 3):
        monkeypatch.setenv(PREFIX + f"NODE_{number}_FENCING_URL", "https://executor")
    monkeypatch.setattr(fencing, "tls_context", lambda: None)

    def request(url: str, payload: dict[str, Any], context: Any) -> dict[str, Any]:
        receipt = {
            **payload,
            "mechanism": "docker",
            "confirmed": True,
            "identities": ["exact"],
        }
        receipt.pop(bad)
        return receipt

    monkeypatch.setattr(fencing, "request_json", request)
    with pytest.raises(fencing.FencingError, match="receipt"):
        fencing.pre_promote()
