"""Delivery ownership, exact identity and durable generation invariants."""

import json
from pathlib import Path
from typing import Any

import pytest

from aelev_spire import exporter
from aelev_spire.exporter import Exporter, verify

PLAN = {
    "environment": "test",
    "trust_domain": "test.example",
    "spiffe_id": "spiffe://test.example/fixture-owner/component/instance",
    "svid_ttl_seconds": 20,
}


def test_generation_is_atomic_and_repeated_material_does_not_rotate(
    tmp_path: Path, credential: tuple[bytes, bytes, bytes]
) -> None:
    delivery = Exporter(tmp_path / "svid", PLAN)
    first = delivery.publish(*credential)
    assert first["state"] == "READY"
    assert delivery.publish(*credential)["generation"] == first["generation"]
    assert len(list((delivery.root / "generations").iterdir())) == 1
    delivery.unavailable("SPIRE_WORKLOAD_NOT_AUTHORIZED")
    assert (
        json.loads((delivery.root / "current.json").read_bytes())["state"]
        == "UNAVAILABLE"
    )


def test_wrong_identity_or_key_is_not_delivered(
    tmp_path: Path, credential: tuple[bytes, bytes, bytes]
) -> None:
    with pytest.raises(ValueError, match="SCOPE_INVALID"):
        verify(*credential, "spiffe://test.example/other-owner/component/instance")
    delivery = Exporter(
        tmp_path / "svid",
        {**PLAN, "spiffe_id": "spiffe://test.example/other-owner/component/instance"},
    )
    with pytest.raises(ValueError):
        delivery.publish(*credential)
    assert not (delivery.root / "current.json").exists()


def test_known_partial_is_resumed_without_new_keys(
    tmp_path: Path,
    credential: tuple[bytes, bytes, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = Exporter(tmp_path / "svid", PLAN)
    original = exporter.atomic

    def fail_bundle(
        path: Path, value: bytes | dict[str, Any], mode: int = 0o600
    ) -> None:
        if path.name == "bundle.pem":
            raise OSError("isolated write interruption")
        original(path, value, mode)

    with monkeypatch.context() as patch:
        patch.setattr(exporter, "atomic", fail_bundle)
        with pytest.raises(OSError):
            delivery.publish(*credential)
    assert not (delivery.root / "current.json").exists()
    result = delivery.publish(*credential)
    assert result["state"] == "READY"
    assert len(list((delivery.root / "generations").iterdir())) == 1


def test_foreign_volume_is_not_adopted(tmp_path: Path) -> None:
    root = tmp_path / "foreign"
    root.mkdir()
    (root / "keep").write_text("not an installation")
    with pytest.raises(ValueError, match="UNKNOWN_STATE_PRESERVED"):
        Exporter(root, PLAN)
    assert (root / "keep").read_text() == "not an installation"
