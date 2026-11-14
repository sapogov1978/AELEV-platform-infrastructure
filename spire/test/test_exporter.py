"""Delivery ownership, exact identity and durable generation invariants."""

import json
import os
import stat
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


def test_declared_public_configuration_mountpoints_preserve_consumer_bytes(
    tmp_path: Path,
) -> None:
    plan = {**PLAN, "policy_mountpoints": ["identity.json", "network.json"]}
    root = tmp_path / "svid"
    Exporter(root, plan)
    public = root / "network.json"
    assert public.read_bytes() == b""
    assert stat.S_IMODE(public.stat().st_mode) == 0o444
    public.chmod(0o600)
    public.write_bytes(b'{"owner-selected":"configuration"}')
    before = public.stat().st_ino
    Exporter(root, plan)
    assert public.stat().st_ino == before
    assert public.read_bytes() == b'{"owner-selected":"configuration"}'


@pytest.mark.parametrize(
    "names",
    [
        ["identity.json", "current.json"],
        ["identity.json", "owner.json"],
        ["identity.json", "../network.json"],
        ["identity.json"] * 2,
        ["network.json"],
        ["identity.json", "private.key"],
    ],
)
def test_policy_mountpoints_cannot_replace_delivery_or_escape_root(
    tmp_path: Path, names: list[str]
) -> None:
    root = tmp_path / "svid"
    with pytest.raises(ValueError, match="POLICY_MOUNTPOINT_INVALID"):
        Exporter(root, {**PLAN, "policy_mountpoints": names})
    assert not root.exists()


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


def test_consumer_policy_is_preserved_across_delivery_and_repeat(
    tmp_path: Path, credential: tuple[bytes, bytes, bytes]
) -> None:
    root = tmp_path / "svid"
    Exporter(root, PLAN)
    mountpoint = root / "identity.json"
    assert mountpoint.read_bytes() == b""
    assert stat.S_IMODE(mountpoint.stat().st_mode) == 0o444
    mountpoint.chmod(0o600)
    mountpoint.write_bytes(b'{"consumer-owned":"opaque"}')
    before = mountpoint.stat()
    delivery = Exporter(root, PLAN)
    delivery.publish(*credential)
    delivery.unavailable("SPIRE_WORKLOAD_NOT_AUTHORIZED")
    Exporter(root, PLAN)
    after = mountpoint.stat()
    assert mountpoint.read_bytes() == b'{"consumer-owned":"opaque"}'
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
    )


@pytest.mark.parametrize("kind", ["directory", "symlink", "fifo"])
def test_nonregular_policy_mountpoint_is_rejected_without_following(
    tmp_path: Path, kind: str
) -> None:
    root = tmp_path / "svid"
    Exporter(root, PLAN)
    mountpoint = root / "identity.json"
    mountpoint.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"must remain unchanged")
    if kind == "directory":
        mountpoint.mkdir()
    elif kind == "symlink":
        mountpoint.symlink_to(outside)
    else:
        os.mkfifo(mountpoint)
    before = mountpoint.lstat()
    with pytest.raises(ValueError, match="SPIRE_SVID_POLICY_MOUNTPOINT_INVALID"):
        Exporter(root, PLAN)
    after = mountpoint.lstat()
    assert (after.st_ino, after.st_mode) == (before.st_ino, before.st_mode)
    assert outside.read_bytes() == b"must remain unchanged"
