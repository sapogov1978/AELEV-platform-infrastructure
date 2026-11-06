"""Real local X.509 issuance and failure/reconciliation, never runtime secrets."""

from __future__ import annotations

import json
import base64
from pathlib import Path
import shutil
import subprocess
from typing import Any
from unittest.mock import Mock

import pytest

import renewal as owner


def test_delivery_is_public_only_and_revocation_sends_no_leaf(
    bundle: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = bundle
    owner.enroll(root, config)
    module = Mock()
    module.DeliveryError = ValueError
    monkeypatch.setattr(owner, "delivery_operator", lambda: module)
    state_before = (root / "state.json").read_bytes()
    owner.bind_delivery(root, {"consumer_owner": config["consumer_owner"]})
    assert (root / "state.json").read_bytes() == state_before
    module.bind.assert_called_once()
    (root / "delivery.json").write_text("{}")
    assert owner.once(root)["healthy"]
    sent = module.send.call_args.args[-1]
    assert len(sent) == 1 and set(sent[0]) == {
        "id",
        "certificate_base64",
        "issuer_certificate_base64",
    }
    owner.revoke(root)
    assert module.send.call_args.args[-1] == []
    with pytest.raises(owner.RenewalError):
        owner.bind_delivery(root, {"consumer_owner": config["consumer_owner"]})


def test_failed_delivery_is_not_reported_healthy(
    bundle: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = bundle
    owner.enroll(root, config)
    module = Mock()
    module.DeliveryError = ValueError
    module.send.side_effect = ValueError("isolated failure")
    monkeypatch.setattr(owner, "delivery_operator", lambda: module)
    (root / "delivery.json").write_text("{}")
    result = owner.once(root)
    assert (
        not result["healthy"] and result["code"] == "TELEMETRY_RENEWAL_DELIVERY_FAILED"
    )
    assert not json.loads(Path(config["report"]).read_bytes())["healthy"]


@pytest.fixture
def bundle(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    executable = shutil.which("openssl")
    if executable is None:
        pytest.skip("OpenSSL required")
    binary = Path(executable).resolve()
    issuer, client, reports = (
        tmp_path / name for name in ("issuer", "client", "reports")
    )
    for path in (issuer, client, reports):
        path.mkdir(mode=0o700)

    def run(*args: str) -> None:
        subprocess.run([str(binary), *args], capture_output=True, check=True)

    run(
        "req",
        "-new",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "365",
        "-subj",
        "/CN=isolated-telemetry-test-ca",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout",
        str(issuer / "ca.key"),
        "-out",
        str(issuer / "ca.crt"),
    )
    run(
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        "/CN=isolated-telemetry-client",
        "-keyout",
        str(client / "client.key"),
        "-out",
        str(client / "client.csr"),
    )
    extension = tmp_path / "extensions.cnf"
    extension.write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=clientAuth\nsubjectAltName=DNS:isolated-client\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n"
    )
    run(
        "x509",
        "-req",
        "-in",
        str(client / "client.csr"),
        "-CA",
        str(issuer / "ca.crt"),
        "-CAkey",
        str(issuer / "ca.key"),
        "-set_serial",
        "1234",
        "-days",
        "1",
        "-sha256",
        "-extfile",
        str(extension),
        "-out",
        str(client / "client.crt"),
    )
    for path in (client / "client.key", issuer / "ca.key"):
        path.chmod(0o600)
    config: dict[str, Any] = {
        "schema_version": 1,
        "environment": "test",
        "consumer_owner": "isolated-test",
        "openssl": str(binary),
        "report": str(reports / "status.json"),
        "certificates": [
            {
                "id": "client",
                "certificate": str(client / "client.crt"),
                "key": str(client / "client.key"),
                "issuer_certificate": str(issuer / "ca.crt"),
                "issuer_key": str(issuer / "ca.key"),
            }
        ],
    }
    return tmp_path / "state", config


def test_issue_preserves_key_trust_subject_and_extensions(
    bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, config = bundle
    entry = config["certificates"][0]
    old = owner.material(config, entry, Path(entry["certificate"]))
    bound = owner.bound(config, entry)
    metadata = Path(entry["certificate"]).stat()
    owner.enroll(root, config)
    result = owner.once(root)
    fresh = owner.material(config, entry, Path(entry["certificate"]))
    assert result["healthy"] and fresh["sha256"] != old["sha256"]
    assert fresh["extensions_sha256"] == old["extensions_sha256"]
    assert fresh["subject_sha256"] == old["subject_sha256"]
    assert owner.bound(config, entry) == bound
    assert fresh["expires_at"] - old["expires_at"] > 88 * 86400
    assert Path(entry["certificate"]).stat().st_uid == metadata.st_uid
    assert owner.once(root)["healthy"]
    assert owner.read(root / "state.json")["certificates"]["client"]["issued"] == 1
    assert str(root.parent) not in json.dumps(result)


def test_fresh_receipt_is_a_fail_closed_installation_gate(
    bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, config = bundle
    owner.enroll(root, config)
    result = owner.once(root)
    receipt = owner.check(root)
    assert result["renewal_healthy"] is True and result["ready"] is True
    assert receipt["code"] == "TELEMETRY_RENEWAL_INSTALLATION_READY"
    report = json.loads(Path(config["report"]).read_bytes())
    report["checked_at"] -= 181
    Path(config["report"]).write_text(json.dumps(report))
    with pytest.raises(owner.RenewalError, match="RECEIPT_NOT_READY"):
        owner.check(root)


def test_explicit_schema_two_ca_transition_preserves_leaf_profile(
    bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, config = bundle
    entry = config["certificates"][0]
    previous = entry["issuer_certificate"]
    candidate = root.parent / "candidate-issuer"
    candidate.mkdir(mode=0o700)
    subprocess.run(
        [
            config["openssl"],
            "req",
            "-new",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "365",
            "-subj",
            "/CN=isolated-telemetry-test-ca-next",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",
            str(candidate / "ca.key"),
            "-out",
            str(candidate / "ca.crt"),
        ],
        capture_output=True,
        check=True,
    )
    (candidate / "ca.key").chmod(0o600)
    config["schema_version"] = 2
    entry.update(
        previous_issuer_certificate=previous,
        issuer_certificate=str(candidate / "ca.crt"),
        issuer_key=str(candidate / "ca.key"),
    )
    before = owner.material(
        config,
        entry,
        Path(entry["certificate"]),
        issuer=entry["previous_issuer_certificate"],
    )
    owner.enroll(root, config)
    result = owner.once(root)
    assert result["ready"] is True, result["certificates"][0]["code"]
    after = owner.material(config, entry, Path(entry["certificate"]))
    assert before["subject_sha256"] == after["subject_sha256"]
    assert before["profile_extensions_sha256"] == after["profile_extensions_sha256"]


def test_explicit_schema_three_normalizes_legacy_leaf_once(
    bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, config = bundle
    entry = config["certificates"][0]
    extension = root.parent / "legacy-extensions.cnf"
    extension.write_text(
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=clientAuth\n"
        "subjectAltName=DNS:isolated-client\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid\n"
    )
    subprocess.run(
        [
            config["openssl"],
            "x509",
            "-req",
            "-in",
            str(root.parent / "client" / "client.csr"),
            "-CA",
            entry["issuer_certificate"],
            "-CAkey",
            entry["issuer_key"],
            "-set_serial",
            "5678",
            "-days",
            "1",
            "-sha256",
            "-extfile",
            str(extension),
            "-out",
            entry["certificate"],
        ],
        capture_output=True,
        check=True,
    )
    config["schema_version"] = 3
    entry["initial_profile"] = "legacy-no-basic-constraints"
    before = owner.material(
        config,
        entry,
        Path(entry["certificate"]),
        initial_profile="legacy-no-basic-constraints",
    )
    assert before["legacy_profile"] == "legacy-no-basic-constraints"
    owner.enroll(root, config)
    assert owner.once(root, force=True)["ready"] is True
    after = owner.material(config, entry, Path(entry["certificate"]))
    assert after["legacy_profile"] is None
    assert after["subject_sha256"] == before["subject_sha256"]
    assert after["scope_extensions_sha256"] == before["scope_extensions_sha256"]
    state = owner.read(root / "state.json")["certificates"]["client"]
    assert "legacy_profile_active" not in state


def test_revocation_not_restored_by_registration(
    bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, config = bundle
    owner.enroll(root, config)
    owner.revoke(root)
    result = owner.once(root, force=True)
    assert not result["healthy"] and result["code"] == "TELEMETRY_RENEWAL_REVOKED"
    with pytest.raises(owner.RenewalError, match="REVOKED_OR_CHANGED"):
        owner.enroll(root, config, allow_expired=True)


def test_changed_key_blocks_issuance(bundle: tuple[Path, dict[str, Any]]) -> None:
    root, config = bundle
    owner.enroll(root, config)
    entry = config["certificates"][0]
    Path(entry["key"]).write_bytes(b"isolated-invalid-key")
    result = owner.once(root, force=True)
    assert not result["healthy"]
    assert (
        result["certificates"][0]["code"] == "TELEMETRY_RENEWAL_BOUND_MATERIAL_CHANGED"
    )
    assert owner.read(root / "state.json")["certificates"]["client"]["issued"] == 0


def test_interrupted_publication_reconciles_without_reissuing(
    bundle: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = bundle
    owner.enroll(root, config)
    original = owner.atomic

    def interrupted(path: Path, data: bytes, mode: int = 0o600) -> None:
        original(path, data, mode)
        if str(path) == config["certificates"][0]["certificate"]:
            raise OSError("isolated interruption after publication")

    monkeypatch.setattr(owner, "atomic", interrupted)
    assert not owner.once(root, force=True)["healthy"]
    monkeypatch.setattr(owner, "atomic", original)
    assert owner.once(root)["healthy"]
    assert owner.read(root / "state.json")["certificates"]["client"]["issued"] == 1


def test_production_cannot_use_local_fallback(
    bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, config = bundle
    config["environment"] = "production"
    with pytest.raises(owner.RenewalError, match="PRODUCTION_CA_FORBIDDEN"):
        owner.enroll(root, config)


def test_public_export_never_reads_private_keys_or_changes_state(
    bundle: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config = bundle
    owner.enroll(root, config)
    entry = config["certificates"][0]
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }
    original = Path.read_bytes
    forbidden = {Path(entry["issuer_key"]), Path(entry["key"])}

    def public_read(path: Path) -> bytes:
        assert path not in forbidden, "public export attempted a private-key read"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", public_read)
    first = owner.export_issuers(root, "isolated-test")
    assert first == owner.export_issuers(root, "isolated-test")
    assert first["code"] == "TELEMETRY_RENEWAL_PUBLIC_ISSUERS_EXPORTED"
    assert len(first["certificates"]) == 1
    result = first["certificates"][0]
    assert (
        base64.b64decode(result["issuer_certificate_base64"], validate=True)
        == Path(entry["issuer_certificate"]).read_bytes()
    )
    assert result["issuer_sha256"] == owner.sha(
        base64.b64decode(result["issuer_certificate_base64"])
    )
    assert str(root.parent) not in json.dumps(first)
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}
    assert owner.read(root / "state.json")["certificates"]["client"]["issued"] == 0


@pytest.mark.parametrize(
    "defect", ["owner", "revoked", "pending", "ca", "leaf", "expired"]
)
def test_public_export_fails_closed_without_repairing_approval(
    bundle: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    root, config = bundle
    owner.enroll(root, config)
    entry = config["certificates"][0]
    if defect == "revoked":
        owner.revoke(root)
    elif defect == "pending":
        state = owner.read(root / "state.json")
        state["certificates"]["client"]["intent"] = {"unconfirmed": True}
        owner.persist(root, state)
    elif defect == "ca":
        Path(entry["issuer_certificate"]).write_bytes(b"different public authority")
    elif defect == "leaf":
        Path(entry["certificate"]).write_bytes(b"different public leaf")
    elif defect == "expired":
        monkeypatch.setattr(owner, "expiry", lambda *_args: 0)
    before = (root / "state.json").read_bytes()
    with pytest.raises(owner.RenewalError):
        owner.export_issuers(
            root, "different-owner" if defect == "owner" else "isolated-test"
        )
    assert (root / "state.json").read_bytes() == before
