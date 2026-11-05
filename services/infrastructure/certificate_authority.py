"""Prepare and stage an explicit local CA certificate rollover."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


def atomic(path: Path, raw: bytes, mode: int | None = None) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("CERTIFICATE_AUTHORITY_PATH_INVALID")
    descriptor, name = tempfile.mkstemp(prefix=".authority-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if path.exists():
                metadata = path.stat()
                os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
                os.fchmod(stream.fileno(), metadata.st_mode & 0o777)
            else:
                os.fchmod(stream.fileno(), 0o600 if mode is None else mode)
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


def regular(path: Path, *, private: bool = False) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("CERTIFICATE_AUTHORITY_PATH_INVALID")
    if private and path.stat().st_mode & 0o077:
        raise ValueError("CERTIFICATE_AUTHORITY_PATH_INVALID")


def openssl(executable: str, *arguments: str) -> bytes:
    result = subprocess.run(
        [executable, *arguments], capture_output=True, timeout=20, check=False
    )
    if result.returncode:
        raise ValueError("CERTIFICATE_AUTHORITY_NATIVE_VALIDATION_FAILED")
    return result.stdout


def fingerprint(executable: str, path: Path) -> str:
    value = openssl(
        executable, "x509", "-in", str(path), "-noout", "-fingerprint", "-sha256"
    ).decode()
    return value.strip().split("=", 1)[1].replace(":", "").lower()


def expires_at(executable: str, path: Path) -> float:
    value = openssl(
        executable, "x509", "-in", str(path), "-noout", "-enddate"
    ).decode().strip()
    if not value.startswith("notAfter="):
        raise ValueError("CERTIFICATE_AUTHORITY_DATE_INVALID")
    return ssl.cert_time_to_seconds(value.removeprefix("notAfter="))


def validate_authority(executable: str, certificate: Path, key: Path) -> None:
    regular(certificate)
    regular(key, private=True)
    subject = openssl(
        executable,
        "x509",
        "-in",
        str(certificate),
        "-noout",
        "-subject",
        "-nameopt",
        "RFC2253",
    )
    issuer = openssl(
        executable,
        "x509",
        "-in",
        str(certificate),
        "-noout",
        "-issuer",
        "-nameopt",
        "RFC2253",
    )
    text = openssl(executable, "x509", "-in", str(certificate), "-noout", "-text")
    if (
        subject.removeprefix(b"subject=") != issuer.removeprefix(b"issuer=")
        or b"CA:TRUE" not in text
        or b"Certificate Sign" not in text
        or openssl(
            executable, "x509", "-in", str(certificate), "-pubkey", "-noout"
        )
        != openssl(executable, "pkey", "-in", str(key), "-pubout")
    ):
        raise ValueError("CERTIFICATE_AUTHORITY_SOURCE_INVALID")


def prepare(
    source_certificate: Path,
    source_key: Path | None,
    output: Path,
    owner: str,
    days: int = 3650,
    executable: str | None = None,
    certificate_name: str = "ca.crt",
    new_key: bool = False,
) -> dict[str, Any]:
    binary = executable or shutil.which("openssl")
    if (
        binary is None
        or not owner
        or not 365 <= days <= 3650
        or certificate_name not in {"ca.crt", "ca.pem"}
    ):
        raise ValueError("CERTIFICATE_AUTHORITY_SCOPE_INVALID")
    regular(source_certificate)
    if new_key:
        text = openssl(
            binary, "x509", "-in", str(source_certificate), "-noout", "-text"
        )
        if b"CA:TRUE" not in text:
            raise ValueError("CERTIFICATE_AUTHORITY_SOURCE_INVALID")
    else:
        if source_key is None:
            raise ValueError("CERTIFICATE_AUTHORITY_PATH_INVALID")
        validate_authority(binary, source_certificate, source_key)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(output / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt = output / "receipt.json"
        if receipt.exists():
            value = json.loads(receipt.read_bytes())
            if not isinstance(value, dict):
                raise ValueError("CERTIFICATE_AUTHORITY_RECEIPT_INVALID")
            return value
        candidate = output / ".candidate.crt"
        candidate.touch(mode=0o600, exist_ok=False)
        try:
            if new_key:
                subject = openssl(
                    binary,
                    "x509",
                    "-in",
                    str(source_certificate),
                    "-noout",
                    "-subject",
                    "-nameopt",
                    "RFC2253",
                ).decode().strip().removeprefix("subject=")
                if "," in subject or not subject.startswith("CN="):
                    raise ValueError("CERTIFICATE_AUTHORITY_SUBJECT_UNSUPPORTED")
                request_subject = "/" + subject
                openssl(
                    binary,
                    "req",
                    "-new",
                    "-x509",
                    "-newkey",
                    "rsa:3072",
                    "-nodes",
                    "-days",
                    str(days),
                    "-sha256",
                    "-subj",
                    request_subject,
                    "-addext",
                    "basicConstraints=critical,CA:TRUE,pathlen:0",
                    "-addext",
                    "keyUsage=critical,keyCertSign,cRLSign",
                    "-keyout",
                    str(output / "ca.key"),
                    "-out",
                    str(candidate),
                )
                (output / "ca.key").chmod(0o600)
                validate_authority(binary, candidate, output / "ca.key")
            else:
                openssl(
                    binary,
                    "x509",
                    "-in",
                    str(source_certificate),
                    "-signkey",
                    str(source_key),
                    "-set_serial",
                    str(secrets.randbits(128) | 1),
                    "-days",
                    str(days),
                    "-sha256",
                    "-out",
                    str(candidate),
                )
                validate_authority(binary, candidate, source_key)
            atomic(output / certificate_name, candidate.read_bytes())
            if not new_key:
                atomic(output / "ca.key", source_key.read_bytes())
        finally:
            candidate.unlink(missing_ok=True)
        result: dict[str, Any] = {
            "owner": owner,
            "schema_version": 1,
            "code": "CERTIFICATE_AUTHORITY_ROLLOVER_PREPARED",
            "prepared_at": time.time(),
            "automatic_activation": False,
            "private_key_reused": not new_key,
            "previous_sha256": fingerprint(binary, source_certificate),
            "certificate_name": certificate_name,
            "candidate_sha256": fingerprint(binary, output / certificate_name),
            "candidate_expires_at": expires_at(binary, output / certificate_name),
        }
        atomic(output / "previous-ca.crt", source_certificate.read_bytes(), 0o644)
        atomic(receipt, json.dumps(result, sort_keys=True).encode())
        return result
    finally:
        os.close(lock)


def stage(
    source_certificate: Path,
    candidate: Path,
    trust: Path,
    owner: str,
    executable: str | None = None,
) -> dict[str, Any]:
    binary = executable or shutil.which("openssl")
    if binary is None:
        raise ValueError("CERTIFICATE_AUTHORITY_SCOPE_INVALID")
    regular(source_certificate)
    regular(trust)
    receipt = json.loads((candidate / "receipt.json").read_bytes())
    certificate_name = receipt.get("certificate_name", "ca.crt")
    if certificate_name not in {"ca.crt", "ca.pem"}:
        raise ValueError("CERTIFICATE_AUTHORITY_RECEIPT_INVALID")
    regular(candidate / certificate_name)
    if (
        receipt.get("owner") != owner
        or receipt.get("code") != "CERTIFICATE_AUTHORITY_ROLLOVER_PREPARED"
        or receipt.get("automatic_activation") is not False
        or receipt.get("previous_sha256") != fingerprint(binary, source_certificate)
        or receipt.get("candidate_sha256")
        != fingerprint(binary, candidate / certificate_name)
    ):
        raise ValueError("CERTIFICATE_AUTHORITY_RECEIPT_INVALID")
    old = openssl(binary, "x509", "-in", str(source_certificate), "-outform", "PEM")
    new = openssl(
        binary, "x509", "-in", str(candidate / certificate_name), "-outform", "PEM"
    )
    current = trust.read_bytes()
    if current.count(old) != 1 or current.count(new) > 1:
        raise ValueError("CERTIFICATE_AUTHORITY_TRUST_INVALID")
    raw = current if new in current else current.replace(old, old + new)
    if raw.count(old) != 1 or raw.count(new) != 1:
        raise ValueError("CERTIFICATE_AUTHORITY_TRUST_INVALID")
    changed = current != raw
    if changed:
        atomic(trust, raw)
    result = {
        "owner": owner,
        "schema_version": 1,
        "code": "CERTIFICATE_AUTHORITY_TRUST_STAGED",
        "staged_at": time.time(),
        "automatic_activation": False,
        "changed": changed,
        "trust_sha256": hashlib.sha256(raw).hexdigest(),
    }
    name = "trust-stage-" + hashlib.sha256(str(trust).encode()).hexdigest() + ".json"
    atomic(candidate / name, json.dumps(result, sort_keys=True).encode())
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "stage"))
    parser.add_argument("--source-certificate", type=Path, required=True)
    parser.add_argument("--source-key", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trust", type=Path)
    parser.add_argument("--owner", required=True)
    parser.add_argument(
        "--environment", choices=("local", "test", "production"), required=True
    )
    parser.add_argument("--days", type=int, default=3650)
    parser.add_argument("--openssl")
    parser.add_argument("--certificate-name", choices=("ca.crt", "ca.pem"), default="ca.crt")
    parser.add_argument("--new-key", action="store_true")
    arguments = parser.parse_args()
    os.umask(0o077)
    try:
        if arguments.environment == "production":
            raise ValueError("CERTIFICATE_AUTHORITY_EXTERNAL_ISSUER_REQUIRED")
        if arguments.action == "prepare":
            if arguments.source_key is None and not arguments.new_key:
                raise ValueError("CERTIFICATE_AUTHORITY_PATH_INVALID")
            result = prepare(
                arguments.source_certificate,
                arguments.source_key,
                arguments.output,
                arguments.owner,
                arguments.days,
                arguments.openssl,
                arguments.certificate_name,
                arguments.new_key,
            )
        else:
            if arguments.trust is None:
                raise ValueError("CERTIFICATE_AUTHORITY_PATH_INVALID")
            result = stage(
                arguments.source_certificate,
                arguments.output,
                arguments.trust,
                arguments.owner,
                arguments.openssl,
            )
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        print('{"code":"CERTIFICATE_AUTHORITY_ROLLOVER_FAILED"}')
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
