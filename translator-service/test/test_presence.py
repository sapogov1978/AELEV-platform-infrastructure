"""Technical presence is rebuildable and never substitutes queue progress."""

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from sqlalchemy import Engine

from translator_service import presence
from translator_service.schema import HEAD


def engine_with(rows: list[dict[str, object]]) -> Engine:
    engine = MagicMock()
    result = engine.connect.return_value.__enter__.return_value.execute.return_value
    result.mappings.return_value.all.return_value = rows
    return cast(Engine, engine)


def test_missing_roles_are_unknown() -> None:
    result = presence.read(engine_with([]), 30)
    assert [row["id"] for row in result] == list(presence.COMPONENTS)
    assert all(row["state"] == "UNKNOWN" for row in result)
    assert all(row["observed_at"] is None for row in result)


def test_stale_heartbeat_never_makes_role_online() -> None:
    engine = engine_with(
        [
            {
                "component": "worker",
                "state": "ONLINE",
                "fresh": False,
                "observed_at": datetime.now(UTC),
            },
        ]
    )
    assert (
        next(row for row in presence.read(engine, 30) if row["id"] == "worker")["state"]
        == "UNKNOWN"
    )


def test_live_replica_proves_availability_not_all_replicas() -> None:
    now = datetime.now(UTC)
    engine = engine_with(
        [
            {
                "component": "worker",
                "state": "UNKNOWN",
                "fresh": True,
                "observed_at": now,
            },
            {
                "component": "worker",
                "state": "ONLINE",
                "fresh": True,
                "observed_at": now,
            },
        ]
    )
    row = next(row for row in presence.read(engine, 30) if row["id"] == "worker")
    assert row["state"] == "ONLINE" and row["observed_at"] == now.isoformat()


@pytest.mark.parametrize("ttl", [0, -1])
def test_positive_ttl_required(ttl: int) -> None:
    with pytest.raises(ValueError):
        presence.read(engine_with([]), ttl)


def test_only_presence_projection_has_retention() -> None:
    source = Path(presence.__file__).read_text()
    assert "DELETE FROM translation_runtime_presence " in source
    assert "DELETE FROM translation_jobs" not in source
    assert "DELETE FROM translation_results" not in source
    assert HEAD == "20261014_0005"


def test_model_failure_is_uncertainty(monkeypatch: pytest.MonkeyPatch) -> None:
    import translator_service.translator_healthcheck as health

    monkeypatch.setattr(health, "main", Mock(side_effect=OSError("unreachable")))
    assert presence.observe_model() == "UNKNOWN"


def test_missing_private_model_socket_is_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import translator_service.translator_healthcheck as health

    error = httpx.ConnectError(
        "private model socket is unavailable",
        request=httpx.Request("GET", "http://translator/runtimez"),
    )
    monkeypatch.setattr(health, "main", Mock(side_effect=error))
    assert presence.observe_model() == "OFFLINE"


def test_consumer_heartbeat_stops_and_writes_only_presence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AELEV_TRANSLATOR_PRESENCE_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("AELEV_TRANSLATOR_REQUEST_PRIORITY", "0")
    stopped = threading.Event()
    engine = MagicMock()
    execute = engine.begin.return_value.__enter__.return_value.execute

    def observe_write(*args: object) -> None:
        stopped.set()

    execute.side_effect = observe_write
    thread = presence.start(cast(Engine, engine), "consumer", stopped)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert execute.call_args_list[0].args[1]["component"] == "priority-consumer"
    assert all(
        "translation_runtime_presence" in str(call.args[0])
        for call in execute.call_args_list
    )
