"""Crash/restart and safety regressions for autonomous exact-intent return."""

from __future__ import annotations

import base64
import copy
import threading
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from aelev_postgresql import fencing, fencing_intent, fencing_return
from aelev_postgresql.configuration import PREFIX


def use_ordered_dcs_fixture(monkeypatch: Any) -> None:
    """Keep validation cases deterministic; transport races have their own tests."""

    def read(context: Any, payload: Any, excluded: set[str], reader: Any) -> Any:
        for number in range(1, 4):
            endpoint = f"https://etcd-{number}:2379/v3/kv/range"
            if endpoint in excluded:
                continue
            try:
                return endpoint, reader(endpoint, payload, context)
            except OSError:
                continue
        raise fencing.FencingError("No reachable endpoint", "DCS_UNAVAILABLE")

    monkeypatch.setattr(fencing_return, "read_dcs", read)


@pytest.fixture
def rig(monkeypatch: Any, tmp_path: Path) -> Any:
    monkeypatch.setattr(fencing_intent, "ROOT", tmp_path / "state")
    monkeypatch.setenv(PREFIX + "CLUSTER", "test-cluster")
    monkeypatch.setenv(
        PREFIX + "FENCING_DOCKER_LOCAL_NODES", "postgresql-2,postgresql-3"
    )
    claim = fencing.Claim("postgresql-1", "9", "10")
    monkeypatch.setattr(fencing_return, "confirmed_claim", lambda: claim)
    monkeypatch.setattr(fencing_return.time, "time", lambda: 1000.0)
    state = {
        "Id": "a" * 64,
        "Config": {
            "Labels": {
                fencing.CLUSTER_LABEL: "test-cluster",
                fencing.NODE_LABEL: "postgresql-2",
            }
        },
        "State": {"StartedAt": "old", "Running": False, "Restarting": False, "Pid": 0},
    }
    calls: list[tuple[str, str]] = []
    rows = [{"Id": "a" * 64}]

    def call(method: str, path: str) -> Any:
        calls.append((method, path))
        if path.startswith("/containers/json?"):
            return copy.deepcopy(rows)
        if method == "POST":
            assert path == "/containers/" + "a" * 64 + "/start"
            state["State"].update(StartedAt="new", Running=True, Pid=3)
            return None
        assert path == "/containers/" + "a" * 64 + "/json"
        return copy.deepcopy(state)

    backend = Mock(spec=fencing.Docker)
    backend.call.side_effect = call
    record = Mock()
    locks = {node: threading.Lock() for node in fencing.NODES}
    store = fencing_intent.IntentStore("postgresql-2")
    with store.locked():
        store.save(
            {
                "version": 1,
                "cluster": "test-cluster",
                "node": "postgresql-2",
                "container_id": "a" * 64,
                "started_at": "old",
                "restart_required": True,
                "start_attempts": 0,
                "phase": "STOP_CONFIRMED",
                "claim": {
                    "candidate": "postgresql-3",
                    "lease": "old",
                    "revision": "old",
                },
            }
        )
    return {
        "worker": fencing_return.Returns(backend, locks, record),
        "state": state,
        "backend": backend,
        "calls": calls,
        "store": store,
        "record": record,
        "claim": claim,
        "rows": rows,
        "locks": locks,
    }


def test_lost_lease_after_stop_returns_without_another_promotion_rpc(rig: Any) -> None:
    rig["worker"].once()
    assert [method for method, _ in rig["calls"]].count("POST") == 1
    assert not any("stop" in path for _, path in rig["calls"])
    intent = rig["store"].load()
    assert intent["phase"] == "COMPLETED" and intent["start_attempts"] == 1
    # A new worker process and repeated passes must not restart it again.
    fencing_return.Returns(rig["backend"], rig["locks"], rig["record"]).once()
    assert [method for method, _ in rig["calls"]].count("POST") == 1


@pytest.mark.parametrize(
    "phase", ["STOP_REQUESTED", "STOP_CONFIRMED", "START_REQUESTED"]
)
def test_interrupted_phase_resumes_exact_stopped_process(rig: Any, phase: str) -> None:
    with rig["store"].locked():
        intent = rig["store"].load()
        intent.update(
            phase=phase, start_attempts=1 if phase == "START_REQUESTED" else 0
        )
        rig["store"].save(intent)
    rig["worker"].once()
    assert rig["store"].load()["phase"] == "COMPLETED"


@pytest.mark.parametrize(
    "change",
    ["container", "duplicate", "label", "incarnation", "running", "restarting"],
)
def test_uncertain_identity_or_termination_never_starts(rig: Any, change: str) -> None:
    if change == "container":
        rig["state"]["Id"] = "b" * 64
    elif change == "duplicate":
        rig["rows"].append({"Id": "b" * 64})
    elif change == "label":
        rig["state"]["Config"]["Labels"][fencing.CLUSTER_LABEL] = "foreign"
    elif change == "incarnation":
        rig["state"]["State"]["StartedAt"] = "changed"
    elif change == "running":
        rig["state"]["State"].update(Running=True, Pid=8)
    else:
        rig["state"]["State"]["Restarting"] = True
    rig["worker"].once()
    assert not any(method == "POST" for method, _ in rig["calls"])


@pytest.mark.parametrize(
    "phase,required",
    [("CANCELLED", True), ("COMPLETED", True), ("STOP_CONFIRMED", False)],
)
def test_manual_stops_cancelled_and_completed_are_untouched(
    rig: Any, phase: str, required: bool
) -> None:
    with rig["store"].locked():
        intent = rig["store"].load()
        intent.update(phase=phase, restart_required=required)
        rig["store"].save(intent)
    rig["worker"].once()
    assert not rig["calls"]


def test_absent_or_changed_lease_defers_without_spending_attempt(
    rig: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        fencing_return,
        "confirmed_claim",
        Mock(side_effect=fencing.FencingError("private", "DCS_QUORUM_UNCONFIRMED")),
    )
    rig["worker"].once()
    assert rig["store"].load()["start_attempts"] == 0
    assert rig["store"].load()["last_return_code"] == "DCS_QUORUM_UNCONFIRMED"
    monkeypatch.setattr(
        fencing_return,
        "confirmed_claim",
        Mock(side_effect=[rig["claim"], fencing.Claim("postgresql-3", "11", "12")]),
    )
    rig["worker"].once()
    assert not any(method == "POST" for method, _ in rig["calls"])


def test_never_starts_current_claimant(rig: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        fencing_return,
        "confirmed_claim",
        lambda: fencing.Claim("postgresql-2", "9", "10"),
    )
    rig["worker"].once()
    assert rig["store"].load()["last_return_code"] == "RETURN_TARGET_IS_CLAIMANT"
    assert not any(method == "POST" for method, _ in rig["calls"])


def test_cancel_and_rpc_share_nonblocking_file_and_node_locks(rig: Any) -> None:
    with rig["locks"]["postgresql-2"]:
        rig["worker"].once()
    with rig["store"].locked():
        rig["worker"].once()
    assert not rig["calls"]


def test_lost_start_completion_reconciles_without_second_command(
    rig: Any, monkeypatch: Any
) -> None:
    original = fencing_intent.IntentStore.save
    fail = True

    def save(self: Any, value: Any) -> None:
        nonlocal fail
        if fail and value["phase"] == "COMPLETED":
            fail = False
            raise OSError("disk busy")
        original(self, value)

    monkeypatch.setattr(fencing_intent.IntentStore, "save", save)
    rig["worker"].once()
    assert rig["store"].load()["phase"] == "START_REQUESTED"
    fencing_return.Returns(rig["backend"], rig["locks"], rig["record"]).once()
    assert rig["store"].load()["phase"] == "COMPLETED"
    assert sum(method == "POST" for method, _ in rig["calls"]) == 1


def test_failed_start_budget_survives_worker_restarts_and_cooldown(
    rig: Any, monkeypatch: Any
) -> None:
    def unavailable(method: str, path: str) -> Any:
        rig["calls"].append((method, path))
        if path.startswith("/containers/json?"):
            return copy.deepcopy(rig["rows"])
        if method == "POST":
            raise TimeoutError("private payload")
        return copy.deepcopy(rig["state"])

    rig["backend"].call.side_effect = unavailable
    for number in range(1, 5):
        monkeypatch.setattr(fencing_return.time, "time", lambda: 1000.0 + number * 400)
        worker = fencing_return.Returns(rig["backend"], rig["locks"], rig["record"])
        worker.once()
        worker.once()  # Same wall time cannot spend another attempt.
    assert rig["store"].load()["start_attempts"] == 3
    assert sum(method == "POST" for method, _ in rig["calls"]) == 3
    assert rig["store"].load()["last_return_code"] == "RETURN_ATTEMPTS_EXHAUSTED"


def test_journal_failure_before_dispatch_never_starts(rig: Any) -> None:
    rig["record"].side_effect = OSError("audit unavailable")
    rig["worker"].once()
    assert not any(method == "POST" for method, _ in rig["calls"])
    assert rig["store"].load()["start_attempts"] == 1


@pytest.mark.parametrize(
    "values,code",
    [
        (["same", "same"], None),
        (["same", "other"], "LEASE_CHANGED"),
        ([None, None], None),
        ([None, "same"], "LEASE_CHANGED"),
        (["same", None], "LEASE_CHANGED"),
        ([None, OSError(), OSError()], "DCS_QUORUM_UNCONFIRMED"),
        ([OSError(), OSError(), OSError()], "DCS_QUORUM_UNCONFIRMED"),
    ],
)
def test_native_dcs_reads_require_two_matching_linearizable_answers(
    monkeypatch: Any, values: list[Any], code: str | None
) -> None:
    use_ordered_dcs_fixture(monkeypatch)
    monkeypatch.setenv(PREFIX + "CLUSTER", "unit")
    for n in (1, 2, 3):
        monkeypatch.setenv(PREFIX + f"NODE_{n}_ETCD_HOST", f"etcd-{n}")
        monkeypatch.setenv(PREFIX + f"NODE_{n}_ETCD_CLIENT_PORT", "2379")
    monkeypatch.setattr(
        fencing_return.ssl, "create_default_context", Mock(return_value=Mock())
    )
    requests: list[Any] = []

    def request(url: str, payload: Any, context: Any) -> Any:
        requests.append(payload)
        value = values[len(requests) - 1]
        if isinstance(value, Exception):
            raise value
        header = {"cluster_id": "42", "member_id": str(len(requests)), "revision": "99"}
        if value is None:
            return {"header": header}
        return {
            "header": header,
            "kvs": [
                {
                    "value": base64.b64encode(b"postgresql-1").decode(),
                    "lease": "9" if value == "same" else "8",
                    "create_revision": "7",
                }
            ],
        }

    monkeypatch.setattr(fencing_return, "request_json", request)
    if code is None:
        assert fencing_return.confirmed_claim() == (
            None if values[0] is None else fencing.Claim("postgresql-1", "9", "7")
        )
        assert len(requests) == 2
    else:
        with pytest.raises(fencing.FencingError) as error:
            fencing_return.confirmed_claim()
        assert error.value.code == code
    assert all(item["serializable"] is False for item in requests)


def test_confirmed_absence_returns_only_owned_intent(
    rig: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(fencing_return, "confirmed_claim", lambda: None)
    rig["worker"].once()
    assert sum(method == "POST" for method, _ in rig["calls"]) == 1
    intent = rig["store"].load()
    assert intent["phase"] == "COMPLETED" and intent["start_attempts"] == 1
    assert intent["return_admission"] == "LEASE_ABSENCE_CONFIRMED"
    assert intent["claim"]["candidate"] == "postgresql-3"  # Old receipt is unchanged.


@pytest.mark.parametrize(
    "before,after",
    [
        (None, fencing.Claim("postgresql-1", "9", "10")),
        (fencing.Claim("postgresql-1", "9", "10"), None),
    ],
)
def test_lease_appears_or_disappears_before_start_defers(
    rig: Any, monkeypatch: Any, before: Any, after: Any
) -> None:
    monkeypatch.setattr(
        fencing_return, "confirmed_claim", Mock(side_effect=[before, after])
    )
    rig["worker"].once()
    assert not any(method == "POST" for method, _ in rig["calls"])
    assert rig["store"].load()["start_attempts"] == 0
    assert rig["store"].load()["last_return_code"] == "LEASE_CHANGED"


@pytest.mark.parametrize(
    "bad",
    [
        "missing_header",
        "null_header",
        "count",
        "more",
        "same_member",
        "other_cluster",
        "revision_regressed",
        "malformed_rows",
    ],
)
def test_absence_needs_two_valid_native_member_responses(
    monkeypatch: Any, bad: str
) -> None:
    use_ordered_dcs_fixture(monkeypatch)
    monkeypatch.setenv(PREFIX + "CLUSTER", "unit")
    for n in (1, 2, 3):
        monkeypatch.setenv(PREFIX + f"NODE_{n}_ETCD_HOST", f"etcd-{n}")
        monkeypatch.setenv(PREFIX + f"NODE_{n}_ETCD_CLIENT_PORT", "2379")
    monkeypatch.setattr(
        fencing_return.ssl, "create_default_context", Mock(return_value=Mock())
    )
    number = 0

    def request(*args: Any) -> Any:
        nonlocal number
        number += 1
        data: dict[str, Any] = {
            "header": {"cluster_id": "42", "member_id": str(number), "revision": "99"}
        }
        if bad == "missing_header":
            data = {}
        if bad == "null_header":
            data["header"] = None
        if bad == "count":
            data["count"] = "1"
        if bad == "more":
            data["more"] = True
        if bad == "same_member":
            data["header"]["member_id"] = "1"
        if bad == "other_cluster" and number == 2:
            data["header"]["cluster_id"] = "43"
        if bad == "revision_regressed" and number == 2:
            data["header"]["revision"] = "98"
        if bad == "malformed_rows":
            data["kvs"] = None
        return data

    monkeypatch.setattr(fencing_return, "request_json", request)
    with pytest.raises(fencing.FencingError):
        fencing_return.confirmed_claim()
