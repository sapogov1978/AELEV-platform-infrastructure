"""The thin probe never imports publisher/application dependencies."""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "client/src/aelev_platform_ui_client/health.py"
spec = importlib.util.spec_from_file_location("standalone_publisher_health", SOURCE)
assert spec is not None and spec.loader is not None
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


def journal(path: Path, *, owner: str = "owner", timestamp: float = 1000) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE state(id INTEGER PRIMARY KEY,module TEXT,heartbeat REAL)"
        )
        db.execute("INSERT INTO state VALUES(1,?,?)", (owner, timestamp))


def test_fresh_owned_heartbeat_needs_no_network(tmp_path, monkeypatch):
    path = tmp_path / "outbox.sqlite"
    journal(path)
    monkeypatch.setattr(health.time, "time", lambda: 1010)
    health.check(path, "owner", 30)


@pytest.mark.parametrize("timestamp", [0, 900, 1100, float("inf"), float("nan")])
def test_stale_or_invalid_heartbeat_is_not_healthy(tmp_path, monkeypatch, timestamp):
    path = tmp_path / "outbox.sqlite"
    journal(path, timestamp=timestamp)
    monkeypatch.setattr(health.time, "time", lambda: 1010)
    with pytest.raises(RuntimeError):
        health.check(path, "owner", 30)


def test_missing_journal_is_never_created(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        health.check(path, "owner", 30)
    assert not path.exists()


def test_foreign_owner_is_rejected(tmp_path):
    path = tmp_path / "outbox.sqlite"
    journal(path, owner="foreign")
    with pytest.raises(ValueError, match="OWNER_MISMATCH"):
        health.check(path, "owner", 30)


def test_probe_does_not_load_application_or_transport_modules():
    assert "standalone_publisher_health" not in sys.modules
    assert not any(name.startswith("sqlalchemy") for name in health.__dict__)
    assert set(health.__dict__) & {"ssl", "threading", "urllib", "psycopg"} == set()
