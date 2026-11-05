"""Verify the embedded store's recovery boundary without touching live volumes."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from aelev_platform_ui_client import Outbox
from fastapi import HTTPException

from api import Description, Settings, Status, Store


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        "https://shell.invalid",
        "https://identity.invalid/realms/r",
        "https://identity.invalid/certs",
        "shell",
        "unused-unit-ca",
        str(tmp_path / "metadata.sqlite"),
        120,
        300,
        10,
        65536,
    )


def declaration() -> Description:
    return Description.model_validate(
        {
            "schema_version": 1,
            "module_id": str(uuid4()),
            "revision": 4,
            "title": {"ru": "Владелец", "en": "Owner"},
            "sections": [],
        }
    )


def backup(source: Path, destination: Path) -> None:
    with (
        closing(sqlite3.connect(source)) as reader,
        closing(sqlite3.connect(destination)) as writer,
    ):
        reader.backup(writer)


def snapshot(store: Store) -> dict[str, list[tuple[Any, ...]]]:
    with store.connect() as db:
        return {
            "modules": [
                tuple(row) for row in db.execute("SELECT * FROM modules ORDER BY id")
            ],
            "audit": [
                tuple(row)
                for row in db.execute("SELECT * FROM audit ORDER BY sequence")
            ],
        }


def test_receiver_uses_wal_and_full_synchronous(settings: Settings) -> None:
    store = Store(settings)
    with store.connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_receiver_serializes_complete_sqlite_connection_lifetimes(
    settings: Settings,
) -> None:
    store = Store(settings)
    attempting = threading.Event()
    entered = threading.Event()

    def second_connection() -> None:
        attempting.set()
        with store.connect():
            entered.set()

    with store.connect():
        contender = threading.Thread(target=second_connection)
        contender.start()
        assert attempting.wait(1)
        assert not entered.wait(0.2)
        assert contender.is_alive()
    contender.join(timeout=1)
    assert entered.is_set()


def test_publisher_uses_wal_and_full_synchronous(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "outbox.sqlite", str(uuid4()))
    with closing(outbox.connect()) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_receiver_backup_preserves_history_and_guardrails(
    settings: Settings, tmp_path: Path
) -> None:
    store = Store(settings)
    description = declaration()
    store.publish(description, "publisher")
    report = Status(
        sequence=17,
        state="ONLINE",
        observed_at=datetime.now(timezone.utc).isoformat(),
        active=False,
    )
    store.status(description.module_id, report, "publisher")
    store.delete(description.module_id, "publisher")
    expected = snapshot(store)

    copied = tmp_path / "restored.sqlite"
    backup(Path(settings.database), copied)
    restored = Store(replace(settings, database=str(copied)))
    assert snapshot(restored) == expected
    assert restored.list() == []

    with pytest.raises(HTTPException) as foreign:
        restored.publish(description, "another-publisher")
    assert foreign.value.status_code == 403
    with pytest.raises(HTTPException) as revoked:
        restored.publish(description, "publisher")
    assert revoked.value.status_code == 409

    newer = description.model_copy(update={"revision": description.revision + 1})
    restored.publish(newer, "publisher")
    conflicting = report.model_copy(update={"sequence": 16, "active": True})
    with pytest.raises(HTTPException) as stale:
        restored.status(description.module_id, conflicting, "publisher")
    assert stale.value.status_code == 409
    assert restored.list()[0]["status"]["sequence"] == 17


def test_republication_is_not_receiver_backup(
    settings: Settings, tmp_path: Path
) -> None:
    original = Store(settings)
    description = declaration()
    original.publish(description, "publisher")
    original.status(
        description.module_id,
        Status(
            sequence=17,
            state="ONLINE",
            observed_at=datetime.now(timezone.utc).isoformat(),
            active=False,
        ),
        "publisher",
    )
    original.delete(description.module_id, "publisher")

    fresh = Store(replace(settings, database=str(tmp_path / "fresh.sqlite")))
    fresh.publish(description, "publisher")
    assert original.list() == []
    assert len(fresh.list()) == 1
    assert fresh.list()[0]["status"]["sequence"] == 0
    assert len(snapshot(original)["audit"]) == 3
    assert len(snapshot(fresh)["audit"]) == 1


def test_publisher_backup_preserves_exact_pending_and_withdrawal(
    tmp_path: Path,
) -> None:
    module_id = str(uuid4())
    source = tmp_path / "outbox.sqlite"
    outbox = Outbox(source, module_id)
    observed = datetime.now(timezone.utc).isoformat()
    pending = outbox.prepare("ONLINE", observed)
    outbox.begin_withdrawal()

    copied = tmp_path / "restored-outbox.sqlite"
    backup(source, copied)
    restored = Outbox(copied, module_id)
    assert restored.pending() == pending
    assert restored.withdrawing()
    assert restored.prepare("UNKNOWN", observed, active=False) == pending

    with pytest.raises(ValueError, match="another module"):
        Outbox(copied, str(uuid4()))
    assert restored.pending() == pending
    restored.acknowledge(pending)
    withdrawal = restored.prepare("UNKNOWN", observed, active=False)
    restored.acknowledge(withdrawal)
    assert not restored.withdrawing()
    assert restored.pending() is None
    with closing(restored.connect()) as db:
        assert db.execute("SELECT module, sequence FROM state").fetchone() == (
            module_id,
            2,
        )


def test_receiver_contains_only_its_technical_tables(settings: Settings) -> None:
    store = Store(settings)
    with store.connect() as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert tables == {"modules", "audit"}
