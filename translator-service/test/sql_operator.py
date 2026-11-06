"""Run owner SQL tests in a fresh disposable database on the local own PostgreSQL."""

import argparse
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import URL, create_engine, text

from alembic import command
from alembic.config import Config
from translator_service.environment import database_url

PREFIX = "AELEV_INFRASTRUCTURE_TRANSLATOR_"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=Path("test/sql-operator-result.md")
    )
    args = parser.parse_args()
    if (
        os.environ[PREFIX + "ENVIRONMENT"] != "local"
        or os.environ[PREFIX + "DATABASE_HOST"] != "translator-database"
        or os.environ[PREFIX + "DATABASE_SSLMODE"] != "disable"
    ):
        raise RuntimeError("SQL_TEST_REQUIRES_LOCAL_OWN_DATABASE")
    for name in (
        "HOST",
        "PORT",
        "NAME",
        "USER",
        "SSLMODE",
        "CA_FILE",
        "CLIENT_CERT_FILE",
        "CLIENT_KEY_FILE",
    ):
        os.environ["AELEV_TRANSLATOR_DATABASE_" + name] = os.environ[
            PREFIX + "DATABASE_" + name
        ]
    os.environ["AELEV_TRANSLATOR_DATABASE_PASSWORD_FILE"] = (
        "/run/secrets/translator-database-password"
    )
    os.environ["AELEV_TRANSLATOR_DATABASE_SECRET_ROOT"] = "/run/secrets"
    admin_url = database_url()
    assert isinstance(admin_url, URL)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    name = "translator_test_operator_" + uuid.uuid4().hex[:12]
    created = False
    code = 1
    cleaned = False
    try:
        with admin.connect() as connection:
            assert not connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": name}
            ).scalar()
            connection.execute(text('CREATE DATABASE "' + name + '"'))
            created = True
        os.environ["AELEV_TRANSLATOR_DATABASE_NAME"] = name
        # The URL remains only in this process environment, never logs or arguments.
        os.environ["AELEV_TRANSLATOR_TEST_DATABASE_URL"] = admin_url.set(
            database=name
        ).render_as_string(hide_password=False)
        command.upgrade(Config("alembic.ini"), "head")
        code = pytest.main(["-q", "-p", "no:cacheprovider", "test/test_repository.py"])
    finally:
        if created:
            with admin.connect() as connection:
                # This new generated name is never the own working database.
                connection.execute(text('DROP DATABASE "' + name + '" WITH (FORCE)'))
                cleaned = not connection.execute(
                    text("SELECT 1 FROM pg_database WHERE datname=:name"),
                    {"name": name},
                ).scalar()
        admin.dispose()
        verdict = "PASS" if code == 0 and cleaned else "FAIL"
        args.report.write_text(
            "# translator-service — изолированные SQL-проверки\n\n"
            f"Дата UTC: {datetime.now(UTC).isoformat()}. Итог: {verdict}.\n\n"
            "Собственная локальная PostgreSQL, новая одноразовая база, "
            "штатные Alembic-миграции и test/test_repository.py. "
            f"Pytest exit code: {code}; созданная база удалена: {cleaned}.\n\n"
            "Рабочая база и Kafka не изменялись; пароль/URL не выводились. "
            "Новый тест проверяет, что операторский результат недоступен "
            "до ACK outbox и при несовпадающем digest исходного запроса.\n"
        )
    return 0 if code == 0 and cleaned else 1


if __name__ == "__main__":
    raise SystemExit(main())
