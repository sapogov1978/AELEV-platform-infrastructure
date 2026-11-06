"""Verify an offline sender snapshot through its selected public SDK image."""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Protocol, cast


class JournalStatus(Protocol):
    def status(self) -> dict[str, int]: ...


class JournalFactory(Protocol):
    def __call__(self, directory: Path, capacity: int) -> JournalStatus: ...


def fingerprint(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    maximum = 0
    pending = 0
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        for row in db.execute(
            "SELECT sequence,id,kind,headers,body,digest,created_ns,projected "
            "FROM records ORDER BY sequence"
        ):
            (
                sequence,
                identifier,
                kind,
                headers,
                body,
                body_digest,
                created,
                projected,
            ) = row
            metadata = json.dumps(
                [sequence, identifier, kind, headers, body_digest, created],
                separators=(",", ":"),
            ).encode()
            digest.update(len(metadata).to_bytes(8) + metadata)
            digest.update(len(body).to_bytes(8) + body)
            count += 1
            maximum = max(maximum, sequence)
            pending += int(projected == 0)
    return {
        "records": count,
        "maximum_sequence": maximum,
        "pending": pending,
        "immutable_sha256": digest.hexdigest(),
    }


def main() -> None:
    sys.path.insert(0, "/opt/aelev")
    journal_factory = cast(JournalFactory, importlib.import_module("delivery").Journal)

    source = Path("/snapshot")
    checked = Path("/tmp/snapshot-check")
    shutil.copytree(source, checked)
    before = fingerprint(checked / "delivery.sqlite3")
    journal = journal_factory(checked, 1024**3)
    after = fingerprint(checked / "delivery.sqlite3")
    assert before == after
    assert journal.status()["records"] == before["records"]
    assert sqlite3.sqlite_version_info == (3, 53, 4)
    print(
        json.dumps(
            {
                "snapshot": before,
                "sqlite": sqlite3.sqlite_version,
                "sdk_open_preserved_records": True,
            }
        )
    )


if __name__ == "__main__":
    main()
