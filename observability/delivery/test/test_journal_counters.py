"""Exact counters remain transactional, rebuildable and independent of payload scans."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from delivery import CapacityError, Journal


def assert_exact(journal: Journal) -> None:
    with journal.connect() as db:
        expected = db.execute(
            "SELECT COUNT(*),COALESCE(SUM(projected=0),0),"
            "COALESCE(SUM(length(body)+1024),0),COALESCE(SUM(error!=''),0) FROM records"
        ).fetchone()
    actual = journal.status()
    assert (
        tuple(actual[k] for k in ("records", "pending", "bytes", "failed")) == expected
    )


def test_legacy_backfill_restarts_and_old_sql_writers(tmp_path: Path) -> None:
    journal = Journal(tmp_path, 100_000)
    for kind in ("logs", "metrics", "traces", "otlp_metrics"):
        journal.accept(kind, {}, b"exact", str(uuid4()))
    record = journal.first("logs")
    assert record is not None
    journal.failed(record, "http_403")
    projected = journal.first("traces")
    assert projected is not None
    journal.complete(projected, archive=True)
    # Remove only the derived projection to reproduce the previous release's schema.
    with journal.connect() as db:
        for action in ("insert", "update", "delete"):
            db.execute(f"DROP TRIGGER record_counters_v1_{action}")
        db.execute("DROP TABLE record_counters_v1")
    migrated = Journal(tmp_path, journal.capacity)
    assert_exact(migrated)
    assert migrated.first("logs") == record
    assert migrated.blocking_errors() == {"logs": "http_403"}
    assert Journal(tmp_path, journal.capacity).status() == migrated.status()
    with migrated.connect() as db:
        db.execute("UPDATE records SET error='transport_failure',body=?", (b"changed",))
    assert_exact(migrated)
    migrated.replay()
    assert_exact(migrated)
    assert migrated.blocking_errors() == {}
    migrated.complete(record, archive=False)
    assert_exact(migrated)


def test_transaction_rollback_does_not_change_counters(tmp_path: Path) -> None:
    journal = Journal(tmp_path, 100_000)
    identifier = str(uuid4())
    journal.accept("metrics", {}, b"exact", identifier)
    before = journal.status()
    with pytest.raises(RuntimeError), journal.connect() as db:
        db.execute("DELETE FROM records WHERE id=?", (identifier,))
        raise RuntimeError("simulated transaction failure")
    assert journal.status() == before
    assert_exact(journal)


def test_backfill_uses_existing_index_without_sorting_payloads(tmp_path: Path) -> None:
    journal = Journal(tmp_path, 100_000)
    with journal.connect() as db:
        for action in ("insert", "update", "delete"):
            db.execute(f"DROP TRIGGER record_counters_v1_{action}")
        db.execute("DROP TABLE record_counters_v1")
        statements: list[str] = []
        db.set_trace_callback(statements.append)
        Journal.prepare_counters(db)
        insert = next(
            statement
            for statement in statements
            if statement.startswith("INSERT INTO record_counters_v1 SELECT")
        )
        plan = db.execute("EXPLAIN QUERY PLAN " + insert).fetchall()
        assert any("USING INDEX pending" in row[3] for row in plan)
        assert all("TEMP B-TREE" not in row[3] for row in plan)


def test_concurrent_connections_cannot_over_admit(tmp_path: Path) -> None:
    journals = [Journal(tmp_path, 1100), Journal(tmp_path, 1100)]
    barrier = threading.Barrier(2)

    def accept(journal: Journal) -> bool:
        barrier.wait(timeout=3)
        try:
            journal.accept("logs", {}, b"retained", str(uuid4()))
        except CapacityError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(accept, journals)) == [False, True]
    assert_exact(journals[0])
    assert journals[0].status()["records"] == 1


def test_status_does_not_read_records_or_wait_for_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = Journal(tmp_path, 100_000)
    journal.accept("logs", {}, b"opaque", str(uuid4()))
    original_connect = sqlite3.connect

    def connect(database: Path, timeout: float = 5) -> sqlite3.Connection:
        db = original_connect(database, timeout=timeout)

        def authorize(
            action: int,
            table: str | None,
            column: str | None,
            database: str | None,
            trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_READ and table == "records":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        return db

    monkeypatch.setattr(sqlite3, "connect", connect)
    with journal.lock, ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(journal.status).result(timeout=2)["bytes"] == 1030
        assert pool.submit(journal.admission_available, 4096).result(timeout=2)


def test_diagnostics_redact_unknown_historical_error(tmp_path: Path) -> None:
    journal = Journal(tmp_path, 100_000)
    journal.accept("metrics", {}, b"exact", str(uuid4()))
    record = journal.first("metrics")
    assert record is not None
    journal.failed(record, "sensitive historical exception text")
    assert journal.blocking_errors() == {"metrics": "delivery_failed"}
    assert_exact(journal)
