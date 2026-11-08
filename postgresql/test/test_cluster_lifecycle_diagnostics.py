"""Safe failure classification for the destructive PostgreSQL lifecycle driver."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PATH = Path(__file__).with_name("cluster-lifecycle.py")
SPEC = importlib.util.spec_from_file_location("cluster_lifecycle", PATH)
assert SPEC and SPEC.loader
cluster_lifecycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cluster_lifecycle)


@pytest.mark.parametrize(
    ("message", "code"),
    [
        (
            "ValueError: Persisted cluster safety configuration does not match the delivery",
            "PERSISTED_SAFETY_CONFIG_MISMATCH",
        ),
        (
            "ValueError: No reachable Patroni configuration endpoint",
            "PATRONI_CONFIG_UNREACHABLE",
        ),
        ("HTTP Error 401: Unauthorized", "PATRONI_AUTHORIZATION_FAILED"),
        ("certificate verify failed", "TLS_VERIFICATION_FAILED"),
        ("connection refused", "CONNECTION_REFUSED"),
        ("AssertionError", "ASSERTION_FAILED"),
        ("token=must-not-survive", "PROCESS_FAILED"),
    ],
)
def test_failure_output_is_reduced_to_fixed_code(message: str, code: str) -> None:
    assert cluster_lifecycle.classify_command_failure(message) == code


def test_command_failure_never_exposes_raw_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1,
            stdout="credential=must-not-survive\n",
            stderr="ValueError: No reachable Patroni configuration endpoint\n",
        )

    monkeypatch.setattr(cluster_lifecycle.subprocess, "run", failed)
    with pytest.raises(
        RuntimeError,
        match=(
            "VERIFY_CLUSTER_SAFETY failed "
            r"\(exit 1; reason=PATRONI_CONFIG_UNREACHABLE\)"
        ),
    ) as failure:
        cluster_lifecycle.command(
            "docker", "compose", operation="VERIFY_CLUSTER_SAFETY"
        )
    assert "must-not-survive" not in str(failure.value)


def test_failure_signals_exclude_private_native_output() -> None:
    output = (
        "secret=must-not-survive https://private.invalid\n"
        "reason_code=LEASE_CHANGED\n"
        "reason_code=ARBITRARY_PRIVATE_TEXT\n"
        "pre_promote script failed\nPermission denied\n"
    )
    assert cluster_lifecycle.failure_signals(output) == {
        "PRE_PROMOTE_FAILED": 1,
        "PRE_PROMOTE_COMMAND": 1,
        "LEASE_CHANGED": 1,
        "PERMISSION_DENIED": 1,
    }


def test_fencing_diagnostic_retains_timeline_without_private_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    payload = {
        "node": "postgresql-2",
        "candidate": "postgresql-3",
        "phase": "failed",
        "reason_code": "NO_LEADER_LEASE",
        "recorded_at": 123.5,
        "lease": "123",
        "revision": "10",
        "password": "must-not-survive",
        "message": "must-not-survive",
        "identities": ["must-not-survive"],
    }
    monkeypatch.setattr(
        cluster_lifecycle,
        "compose",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(payload)),
    )
    assert cluster_lifecycle.fencing_audit() == [
        {
            "node": "postgresql-2",
            "candidate": "postgresql-3",
            "phase": "failed",
            "reason_code": "NO_LEADER_LEASE",
            "recorded_at": 123.5,
            "lease": "123",
            "revision": "10",
        }
    ]


def test_quorum_waits_for_actual_streaming_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    samples = Mock(
        side_effect=[
            {"commit_on": True, "quorum_rule": False, "sync_replicas": 0},
            {"commit_on": True, "quorum_rule": True, "sync_replicas": 0},
            {"commit_on": True, "quorum_rule": True, "sync_replicas": 1},
        ]
    )
    monkeypatch.setattr(cluster_lifecycle, "synchronous_quorum", samples)
    monkeypatch.setattr(cluster_lifecycle.time, "sleep", lambda _: None)
    cluster_lifecycle.wait_synchronous_quorum(1)
    assert samples.call_count == 3


def test_bootstrap_creates_both_fencing_peers_before_starting_initial_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    run = Mock(
        side_effect=[
            SimpleNamespace(stdout=""),
            SimpleNamespace(stdout=""),
            SimpleNamespace(stdout='{"Health":"healthy"}\n'),
        ]
    )
    monkeypatch.setattr(cluster_lifecycle, "compose", run)
    cluster_lifecycle.start_bootstrap_member(1)
    assert [call.args for call in run.call_args_list] == [
        ("create", "--no-build", "--pull", "never"),
        (
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
            "etcd-1",
            "etcd-2",
            "etcd-3",
            "postgresql-1",
        ),
        ("ps", "--format", "json", "postgresql-1"),
    ]


def test_quorum_never_waits_away_disabled_sync_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cluster_lifecycle,
        "synchronous_quorum",
        lambda _: {"commit_on": False, "quorum_rule": True, "sync_replicas": 2},
    )
    with pytest.raises(AssertionError, match="SYNCHRONOUS_COMMIT_DISABLED"):
        cluster_lifecycle.wait_synchronous_quorum(1)


def test_fault_target_requires_one_primary_and_all_three_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    execute = Mock()
    samples = Mock(
        side_effect=[
            {1: False, 2: False, 3: True},
            {1: False, 2: True},
            {1: False, 2: True, 3: True},
        ]
    )
    quorum = Mock()
    monkeypatch.setattr(cluster_lifecycle, "execute", execute)
    monkeypatch.setattr(cluster_lifecycle, "roles", samples)
    monkeypatch.setattr(cluster_lifecycle, "wait_synchronous_quorum", quorum)
    monkeypatch.setattr(cluster_lifecycle.time, "sleep", lambda _: None)
    assert cluster_lifecycle.select_fault_target(2, 1) == 1
    assert execute.call_count == 1
    assert samples.call_count == 3
    quorum.assert_called_once_with(1)


def test_new_writer_is_not_accepted_while_excluded_old_writer_is_still_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    samples = Mock(
        side_effect=[
            {1: False, 2: False, 3: True},
            {1: True, 2: False, 3: True},
        ]
    )
    monkeypatch.setattr(cluster_lifecycle, "roles", samples)
    monkeypatch.setattr(cluster_lifecycle.time, "sleep", lambda _: None)
    assert cluster_lifecycle.wait_writer(excluded=1) == 2
    assert samples.call_count == 2
