"""Encrypted offline recovery, exact receipt, revocation and writer exclusion."""

import json
from pathlib import Path

import pytest

from aelev_spire.backup import create, restore
from aelev_spire.files import atomic, writer


def fixture(root: Path) -> tuple[list[str], Path]:
    roles = ["server", "operator"]
    for role in roles:
        atomic(
            root / role / "owner.json", {"owner": "infrastructure-spire", "role": role}
        )
    atomic(root / "operator/operations/revoked.json", {"phase": "REVOKED"})
    atomic(root / "server/datastore.sqlite3", b"native-snapshot-test")
    key = root / "backup.key"
    atomic(key, bytes(range(32)))
    return roles, key


def test_backup_restore_preserves_revocation_and_authenticates(tmp_path: Path) -> None:
    roles, key = fixture(tmp_path / "original")
    archive = tmp_path / "state.enc"
    create(tmp_path / "original", roles, archive, key)
    assert b"REVOKED" not in archive.read_bytes()
    target = tmp_path / "restored"
    for role in roles:
        (target / role).mkdir(parents=True)
    receipt = archive.with_name(archive.name + ".receipt.json")
    restore(target, roles, archive, key, receipt)
    assert (
        json.loads((target / "operator/operations/revoked.json").read_bytes())["phase"]
        == "REVOKED"
    )
    with pytest.raises(ValueError, match="EMPTY_TARGET"):
        restore(target, roles, archive, key, receipt)
    raw = archive.read_bytes()
    archive.write_bytes(raw[:-20] + b"bad!" + raw[-16:])
    with pytest.raises(ValueError, match="TRUSTED_RECEIPT"):
        restore(target, roles, archive, key, receipt)


def test_active_writer_or_symlink_refuses_backup(tmp_path: Path) -> None:
    roles, key = fixture(tmp_path)
    archive = tmp_path.parent / (tmp_path.name + ".enc")
    with writer(tmp_path / "server"), pytest.raises(ValueError, match="WRITER_ACTIVE"):
        create(tmp_path, roles, archive, key)
    assert not archive.exists()
    (tmp_path / "server/escape").symlink_to(key)
    with pytest.raises(ValueError, match="SPECIAL_FILE"):
        create(tmp_path, roles, archive, key)
    assert not archive.exists()


def test_previous_trusted_receipt_cannot_restore_different_archive(
    tmp_path: Path,
) -> None:
    roles, key = fixture(tmp_path / "original")
    first, second = tmp_path / "first.enc", tmp_path / "second.enc"
    create(tmp_path / "original", roles, first, key)
    create(tmp_path / "original", roles, second, key)
    target = tmp_path / "target"
    for role in roles:
        (target / role).mkdir(parents=True)
    with pytest.raises(ValueError, match="TRUSTED_RECEIPT"):
        restore(target, roles, first, key, second.with_name("second.enc.receipt.json"))
    assert all(not list((target / role).iterdir()) for role in roles)
