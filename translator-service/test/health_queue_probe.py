"""Read-only release-head, queue-command and exact selected-model evidence."""

import json
import os
from pathlib import Path

from sqlalchemy import text

from translator_service.index_translation_provider import read_translator_lock
from translator_service.runtime import repository
from translator_service.schema import HEAD


def main() -> None:
    repo = repository()
    try:
        with repo.engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout='3s'"))
            head = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert head == HEAD
            control = dict(
                connection.execute(
                    text("SELECT desired_state,revision FROM translation_control")
                )
                .mappings()
                .one()
            )
            counts = {
                row[0]: row[1]
                for row in connection.execute(
                    text("SELECT state,count(*) FROM translation_job GROUP BY state")
                )
            }
        model, _ = read_translator_lock(Path(os.environ["AELEV_TRANSLATOR_LOCK_PATH"]))
        print(
            json.dumps(
                {
                    "head": head,
                    "control": control,
                    "jobs": counts,
                    "translator_ref": model,
                },
                sort_keys=True,
            )
        )
    finally:
        repo.engine.dispose()


if __name__ == "__main__":
    main()
