"""Fail-closed SSH receipts and bounded transport, without a real Docker API."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from aelev_postgresql import fencing, fencing_ssh
from aelev_postgresql.configuration import PREFIX


@pytest.fixture
def remote(monkeypatch: Any, tmp_path: Path) -> Any:
    key, hosts, configuration = (
        tmp_path / name for name in ("key", "known_hosts", "bindings.json")
    )
    key.write_text("private-test-placeholder")
    hosts.write_text("test-placeholder")
    key.chmod(0o600)
    hosts.chmod(0o600)
    binding = {
        "host": "fencing-node.invalid",
        "port": 22,
        "user": "fencing",
        "identity_file": str(key),
        "known_hosts_file": str(hosts),
        "daemon_id": "owned-daemon-id",
    }
    configuration.write_text(json.dumps({"postgresql-2": binding}))
    configuration.chmod(0o600)
    monkeypatch.setenv(PREFIX + "FENCING_DOCKER_SSH_BINDINGS", str(configuration))
    monkeypatch.setenv(PREFIX + "CLUSTER", "fencing-test")
    monkeypatch.setenv(PREFIX + "FENCING_TIMEOUT_SECONDS", "5")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def exchange(command: list[str], body: bytes) -> bytes:
        request = json.loads(body)
        calls.append((command, request))
        return json.dumps(
            {
                "ok": True,
                "request": request,
                "identities": ["a" * 64],
                "dispatched": False,
            }
        ).encode()

    monkeypatch.setattr(fencing_ssh, "exchange", exchange)
    return {
        "backend": fencing_ssh.SSHDocker(),
        "configuration": configuration,
        "calls": calls,
        "key": key,
    }


def test_fixed_ssh_command_and_strict_trust(remote: Any) -> None:
    remote["backend"].call("postgresql-2", "status")
    command, request = remote["calls"][0]
    assert command[-1] == "aelev-postgresql-fencing-node-v1"
    assert command[:3] == ["/usr/bin/ssh", "-F", "/dev/null"]
    assert {
        "StrictHostKeyChecking=yes",
        "IdentityAgent=none",
        "ForwardAgent=no",
        "ClearAllForwardings=yes",
        "PasswordAuthentication=no",
        "ControlMaster=no",
    } <= set(command)
    assert request["daemon_id"] == "owned-daemon-id"
    assert request["action"] == "status"


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "-oProxyCommand=bad"),
        ("user", "bad;command"),
        ("port", True),
        ("port", 0),
        ("daemon_id", ""),
        ("command", "sh"),
    ],
)
def test_rejects_destination_injection(remote: Any, field: str, value: Any) -> None:
    path = remote["configuration"]
    data = json.loads(path.read_text())
    data["postgresql-2"][field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(fencing.FencingError):
        fencing_ssh.SSHDocker()


def test_private_key_required(remote: Any) -> None:
    remote["key"].chmod(0o644)
    with pytest.raises(fencing.FencingError):
        fencing_ssh.SSHDocker()


def test_unconfigured_target_has_no_local_fallback(remote: Any) -> None:
    with pytest.raises(fencing.FencingError):
        remote["backend"].call("postgresql-3", "fence")
    assert not remote["calls"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: {**r, "ok": False},
        lambda r: {**r, "request": {**r["request"], "node": "postgresql-3"}},
        lambda r: {**r, "request": {**r["request"], "request_id": "stale"}},
    ],
)
def test_rejects_failed_foreign_or_stale_receipt(
    remote: Any, monkeypatch: Any, mutate: Any
) -> None:
    monkeypatch.setattr(
        fencing_ssh,
        "exchange",
        lambda command, body: json.dumps(
            mutate({"ok": True, "request": json.loads(body)})
        ).encode(),
    )
    with pytest.raises(fencing.FencingError):
        remote["backend"].call("postgresql-2", "status")


def test_native_claim_verified_before_and_after_remote_operation(
    remote: Any, monkeypatch: Any
) -> None:
    claim = fencing.Claim("postgresql-1", "7", "8")
    verify = Mock()
    monkeypatch.setattr(fencing.Claim, "verify", verify)
    record = Mock()
    assert remote["backend"].fence("postgresql-2", claim, record) == ["a" * 64]
    assert verify.call_count == 2
    assert remote["calls"][0][1]["claim"] == asdict(claim)
    record.assert_called_once_with("remote_confirmed", ["a" * 64])


def test_return_transport_does_not_store_replica_local_budget(remote: Any) -> None:
    for _ in range(3):
        fencing_ssh.SSHReturns(fencing_ssh.SSHDocker()).once()
    assert len(remote["calls"]) == 3
    assert all(request["action"] == "return-once" for _, request in remote["calls"])


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_unbounded_peer_output_is_stopped(monkeypatch: Any, stream: str) -> None:
    monkeypatch.setenv(PREFIX + "FENCING_TIMEOUT_SECONDS", "5")
    with pytest.raises(fencing.FencingError):
        fencing_ssh.exchange(
            [sys.executable, "-c", f"import sys; sys.{stream}.write('x'*20000)"], b"{}"
        )


def test_remote_health_requires_fresh_admitted_nodes(
    remote: Any, monkeypatch: Any
) -> None:
    worker = fencing_ssh.SSHReturns(remote["backend"])
    assert worker.healthy() is False
    worker.once()
    assert worker.healthy() is True
    last = worker.verified["postgresql-2"]
    monkeypatch.setattr(fencing_ssh.time, "monotonic", lambda: last + 1000)
    assert worker.healthy() is False


def test_exchange_roundtrip(monkeypatch: Any) -> None:
    monkeypatch.setenv(PREFIX + "FENCING_TIMEOUT_SECONDS", "5")
    assert (
        fencing_ssh.exchange(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            ],
            b'{"bounded":true}',
        )
        == b'{"bounded":true}'
    )


def test_exchange_deadline_kills_stalled_child(monkeypatch: Any) -> None:
    monkeypatch.setattr(fencing_ssh, "timeout", lambda: 0.05)
    with pytest.raises(fencing.FencingError):
        fencing_ssh.exchange(
            [sys.executable, "-c", "import time; time.sleep(10)"], b"{}"
        )
