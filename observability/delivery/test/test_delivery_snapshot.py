"""The release checker preserves the stopped source and never publishes."""

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from delivery import Journal
from delivery_snapshot import fingerprint, verify


def snapshot(root: Path) -> Journal:
    journal = Journal(root, 1024**2)
    journal.accept("logs", {}, b"private-fixture-payload", str(uuid4()))
    record = journal.first("logs")
    assert record is not None
    journal.failed(record, "http_503")
    with journal.connect() as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return journal


def test_checker_preserves_records_retry_state_and_source(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    snapshot(root)
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    result = verify(root, capacity=1024**2)
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before
    assert result["snapshot"]["records"] == result["snapshot"]["failed"] == 1
    assert result["snapshot"]["pending"] == 1
    assert "private-fixture" not in str(result)


def test_checker_detects_packet_corruption(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    journal = snapshot(root)
    with journal.connect() as db:
        db.execute("UPDATE records SET body=?", (b"corrupted",))
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with pytest.raises(ValueError, match="PACKET_DIGEST"):
        verify(root, capacity=1024**2)


def test_checker_rejects_symlink_database(tmp_path: Path) -> None:
    journal = snapshot(tmp_path / "original")
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "delivery.sqlite3").symlink_to(journal.path)
    with pytest.raises(ValueError, match="FILE_INVALID"):
        verify(root, capacity=1024**2)


def test_empty_acknowledged_journal_preserves_sequence_highwater(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    journal = snapshot(root)
    record = journal.first("logs")
    assert record is not None
    journal.complete(record, archive=False)
    with journal.connect() as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    result = verify(root, capacity=1024**2)
    assert result["snapshot"]["records"] == 0
    assert result["snapshot"]["sequence_highwater"] == 1


def test_checker_restores_only_derived_legacy_projection_on_copy(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    journal = snapshot(root)
    with sqlite3.connect(journal.path) as db:
        for action in ("insert", "update", "delete"):
            db.execute("DROP TRIGGER record_counters_v1_" + action)
        db.execute("DROP TABLE record_counters_v1")
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = journal.path.read_bytes()
    assert verify(root, capacity=1024**2)["snapshot"]["records"] == 1
    assert journal.path.read_bytes() == before


def test_checker_preserves_central_archive_and_pending_packets(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    journal = snapshot(root)
    record = journal.first("logs")
    assert record is not None
    journal.complete(record, archive=True)
    journal.accept("metrics", {}, b"private-pending-metric", str(uuid4()))
    with journal.connect() as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    result = verify(root, capacity=1024**2)
    assert result["snapshot"]["records"] == 2
    assert result["snapshot"]["pending"] == 1
    assert result["snapshot"]["sequence_highwater"] == 2
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before
    assert result["delivery_started"] is False


def test_offline_integrity_cache_is_bounded_and_connection_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = snapshot(tmp_path / "snapshot")
    original = sqlite3.connect
    connections: list[sqlite3.Connection] = []
    queries: list[str] = []

    def connect(database: str, *, uri: bool = False) -> sqlite3.Connection:
        db = original(database, uri=uri)
        db.set_trace_callback(queries.append)
        connections.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", connect)
    assert fingerprint(journal.path)["records"] == 1
    assert "PRAGMA cache_size=-65536" in queries
    assert "PRAGMA integrity_check" in queries
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")
