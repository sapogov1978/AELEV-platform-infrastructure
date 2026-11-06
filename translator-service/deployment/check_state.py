"""Read-only fingerprints for a frozen owner database, not a runtime schema gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from translator_service.environment import database_url


def inspect_state() -> dict[str, Any]:
    """Hash every exact public row without exposing source texts or credentials."""
    engine = create_engine(database_url())
    try:
        with engine.connect() as connection:
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout='30s'"))
            head = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            names = list(
                connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname='public' ORDER BY tablename"
                    )
                ).scalars()
            )
            tables: dict[str, Any] = {}
            for name in names:
                if not re.fullmatch(r"[a-z_][a-z_0-9]*", name):
                    raise ValueError("UNEXPECTED_TABLE_IDENTIFIER")
                checksum, count = hashlib.sha256(), 0
                statement = text(
                    f'SELECT row_to_json(t)::text FROM "{name}" t '
                    "ORDER BY row_to_json(t)::text"
                ).execution_options(stream_results=True, yield_per=100)
                for row in connection.execute(statement):
                    checksum.update(row[0].encode() + b"\n")
                    count += 1
                tables[name] = {"rows": count, "sha256": checksum.hexdigest()}
            return {"head": head, "tables": tables}
    finally:
        engine.dispose()


def verify_extension(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Allow exactly the additive 0005 migration, never a partial data rewrite."""
    if before["head"] != "20261013_0004" or after["head"] != "20261014_0005":
        raise ValueError("UNEXPECTED_MIGRATION_HEAD")
    if set(after["tables"]) != set(before["tables"]) | {"translation_segment"}:
        raise ValueError("UNEXPECTED_TABLE_CHANGE")
    if after["tables"]["translation_segment"]["rows"] != 0:
        raise ValueError("SEGMENTS_WRITTEN_BEFORE_ADMISSION")
    for name, value in before["tables"].items():
        if name != "alembic_version" and after["tables"][name] != value:
            raise ValueError("FROZEN_STATE_CHANGED")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--before", type=Path)
    args = parser.parse_args()
    state = inspect_state()
    if args.before is not None:
        verify_extension(json.loads(args.before.read_bytes()), state)
    with args.output.open("xb") as output:
        output.write(json.dumps(state, sort_keys=True).encode() + b"\n")


if __name__ == "__main__":
    main()
