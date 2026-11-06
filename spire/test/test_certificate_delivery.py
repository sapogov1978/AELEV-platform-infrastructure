"""The SPIRE consumer applies complete public renewal output and keeps overlap."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from aelev_spire.certificate_delivery import (
    PROFILE,
    STATUS_PUBLISHER_PROFILE,
    TRUST,
    encoded,
    run,
)


def authority(
    name: str, days: int
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, None, None),
            True,
        )
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def leaf(
    name: str,
    key: ec.EllipticCurvePrivateKey,
    issuer_key: ec.EllipticCurvePrivateKey,
    issuer: x509.Certificate,
    days: int,
) -> x509.Certificate:
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, name)]))
        .issuer_name(issuer.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]), False
        )
        .sign(issuer_key, hashes.SHA256())
    )


def pem(value: x509.Certificate) -> bytes:
    return value.public_bytes(serialization.Encoding.PEM)


def test_complete_delivery_updates_leafs_and_retains_old_trust(tmp_path: Path) -> None:
    root = tmp_path
    for role, marker in {
        "observer-secrets": "observation-observer",
        "sender-secrets": "observation-sender",
    }.items():
        directory = root / role
        directory.mkdir()
        (directory / "owner.json").write_text(
            json.dumps({"owner": "infrastructure-spire", "role": marker})
        )
    (root / "certificate-renewal-status").mkdir()
    # A CA key rollover commonly preserves the subject. The old signer must be
    # skipped after signature verification rather than aborting the bundle.
    old_key, old_ca = authority("local", 120)
    new_key, new_ca = authority("local", 365)
    records = []
    destinations = []
    certificates = []
    for name, (role, filename, key_name) in PROFILE.items():
        private = ec.generate_private_key(ec.SECP256R1())
        current = leaf(name, private, old_key, old_ca, 1)
        candidate = leaf(name, private, new_key, new_ca, 90)
        directory = root / role
        (directory / filename).write_bytes(pem(current))
        (directory / key_name).write_bytes(
            private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        raw = pem(candidate)
        records.append(
            {
                "id": name,
                "healthy": True,
                "certificate_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        destinations.append(
            {
                "id": name,
                "volume": "fixture_" + role,
                "role": role,
                "file": filename,
            }
        )
        certificates.append(
            {
                "id": name,
                "certificate_base64": base64.b64encode(raw).decode(),
                "issuer_certificate_base64": base64.b64encode(
                    pem(old_ca) + pem(new_ca)
                ).decode(),
            }
        )
    for role, filename in {target for targets in TRUST.values() for target in targets}:
        (root / role / filename).write_bytes(pem(old_ca))
    report = {
        "owner": "telemetry-certificate-renewal",
        "consumer_owner": "infrastructure-spire",
        "schema_version": 1,
        "healthy": True,
        "checked_at": datetime.now(UTC).timestamp(),
        "certificates": records,
    }
    value = {
        "owner": report["owner"],
        "schema_version": 2,
        "consumer_owner": "infrastructure-spire",
        "installation_id": "fixture",
        "destinations": destinations,
        "report_destination": {
            "volume": "fixture_certificate-renewal-status",
            "role": "certificate-renewal-status",
            "file": "status.json",
        },
        "report": report,
        "certificates": certificates,
    }
    result = run(value, root)
    assert result["code"] == "CERTIFICATE_DELIVERY_VERIFIED"
    assert run(value, root) == result
    assert (root / "certificate-renewal-status/status.json").read_bytes() == encoded(
        report
    )
    trust = (root / "sender-secrets/server-ca.crt").read_bytes()
    assert len(x509.load_pem_x509_certificates(trust)) == 2


def test_status_delivery_is_a_separate_exact_profile(tmp_path: Path) -> None:
    secrets = tmp_path / "status-secrets"
    receipts = tmp_path / "certificate-renewal-status"
    secrets.mkdir()
    receipts.mkdir()
    (secrets / "owner.json").write_text(
        json.dumps({"owner": "infrastructure-spire", "role": "observation-status"})
    )
    issuer_key, issuer = authority("status-issuer", 365)
    destinations = []
    certificates = []
    records = []
    for name, (role, filename, key_name) in STATUS_PUBLISHER_PROFILE.items():
        private = ec.generate_private_key(ec.SECP256R1())
        current = leaf(name, private, issuer_key, issuer, 1)
        candidate = leaf(name, private, issuer_key, issuer, 90)
        (secrets / filename).write_bytes(pem(current))
        (secrets / key_name).write_bytes(
            private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        raw = pem(candidate)
        destinations.append(
            {
                "id": name,
                "volume": "fixture_status-secrets",
                "role": role,
                "file": filename,
            }
        )
        certificates.append(
            {
                "id": name,
                "certificate_base64": base64.b64encode(raw).decode(),
                "issuer_certificate_base64": base64.b64encode(pem(issuer)).decode(),
            }
        )
        records.append(
            {
                "id": name,
                "healthy": True,
                "certificate_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    report = {
        "owner": "telemetry-certificate-renewal",
        "consumer_owner": "infrastructure-spire",
        "schema_version": 1,
        "healthy": True,
        "checked_at": datetime.now(UTC).timestamp(),
        "certificates": records,
    }
    value = {
        "owner": report["owner"],
        "schema_version": 2,
        "consumer_owner": "infrastructure-spire",
        "installation_id": "fixture",
        "destinations": destinations,
        "report_destination": {
            "volume": "fixture_certificate-renewal-status",
            "role": "certificate-renewal-status",
            "file": "status-publisher.json",
        },
        "report": report,
        "certificates": certificates,
    }
    assert run(value, tmp_path)["code"] == "CERTIFICATE_DELIVERY_VERIFIED"
    assert (receipts / "status-publisher.json").read_bytes() == encoded(report)


def test_unrelated_profile_lock_does_not_block_delivery(tmp_path: Path) -> None:
    secrets = tmp_path / "status-secrets"
    receipts = tmp_path / "certificate-renewal-status"
    secrets.mkdir()
    receipts.mkdir()
    (secrets / "owner.json").write_text(
        json.dumps({"owner": "infrastructure-spire", "role": "observation-status"})
    )
    issuer_key, issuer = authority("status-issuer", 365)
    name, (role, filename, key_name) = next(iter(STATUS_PUBLISHER_PROFILE.items()))
    private = ec.generate_private_key(ec.SECP256R1())
    current = leaf(name, private, issuer_key, issuer, 1)
    candidate = leaf(name, private, issuer_key, issuer, 90)
    (secrets / filename).write_bytes(pem(current))
    (secrets / key_name).write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    raw = pem(candidate)
    checked_at = datetime.now(UTC).timestamp()
    report = {
        "owner": "telemetry-certificate-renewal",
        "consumer_owner": "infrastructure-spire",
        "schema_version": 1,
        "healthy": True,
        "checked_at": checked_at,
        "certificates": [
            {
                "id": name,
                "healthy": True,
                "certificate_sha256": hashlib.sha256(raw).hexdigest(),
            }
        ],
    }
    value = {
        "owner": report["owner"],
        "schema_version": 2,
        "consumer_owner": "infrastructure-spire",
        "installation_id": "fixture",
        "destinations": [
            {
                "id": name,
                "volume": "fixture_status-secrets",
                "role": role,
                "file": filename,
            }
        ],
        "report_destination": {
            "volume": "fixture_certificate-renewal-status",
            "role": "certificate-renewal-status",
            "file": "status-publisher.json",
        },
        "report": report,
        "certificates": [
            {
                "id": name,
                "certificate_base64": base64.b64encode(raw).decode(),
                "issuer_certificate_base64": base64.b64encode(pem(issuer)).decode(),
            }
        ],
    }
    unrelated = os.open(
        receipts / ".status-query.json.lock", os.O_CREAT | os.O_RDWR, 0o600
    )
    try:
        fcntl.flock(unrelated, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run(value, tmp_path)["code"] == "CERTIFICATE_DELIVERY_VERIFIED"
    finally:
        os.close(unrelated)
