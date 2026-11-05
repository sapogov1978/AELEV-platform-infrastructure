"""Prepare and stage an explicit local publisher-CA overlap."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

OWNER = "infrastructure-platform-ui"
AUTHORITIES = {"publisher", "transport"}


def atomic(path: Path, raw: bytes) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("PLATFORM_UI_PUBLISHER_CA_PATH_INVALID")
    descriptor, name = tempfile.mkstemp(prefix=".publisher-ca-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if path.exists():
                metadata = path.stat()
                os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
                os.fchmod(stream.fileno(), metadata.st_mode & 0o777)
            else:
                os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def load(path: Path) -> x509.Certificate:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("PLATFORM_UI_PUBLISHER_CA_PATH_INVALID")
    return x509.load_pem_x509_certificate(path.read_bytes())


def fingerprint(value: x509.Certificate) -> str:
    return value.fingerprint(hashes.SHA256()).hex()


def prepare(
    source: Path, output: Path, days: int = 365, authority: str = "publisher"
) -> dict[str, object]:
    if not 120 <= days <= 730:
        raise ValueError("PLATFORM_UI_PUBLISHER_CA_LIFETIME_INVALID")
    if authority not in AUTHORITIES:
        raise ValueError("PLATFORM_UI_PUBLISHER_CA_AUTHORITY_INVALID")
    old = load(source / f"{authority}.crt")
    key_path = source / f"{authority}.key"
    if (
        key_path.is_symlink()
        or not key_path.is_file()
        or key_path.stat().st_mode & 0o077
    ):
        raise ValueError("PLATFORM_UI_PUBLISHER_CA_PATH_INVALID")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(output / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt = output / "receipt.json"
        if receipt.exists():
            parsed: object = json.loads(receipt.read_bytes())
            if not isinstance(parsed, dict):
                raise ValueError("PLATFORM_UI_PUBLISHER_CA_RECEIPT_INVALID")
            return cast(dict[str, object], parsed)
        basic = old.extensions.get_extension_for_class(x509.BasicConstraints).value
        usage = old.extensions.get_extension_for_class(x509.KeyUsage).value
        if old.subject != old.issuer or not basic.ca or not usage.key_cert_sign:
            raise ValueError("PLATFORM_UI_PUBLISHER_CA_SOURCE_INVALID")
        private = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(old.subject)
            .issuer_name(old.subject)
            .public_key(private.public_key())
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
                    encipher_only=None,  # type: ignore[arg-type]
                    decipher_only=None,  # type: ignore[arg-type]
                ),
                True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(private.public_key()), False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    private.public_key()
                ),
                False,
            )
            .sign(private, hashes.SHA256())
        )
        atomic(
            output / f"{authority}.key",
            private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        atomic(
            output / f"{authority}.crt",
            certificate.public_bytes(serialization.Encoding.PEM),
        )
        value: dict[str, object] = {
            "owner": OWNER,
            "schema_version": 1,
            "code": "PLATFORM_UI_PUBLISHER_CA_PREPARED",
            "prepared_at": time.time(),
            "automatic_activation": False,
            "authority": authority,
            "previous_sha256": fingerprint(old),
            "candidate_sha256": fingerprint(certificate),
            "candidate_expires_at": certificate.not_valid_after_utc.timestamp(),
        }
        atomic(receipt, json.dumps(value, sort_keys=True).encode())
        return value
    finally:
        os.close(lock)


def stage(
    source: Path,
    candidate: Path,
    trust: Path,
    authority: str = "publisher",
) -> dict[str, object]:
    if authority not in AUTHORITIES:
        raise ValueError("PLATFORM_UI_PUBLISHER_CA_AUTHORITY_INVALID")
    old = load(source / f"{authority}.crt")
    new = load(candidate / f"{authority}.crt")
    receipt = json.loads((candidate / "receipt.json").read_bytes())
    if (
        receipt.get("owner") != OWNER
        or receipt.get("code") != "PLATFORM_UI_PUBLISHER_CA_PREPARED"
        or receipt.get("automatic_activation") is not False
        or receipt.get("authority", "publisher") != authority
        or receipt.get("previous_sha256") != fingerprint(old)
        or receipt.get("candidate_sha256") != fingerprint(new)
    ):
        raise ValueError("PLATFORM_UI_PUBLISHER_CA_RECEIPT_INVALID")
    current = x509.load_pem_x509_certificates(trust.read_bytes())
    actual = {item.public_bytes(serialization.Encoding.DER) for item in current}
    old_der = old.public_bytes(serialization.Encoding.DER)
    new_der = new.public_bytes(serialization.Encoding.DER)
    if actual not in ({old_der}, {old_der, new_der}):
        raise ValueError("PLATFORM_UI_PUBLISHER_CA_TRUST_INVALID")
    raw = old.public_bytes(serialization.Encoding.PEM) + new.public_bytes(
        serialization.Encoding.PEM
    )
    changed = trust.read_bytes() != raw
    if changed:
        atomic(trust, raw)
    value: dict[str, object] = {
        "owner": OWNER,
        "schema_version": 1,
        "code": "PLATFORM_UI_PUBLISHER_CA_TRUST_STAGED",
        "staged_at": time.time(),
        "automatic_activation": False,
        "authority": authority,
        "changed": changed,
        "trust_sha256": hashlib.sha256(raw).hexdigest(),
    }
    atomic(
        candidate / "trust-stage-receipt.json",
        json.dumps(value, sort_keys=True).encode(),
    )
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "stage"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trust", type=Path)
    parser.add_argument(
        "--environment", choices=("local", "test", "production"), required=True
    )
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--authority", choices=sorted(AUTHORITIES), default="publisher")
    arguments = parser.parse_args()
    os.umask(0o077)
    try:
        if arguments.environment == "production":
            raise ValueError("PLATFORM_UI_PUBLISHER_CA_EXTERNAL_ISSUER_REQUIRED")
        result = (
            prepare(
                arguments.source, arguments.output, arguments.days, arguments.authority
            )
            if arguments.action == "prepare"
            else stage(
                arguments.source,
                arguments.output,
                arguments.trust if arguments.trust is not None else Path("/invalid"),
                arguments.authority,
            )
        )
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        print('{"code":"PLATFORM_UI_PUBLISHER_CA_ROTATION_FAILED"}')
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
