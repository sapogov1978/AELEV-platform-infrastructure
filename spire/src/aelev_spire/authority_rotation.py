"""Prepare an explicit overlapping local CA generation; never activate it."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .certificate_delivery import atomic as atomic_preserving
from .files import atomic, read, reserve, writer

OWNER = "infrastructure-spire"
AUTHORITIES = ("query", "server", "write")
TRUST_TARGETS = {
    "observer/query-ca.crt": ("query",),
    "observer/sender-ca.crt": ("server",),
    "sender/server-ca.crt": ("server",),
    "sender/local-write-ca.crt": ("write",),
    "sender/local-query-ca.crt": ("query", "server"),
    "status/query-ca.crt": ("server",),
}


def _certificate(path: Path) -> x509.Certificate:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("SPIRE_AUTHORITY_ROTATION_SOURCE_INVALID")
    return x509.load_pem_x509_certificate(path.read_bytes())


def _fingerprint(value: x509.Certificate) -> str:
    return value.fingerprint(hashes.SHA256()).hex()


def prepare(source: Path, output: Path, *, days: int = 365) -> dict[str, object]:
    if not 120 <= days <= 730:
        raise ValueError("SPIRE_AUTHORITY_ROTATION_LIFETIME_INVALID")
    for name in AUTHORITIES:
        certificate = source / f"{name}.crt"
        key = source / f"{name}.key"
        if (
            certificate.is_symlink()
            or key.is_symlink()
            or not certificate.is_file()
            or not key.is_file()
            or key.stat().st_mode & 0o077
        ):
            raise ValueError("SPIRE_AUTHORITY_ROTATION_SOURCE_INVALID")
    reserve(output, {"owner": OWNER, "role": "authority-rotation"})
    with writer(output):
        receipt = output / "receipt.json"
        if receipt.exists():
            value = read(receipt)
            if value.get("code") != "SPIRE_AUTHORITY_ROTATION_PREPARED":
                raise ValueError("SPIRE_AUTHORITY_ROTATION_RECEIPT_INVALID")
            return value
        now = datetime.now(UTC)
        records = []
        for name in AUTHORITIES:
            old = x509.load_pem_x509_certificate((source / f"{name}.crt").read_bytes())
            old_basic = old.extensions.get_extension_for_class(x509.BasicConstraints)
            old_usage = old.extensions.get_extension_for_class(x509.KeyUsage)
            if (
                old.subject != old.issuer
                or not old_basic.value.ca
                or old_basic.value.path_length != 0
                or not old_usage.value.key_cert_sign
                or not old_usage.value.crl_sign
            ):
                raise ValueError("SPIRE_AUTHORITY_ROTATION_SOURCE_INVALID")
            private = ec.generate_private_key(ec.SECP256R1())
            public = private.public_key()
            certificate = (
                x509.CertificateBuilder()
                .subject_name(old.subject)
                .issuer_name(old.subject)
                .public_key(public)
                .serial_number(secrets.randbits(159) | 1)
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=days))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=False,
                        content_commitment=False,
                        key_encipherment=False,
                        data_encipherment=False,
                        key_agreement=False,
                        key_cert_sign=True,
                        crl_sign=True,
                        encipher_only=None,
                        decipher_only=None,
                    ),
                    True,
                )
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), False)
                .add_extension(
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(public), False
                )
                .sign(private, hashes.SHA256())
            )
            key_raw = private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            certificate_raw = certificate.public_bytes(serialization.Encoding.PEM)
            atomic(output / f"{name}.key", key_raw, 0o600)
            atomic(output / f"{name}.crt", certificate_raw, 0o600)
            records.append(
                {
                    "id": name,
                    "previous_sha256": old.fingerprint(hashes.SHA256()).hex(),
                    "candidate_sha256": certificate.fingerprint(hashes.SHA256()).hex(),
                    "candidate_expires_at": certificate.not_valid_after_utc.timestamp(),
                }
            )
        value: dict[str, object] = {
            "owner": OWNER,
            "schema_version": 1,
            "code": "SPIRE_AUTHORITY_ROTATION_PREPARED",
            "prepared_at": now.timestamp(),
            "automatic_activation": False,
            "authorities": records,
        }
        atomic(receipt, value, 0o600)
        return value


def stage_trust(
    source: Path,
    candidate: Path,
    observation: Path,
    *,
    receipt_root: Path | None = None,
) -> dict[str, object]:
    """Widen only existing local trust bundles; never activate or remove a root."""
    receipt = read(candidate / "receipt.json")
    if (
        receipt.get("owner") != OWNER
        or receipt.get("schema_version") != 1
        or receipt.get("code") != "SPIRE_AUTHORITY_ROTATION_PREPARED"
        or receipt.get("automatic_activation") is not False
    ):
        raise ValueError("SPIRE_AUTHORITY_ROTATION_RECEIPT_INVALID")
    records = receipt.get("authorities")
    if not isinstance(records, list) or len(records) != len(AUTHORITIES):
        raise ValueError("SPIRE_AUTHORITY_ROTATION_RECEIPT_INVALID")
    declared = {
        item.get("id"): item
        for item in records
        if isinstance(item, dict) and item.get("id") in AUTHORITIES
    }
    if set(declared) != set(AUTHORITIES):
        raise ValueError("SPIRE_AUTHORITY_ROTATION_RECEIPT_INVALID")
    old: dict[str, x509.Certificate] = {}
    new: dict[str, x509.Certificate] = {}
    for name in AUTHORITIES:
        old[name] = _certificate(source / f"{name}.crt")
        new[name] = _certificate(candidate / f"{name}.crt")
        if _fingerprint(old[name]) != declared[name].get(
            "previous_sha256"
        ) or _fingerprint(new[name]) != declared[name].get("candidate_sha256"):
            raise ValueError("SPIRE_AUTHORITY_ROTATION_RECEIPT_INVALID")
    changed: list[str] = []
    receipts = receipt_root or candidate
    with writer(receipts, ".trust-writer.lock"):
        for relative, authorities in TRUST_TARGETS.items():
            path = observation / relative
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
                raise ValueError("SPIRE_AUTHORITY_ROTATION_TRUST_INVALID")
            current = x509.load_pem_x509_certificates(path.read_bytes())
            expected_old = {
                old[name].public_bytes(serialization.Encoding.DER)
                for name in authorities
            }
            expected_all = expected_old | {
                new[name].public_bytes(serialization.Encoding.DER)
                for name in authorities
            }
            actual = {item.public_bytes(serialization.Encoding.DER) for item in current}
            if actual not in (expected_old, expected_all):
                raise ValueError("SPIRE_AUTHORITY_ROTATION_TRUST_INVALID")
            raw = b"".join(
                certificate.public_bytes(serialization.Encoding.PEM)
                for name in authorities
                for certificate in (old[name], new[name])
            )
            if path.read_bytes() != raw:
                atomic_preserving(path, raw)
                changed.append(relative)
        value: dict[str, object] = {
            "owner": OWNER,
            "schema_version": 1,
            "code": "SPIRE_AUTHORITY_ROTATION_TRUST_STAGED",
            "staged_at": datetime.now(UTC).timestamp(),
            "automatic_activation": False,
            "changed": changed,
        }
        atomic(receipts / "trust-stage-receipt.json", value, 0o600)
        return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "stage-trust"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observation", type=Path)
    parser.add_argument("--receipt-output", type=Path)
    parser.add_argument(
        "--environment", choices=("local", "test", "production"), required=True
    )
    parser.add_argument("--days", type=int, default=365)
    arguments = parser.parse_args()
    os.umask(0o077)
    try:
        if arguments.environment == "production":
            raise ValueError("SPIRE_AUTHORITY_ROTATION_EXTERNAL_ISSUER_REQUIRED")
        if arguments.action == "prepare":
            result = prepare(arguments.source, arguments.output, days=arguments.days)
        else:
            if arguments.observation is None:
                raise ValueError("SPIRE_AUTHORITY_ROTATION_OBSERVATION_REQUIRED")
            result = stage_trust(
                arguments.source,
                arguments.output,
                arguments.observation,
                receipt_root=arguments.receipt_output,
            )
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    except (OSError, ValueError, TypeError, KeyError):
        sys.stdout.write('{"code":"SPIRE_AUTHORITY_ROTATION_FAILED"}\n')
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
