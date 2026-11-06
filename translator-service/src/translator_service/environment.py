"""Translator-owned configuration, without another service's aliases."""

import os
from os import getenv
from pathlib import Path

from sqlalchemy import URL

__all__ = ["database_sslmode", "database_url", "getenv"]


def _secret_path(environment_name: str, root_name: str) -> str:
    filename = os.environ.get(environment_name, "NONE")
    if filename == "NONE":
        return ""
    if not filename or Path(filename).name != filename:
        raise RuntimeError("TRANSLATOR_SECRET_FILENAME_INVALID")
    return str(Path(os.environ[root_name]) / filename)


def database_sslmode() -> str:
    """Return the configured PostgreSQL transport policy."""
    return os.environ["AELEV_TRANSLATOR_DATABASE_SSLMODE"]


def database_url() -> str | URL:
    """Build a SQLAlchemy URL from translator-owned settings and a file secret."""
    password_path = Path(os.environ["AELEV_TRANSLATOR_DATABASE_PASSWORD_FILE"])
    password = password_path.read_text(encoding="utf-8").strip()
    if not password:
        raise RuntimeError("TRANSLATOR_DATABASE_PASSWORD_EMPTY")
    query = {"sslmode": database_sslmode()}
    for key, environment_name in {
        "sslrootcert": "AELEV_TRANSLATOR_DATABASE_CA_FILE",
        "sslcert": "AELEV_TRANSLATOR_DATABASE_CLIENT_CERT_FILE",
        "sslkey": "AELEV_TRANSLATOR_DATABASE_CLIENT_KEY_FILE",
    }.items():
        value = _secret_path(environment_name, "AELEV_TRANSLATOR_DATABASE_SECRET_ROOT")
        if value:
            query[key] = value
    return URL.create(
        "postgresql+psycopg",
        username=os.environ["AELEV_TRANSLATOR_DATABASE_USER"],
        password=password,
        host=os.environ["AELEV_TRANSLATOR_DATABASE_HOST"],
        port=int(os.environ["AELEV_TRANSLATOR_DATABASE_PORT"]),
        database=os.environ["AELEV_TRANSLATOR_DATABASE_NAME"],
        query=query,
    )
