from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

import pytest

import recovery
from test_configuration import _resolved_compose


@pytest.fixture
def restore_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    model = cast(dict[str, Any], _resolved_compose("*"))
    for name in ("keycloak-admin-password", "keycloak-database-password"):
        model["secrets"][name]["file"] = str(tmp_path / name)
    for service in (
        "keycloak",
        "keycloak-status-provision",
        "keycloak-status-publisher",
    ):
        for mount in model["services"][service]["volumes"]:
            if mount["target"] == "/run/secrets/keycloak-https":
                mount["source"] = str(tmp_path / "https")
            elif mount["target"] == "/run/secrets/status":
                mount["source"] = str(tmp_path / "status")
        model["services"][service]["group_add"] = [str(os.getegid())]
    sources, _ = recovery.source_map(model)
    payloads = {"files/" + name: b"synthetic-" + name.encode() for name in sources}
    payloads.update(
        {
            "files/status/credential.json": b"synthetic-status",
            "database.dump": b"synthetic-sql",
            "publisher.sqlite": b"synthetic-journal",
        }
    )
    payloads["files/realm.json"] = sources["realm.json"].read_bytes()
    manifest = {
        "module_id": model["services"]["keycloak-status-publisher"]["environment"][
            "AELEV_INFRASTRUCTURE_KEYCLOAK_UI_MODULE_ID"
        ]
    }
    monkeypatch.setattr(recovery, "configuration", lambda: model)
    monkeypatch.setattr(recovery, "validated_bundle", lambda _: (manifest, payloads))
    return {"model": model, "payloads": payloads, "root": tmp_path}


def test_restore_prepares_mount_roots_before_starting_database(
    restore_target: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    root = restore_target["root"]

    def compose(*arguments: str, payload: bytes | None = None) -> bytes:
        calls.append(arguments)
        if arguments[0] != "ps":
            assert (root / "https/tls.key").read_bytes() == b"synthetic-tls.key"
            assert (root / "status/credential.json").read_bytes() == b"synthetic-status"
            for directory in (root / "https", root / "status"):
                assert directory.stat().st_mode & 0o777 == 0o750
                assert directory.stat().st_gid == os.getegid()
            for name in ("keycloak-admin-password", "keycloak-database-password"):
                assert (root / name).stat().st_mode & 0o777 == 0o640
                assert (root / name).stat().st_gid == os.getegid()
        return b"0" if arguments[0] == "exec" and "psql" in arguments else b""

    monkeypatch.setattr(recovery, "compose", compose)
    recovery.restore(root / "unused-validated-fixture", data_only=True)
    assert calls[0] == ("ps", "-q")
    assert calls[-1] == ("up", "-d", "--wait", "keycloak-database", "keycloak")
    assert any("pg_restore" in call for call in calls)


@pytest.mark.parametrize("conflict", ["status-mode", "existing-key", "alias"])
def test_restore_preflight_writes_no_credentials_on_conflict(
    restore_target: dict[str, Any], monkeypatch: pytest.MonkeyPatch, conflict: str
) -> None:
    root = restore_target["root"]
    if conflict == "status-mode":
        (root / "status").mkdir(mode=0o700)
    elif conflict == "existing-key":
        (root / "https").mkdir(mode=0o750)
        (root / "https/tls.key").write_bytes(b"existing")
    else:
        restore_target["model"]["secrets"]["keycloak-admin-password"]["file"] = str(
            root / "https/tls.key"
        )
    calls: list[tuple[str, ...]] = []

    def compose(*arguments: str, payload: bytes | None = None) -> bytes:
        calls.append(arguments)
        return b""

    monkeypatch.setattr(recovery, "compose", compose)
    with pytest.raises((ValueError, FileExistsError)):
        recovery.restore(root / "unused-validated-fixture")
    assert calls == [("ps", "-q")]
    assert not (root / "keycloak-admin-password").exists()
    assert not (root / "keycloak-database-password").exists()
    assert not (root / "https/tls.crt").exists()
    if conflict == "existing-key":
        assert (root / "https/tls.key").read_bytes() == b"existing"


def test_source_map_accepts_actual_current_directory_recipe() -> None:
    model = cast(dict[str, Any], _resolved_compose("*"))
    sources, status = recovery.source_map(model)
    assert sources["tls.crt"] == recovery.ROOT / "secrets/https/tls.crt"
    assert sources["tls.key"] == recovery.ROOT / "secrets/https/tls.key"
    assert status.is_absolute()
    assert (
        model["services"]["keycloak-status-provision"]["group_add"]
        == model["services"]["keycloak"]["group_add"]
    )


def test_source_map_rejects_ambiguous_tls_bindings() -> None:
    model = cast(dict[str, Any], _resolved_compose("*"))
    model["services"]["keycloak"]["volumes"].append(
        {"source": "/another/certificate", "target": "/opt/keycloak/conf/tls/tls.crt"}
    )
    with pytest.raises(ValueError, match="ambiguous"):
        recovery.source_map(model)


def test_runtime_directory_survives_restrictive_umask(tmp_path: Path) -> None:
    target = tmp_path / "mounted"
    old_umask = os.umask(0o077)
    try:
        recovery.runtime_directory(target, os.getegid(), create=True)
        recovery.write_private(
            target / "key", b"fixture", mode=0o640, group=os.getegid()
        )
    finally:
        os.umask(old_umask)
    assert target.stat().st_mode & 0o777 == 0o750
    assert target.stat().st_gid == os.getegid()
    assert (target / "key").stat().st_mode & 0o777 == 0o640
    recovery.runtime_directory(target, os.getegid(), create=True)


@pytest.mark.parametrize("kind", ["symlink", "mode", "group", "file"])
def test_runtime_directory_rejects_existing_unsafe_target(
    tmp_path: Path, kind: str
) -> None:
    target = tmp_path / "mounted"
    if kind == "symlink":
        other = tmp_path / "other"
        other.mkdir()
        target.symlink_to(other)
    elif kind == "file":
        target.write_bytes(b"existing")
    else:
        target.mkdir(mode=0o750 if kind == "group" else 0o700)
    before = target.lstat()
    gid = os.getegid() + 1 if kind == "group" else os.getegid()
    with pytest.raises(ValueError, match="runtime directory"):
        recovery.runtime_directory(target, gid, create=True)
    after = target.lstat()
    assert (after.st_mode, after.st_gid, after.st_ino) == (
        before.st_mode,
        before.st_gid,
        before.st_ino,
    )


def test_bundle_validation_checks_every_digest(tmp_path: Path) -> None:
    payload = b"database-backup"
    (tmp_path / "database.dump").write_bytes(payload)
    manifest = {
        "schema_version": 1,
        "module_id": "f879b580-5b4c-48c5-a077-2fc96c538a8c",
        "files": {
            "database.dump": {
                "sha256": recovery.digest(payload),
                "size": len(payload),
            }
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    restored, payloads = recovery.validated_bundle(tmp_path)
    assert restored == manifest
    assert payloads == {"database.dump": payload}
    (tmp_path / "database.dump").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="digest mismatch"):
        recovery.validated_bundle(tmp_path)


def test_bundle_rejects_path_traversal(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files": {"../secret": {"sha256": "0" * 64, "size": 0}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="backup path"):
        recovery.validated_bundle(tmp_path)


def test_private_restore_never_overwrites_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "secret"
    recovery.write_private(target, b"first")
    assert target.read_bytes() == b"first"
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        recovery.write_private(target, b"second")
    assert target.read_bytes() == b"first"


def test_source_map_uses_only_declared_owner_files(tmp_path: Path) -> None:
    model = {
        "secrets": {
            "keycloak-admin-password": {"file": str(tmp_path / "admin")},
            "keycloak-database-password": {"file": str(tmp_path / "database")},
        },
        "services": {
            "keycloak": {
                "volumes": [
                    {
                        "source": str(tmp_path / "tls.crt"),
                        "target": "/opt/keycloak/conf/tls/tls.crt",
                    },
                    {
                        "source": str(tmp_path / "tls.key"),
                        "target": "/opt/keycloak/conf/tls/tls.key",
                    },
                    {
                        "source": str(tmp_path / "realm.json"),
                        "target": "/opt/keycloak/data/import/aelev-realm.json",
                    },
                ]
            },
            "keycloak-status-provision": {
                "volumes": [
                    {
                        "source": str(tmp_path / "status"),
                        "target": "/run/secrets/status",
                    }
                ]
            },
        },
    }
    sources, status = recovery.source_map(model)
    assert set(sources) == {
        "admin-password",
        "database-password",
        "tls.crt",
        "tls.key",
        "realm.json",
    }
    assert status == tmp_path / "status"
