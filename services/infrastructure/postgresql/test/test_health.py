"""Local readiness keeps safety checks without importing the SQL application."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from aelev_postgresql import fencing_probe, health, probe
from aelev_postgresql.configuration import PREFIX, desired_cluster_config, includes


@pytest.fixture
def local(monkeypatch: pytest.MonkeyPatch) -> Mock:
    for key, value in {
        "NODE_ID": "2",
        "NODE_2_HOST": "postgresql-2",
        "NODE_2_REST_PORT": "8008",
        "MAX_CONNECTIONS": "120",
        "SHARED_BUFFERS": "64MB",
    }.items():
        monkeypatch.setenv(PREFIX + key, value)
    monkeypatch.setattr(health, "tls_context", Mock())
    factory = Mock()
    reply = factory.return_value.getresponse.return_value
    reply.status = 200
    reply.read.side_effect = [b"{}", json.dumps(desired_cluster_config()).encode()]
    monkeypatch.setattr(health.http.client, "HTTPSConnection", factory)
    return factory


def test_only_this_node_is_probed_with_one_context(local: Mock) -> None:
    health.check()
    assert len(local.call_args_list) == 2
    assert all(call.args == ("postgresql-2", 8008) for call in local.call_args_list)
    assert [call.args for call in local.return_value.request.call_args_list] == [
        ("GET", "/health"),
        ("GET", "/config"),
    ]
    assert local.return_value.close.call_count == 2
    assert health.tls_context.call_count == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize("status", [301, 302, 403, 503])
def test_non_success_never_follows_redirect_or_peers(local: Mock, status: int) -> None:
    local.return_value.getresponse.return_value.status = status
    with pytest.raises(ValueError):
        health.check()
    assert local.call_count == 1
    local.return_value.close.assert_called_once()


@pytest.mark.parametrize("body", [b"null", b"[]", b"{}", b"not json", b"x" * 65537])
def test_malformed_or_incomplete_safety_configuration_fails(
    local: Mock, body: bytes
) -> None:
    local.return_value.getresponse.return_value.read.side_effect = [b"{}", body]
    with pytest.raises(ValueError):
        health.check()


def test_false_and_zero_do_not_satisfy_true_safety_fields() -> None:
    assert not includes({"strict": 1}, {"strict": True})
    assert not includes({"ttl": True}, {"ttl": 1})


def test_probe_does_not_load_application_or_database_driver() -> None:
    root = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import sys; import aelev_postgresql.health; "
            "assert not any(n in sys.modules for n in "
            "('psycopg','patroni','aelev_postgresql.__main__','aelev_postgresql.fencing'))",
        ],
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_absolute_budget_covers_tls_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    monkeypatch.setattr(probe, "BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(health, "check", lambda: time.sleep(1))
    started = time.monotonic()
    with pytest.raises(SystemExit) as exit:
        probe.main()
    assert exit.value.code == 1
    assert time.monotonic() - started < 0.5


@pytest.fixture
def executor(monkeypatch: pytest.MonkeyPatch) -> Mock:
    import http.client

    from aelev_postgresql import configuration

    monkeypatch.setenv(PREFIX + "FENCING_MODE", "docker")
    monkeypatch.setenv(PREFIX + "FENCING_PORT", "19443")
    monkeypatch.setattr(configuration, "tls_context", Mock())
    factory = Mock()
    reply = factory.return_value.getresponse.return_value
    reply.status = 200
    reply.read.return_value = b'{"mechanism":"docker"}'
    monkeypatch.setattr(http.client, "HTTPSConnection", factory)
    return factory


def test_executor_probe_is_local_authenticated_and_bounded(executor: Mock) -> None:
    from aelev_postgresql import configuration

    fencing_probe.check()
    assert executor.call_args.args == ("localhost", 19443)
    assert executor.call_args.kwargs["context"] is configuration.tls_context.return_value  # type: ignore[attr-defined]
    assert 0 < executor.call_args.kwargs["timeout"] <= 3
    executor.return_value.request.assert_called_once_with("GET", "/health")
    executor.return_value.getresponse.return_value.read.assert_called_once_with(4097)
    executor.return_value.close.assert_called_once()


@pytest.mark.parametrize("status", [301, 302, 403, 500, 503])
def test_executor_probe_rejects_redirect_and_failure(
    executor: Mock, status: int
) -> None:
    executor.return_value.getresponse.return_value.status = status
    with pytest.raises(ValueError):
        fencing_probe.check()
    executor.return_value.close.assert_called_once()


@pytest.mark.parametrize(
    "body",
    [b"null", b"[]", b"{}", b"invalid", b'{"mechanism":"kubernetes"}', b"x" * 4097],
)
def test_executor_probe_rejects_invalid_response(executor: Mock, body: bytes) -> None:
    executor.return_value.getresponse.return_value.read.return_value = body
    with pytest.raises(ValueError):
        fencing_probe.check()
    executor.return_value.close.assert_called_once()


def test_executor_probe_closes_failed_connection(executor: Mock) -> None:
    import http.client

    executor.return_value.request.side_effect = http.client.HTTPException()
    with pytest.raises(ValueError):
        fencing_probe.check()
    executor.return_value.close.assert_called_once()


def test_executor_probe_budget_covers_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    monkeypatch.setattr(fencing_probe, "BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(fencing_probe, "check", lambda: time.sleep(1))
    started = time.monotonic()
    with pytest.raises(SystemExit) as exit:
        fencing_probe.main()
    assert exit.value.code == 1
    assert time.monotonic() - started < 0.5


def test_executor_probe_imports_no_driver_or_fencing_application() -> None:
    root = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import sys; import aelev_postgresql.fencing_probe; "
            "assert not any(n in sys.modules for n in "
            "('psycopg','patroni','aelev_postgresql.__main__','aelev_postgresql.fencing'))",
        ],
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
