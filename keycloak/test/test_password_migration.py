"""Migration tests use synthetic values and never inspect deployment secrets."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import migrate_password_files as migration


def legacy() -> tuple[bytes, dict[str, str]]:
    prefix = migration.PREFIX
    data = f"# preserved\n{prefix}ADMIN_PASSWORD='synthetic-admin'\n{prefix}DATABASE_PASSWORD=synthetic-database\n{prefix}PUBLIC_URL=https://identity.invalid\n".encode()
    return data, {
        "ADMIN_PASSWORD": "synthetic-admin",
        "DATABASE_PASSWORD": "synthetic-database",
        "ADMIN_PASSWORD_FILE": "",
        "DATABASE_PASSWORD_FILE": "",
        "SECRET_GID": "",
    }


def test_plan_preserves_unrelated_configuration(tmp_path: Path) -> None:
    source, values = legacy()
    rewritten, files, gid = migration.plan(tmp_path, source, values)
    assert b"# preserved" in rewritten
    assert b"PUBLIC_URL=https://identity.invalid" in rewritten
    assert b"synthetic-admin" not in rewritten
    assert b"synthetic-database" not in rewritten
    assert b"ADMIN_PASSWORD_FILE=./secrets/admin-password" in rewritten
    assert gid == os.getgid()
    assert set(files.values()) == {b"synthetic-admin", b"synthetic-database"}
    assert not (tmp_path / "secrets").exists()


def test_empty_existing_file_settings_fail_before_writes(tmp_path: Path) -> None:
    source, values = legacy()
    for name in ("ADMIN_PASSWORD_FILE", "DATABASE_PASSWORD_FILE", "SECRET_GID"):
        with pytest.raises(ValueError, match="EMPTY_FILE_DELIVERY"):
            migration.plan(
                tmp_path, source + f"{migration.PREFIX}{name}=\n".encode(), values
            )
    assert not (tmp_path / "secrets").exists()


def test_apply_is_idempotent_and_does_not_rotate(tmp_path: Path) -> None:
    source, values = legacy()
    env = tmp_path / ".env"
    env.write_bytes(source)
    with patch.object(migration, "values", return_value=values):
        assert migration.migrate(tmp_path, False)["applied"] is False
        assert env.read_bytes() == source
        migration.migrate(tmp_path, True)
    current = env.read_bytes()
    values.update(
        ADMIN_PASSWORD="",
        DATABASE_PASSWORD="",
        SECRET_GID=str(os.getgid()),
        ADMIN_PASSWORD_FILE="./secrets/admin-password",
        DATABASE_PASSWORD_FILE="./secrets/database-password",
    )
    with patch.object(migration, "values", return_value=values):
        assert migration.migrate(tmp_path, True)["dotenv_changed"] is False
    assert env.read_bytes() == current
    assert (tmp_path / "secrets/admin-password").read_bytes() == b"synthetic-admin"
    assert (tmp_path / "secrets/admin-password").stat().st_mode & 0o777 == 0o640
    assert env.stat().st_mode & 0o777 == 0o600


def test_existing_different_file_is_preserved(tmp_path: Path) -> None:
    source, values = legacy()
    (tmp_path / "secrets").mkdir()
    path = tmp_path / "secrets/admin-password"
    path.write_bytes(b"preserve")
    with pytest.raises(ValueError, match="MISMATCH"):
        migration.plan(tmp_path, source, values)
    assert path.read_bytes() == b"preserve"


@pytest.mark.parametrize("literal", ["'first\nsecond'", '"first\nsecond"', "first\\"])
def test_multiline_is_rejected_without_partial_rewrite(
    tmp_path: Path, literal: str
) -> None:
    source, values = legacy()
    source = source.replace("'synthetic-admin'".encode(), literal.encode())
    with pytest.raises(ValueError, match="MULTILINE|CONTINUED"):
        migration.plan(tmp_path, source, values)


def test_duplicate_and_shared_secret_paths_are_rejected(tmp_path: Path) -> None:
    source, values = legacy()
    with pytest.raises(ValueError, match="DUPLICATE"):
        migration.plan(tmp_path, source + source, values)
    values.update(
        ADMIN_PASSWORD_FILE="./secrets/shared",
        DATABASE_PASSWORD_FILE="./secrets/shared",
    )
    with pytest.raises(ValueError, match="SEPARATE"):
        migration.plan(tmp_path, source, values)


def test_external_or_symlink_secret_is_not_modified(tmp_path: Path) -> None:
    source, values = legacy()
    values["ADMIN_PASSWORD_FILE"] = "../outside"
    with pytest.raises(ValueError, match="OUTSIDE"):
        migration.plan(tmp_path, source, values)
    values["ADMIN_PASSWORD_FILE"] = "./secrets/admin-password"
    (tmp_path / "secrets").mkdir()
    path = tmp_path / "secrets/admin-password"
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="OUTSIDE"):
        migration.plan(tmp_path, source, values)
