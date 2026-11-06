"""Verify the translator-owned Alembic head; never create DDL at startup."""

from sqlalchemy import Engine, text

HEAD = "20261014_0005"


def verify_schema(engine: Engine) -> None:
    with engine.connect() as connection:
        if (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            != HEAD
        ):
            raise RuntimeError("TRANSLATOR_SCHEMA_MISMATCH")
