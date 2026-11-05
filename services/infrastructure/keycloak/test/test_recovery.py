from __future__ import annotations

import json
from pathlib import Path

import pytest

import recovery


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
