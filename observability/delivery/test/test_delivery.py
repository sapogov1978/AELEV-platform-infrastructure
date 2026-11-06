"""Durability, retries, admission limits and immutable delivery identities."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from delivery import CapacityError, Journal, Settings, backend_ack, digest_of


def test_restart_keeps_exact_bytes_and_identity(tmp_path: Path) -> None:
    journal = Journal(tmp_path, 100_000)
    identifier = str(uuid4())
    headers = {"Content-Type": "application/x-protobuf", "Content-Encoding": "snappy"}
    body = b"\x00\xff\x81opaque-native-payload"
    digest = journal.accept("metrics", headers, body, identifier)
    restarted = Journal(tmp_path, 100_000)
    with restarted.connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
    record = restarted.first("metrics")
    assert record is not None
    assert (record.identifier, record.body, record.digest) == (identifier, body, digest)
    assert record.headers == headers
    assert restarted.path.stat().st_mode & 0o777 == 0o600


def test_duplicate_receipt_after_lost_ack_is_idempotent(tmp_path: Path) -> None:
    journal = Journal(tmp_path, 100_000)
    identifier = str(uuid4())
    digest = journal.accept("logs", {}, b"payload", identifier)
    assert journal.accept("logs", {}, b"payload", identifier) == digest
    assert journal.status()["records"] == 1
    with pytest.raises(ValueError, match="immutable"):
        journal.accept("logs", {}, b"different", identifier)
    assert journal.first("logs") is not None


def test_full_journal_rejects_new_data_without_evicting_pending(tmp_path: Path) -> None:
    journal = Journal(tmp_path, 1100)
    identifier = str(uuid4())
    journal.accept("logs", {}, b"retained", identifier)
    with pytest.raises(CapacityError):
        journal.accept("traces", {}, b"too-large" * 100, str(uuid4()))
    assert journal.first("logs") is not None
    assert journal.status()["records"] == 1


def test_pending_records_have_no_age_expiry(tmp_path: Path) -> None:
    journal = Journal(tmp_path, 100_000)
    journal.accept("logs", {}, b"old", str(uuid4()))
    with journal.connect() as db:
        db.execute("UPDATE records SET created_ns=1")
    assert Journal(tmp_path, 100_000).first("logs") is not None


def test_central_archive_retains_projected_payload_and_can_rebuild(
    tmp_path: Path,
) -> None:
    journal = Journal(tmp_path, 100_000)
    journal.accept("traces", {}, b"exact", str(uuid4()))
    record = journal.first("traces")
    assert record is not None
    journal.complete(record, archive=True)
    assert journal.first("traces") is None
    assert journal.status()["records"] == 1
    journal.replay()
    assert journal.first("traces") == record


def test_only_verified_central_receipt_releases_sender_record(tmp_path: Path) -> None:
    journal = Journal(tmp_path, 100_000)
    journal.accept("metrics", {}, b"original", str(uuid4()))
    record = journal.first("metrics")
    assert record is not None
    journal.failed(record, "http_403_or_incomplete_ack")
    journal.failed(record, "http_400_or_incomplete_ack")
    assert journal.first("metrics") == record
    assert journal.status()["failed"] == 1
    journal.complete(record, archive=False)
    assert journal.status()["records"] == 0


def test_corruption_fails_closed(tmp_path: Path) -> None:
    journal = Journal(tmp_path, 100_000)
    journal.accept("logs", {}, b"valid", str(uuid4()))
    with sqlite3.connect(journal.path) as db:
        db.execute("UPDATE records SET body=?", (b"corrupt",))
    with pytest.raises(ValueError, match="integrity"):
        journal.first("logs")


def test_pending_is_ordered_per_signal_not_blocked_by_another_signal(
    tmp_path: Path,
) -> None:
    journal = Journal(tmp_path, 100_000)
    for signal in ("logs", "metrics", "logs", "traces"):
        journal.accept(signal, {}, signal.encode(), str(uuid4()))
    assert journal.first("logs").sequence == 1  # type: ignore[union-attr]
    assert journal.first("metrics").sequence == 2  # type: ignore[union-attr]
    assert journal.first("traces").sequence == 4  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{}",
        b'{"partialSuccess":{}}',
        b"\x0a\x00",
        b'{"partialSuccess":{"rejectedLogRecords":"0","errorMessage":"warning"}}',
    ],
)
def test_full_otlp_ack(body: bytes) -> None:
    assert backend_ack("logs", body)


@pytest.mark.parametrize(
    "body",
    [
        b'{"partialSuccess":{"rejectedLogRecords":"1"}}',
        b'{"partial_success":{"rejected_spans":1}}',
        b'{"partialSuccess":{"errorMessage":"failed"}}',
        b'{"error":"invalid"}',
        b"\x0a\x02\x08\x01",
        b"[1]",
        b"not-json",
        b"\x0a",
        b'{"status":"error"}',
        b'{"partialSuccess":{"rejectedLogRecords":"0","unknownField":"rejection"}}',
    ],
)
def test_partial_or_unknown_ack_never_discards_data(body: bytes) -> None:
    assert not backend_ack("logs", body)


def test_digest_covers_signal_headers_and_binary_payload() -> None:
    assert digest_of("logs", {}, b"a") != digest_of("traces", {}, b"a")
    assert digest_of("logs", {}, b"a") != digest_of(
        "logs", {"Content-Encoding": "gzip"}, b"a"
    )


def test_sender_example_and_no_defaults(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parents[1] / "sender.json.example"
    settings = Settings.read(example)
    assert settings.mode == "sender"
    assert settings.prometheus_command[0] == "/bin/prometheus"
    data = json.loads(example.read_text())
    del data["capacity_bytes"]
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="exactly"):
        Settings.read(invalid)
