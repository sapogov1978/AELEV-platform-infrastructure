"""Apply approved public leaf renewals inside this SPIRE installation only."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtensionOID

ROOT = Path("/var/lib/aelev-renewal")
OWNER = "infrastructure-spire"
PROFILE = {
    "metrics": ("observer-secrets", "metrics.crt", "metrics.key"),
    "observer": ("observer-secrets", "observer.crt", "observer.key"),
    "central-write": ("sender-secrets", "central-write.crt", "central-write.key"),
    "local-query": ("sender-secrets", "local-query.crt", "local-query.key"),
    "local-write": ("sender-secrets", "local-write.crt", "local-write.key"),
    "sender": ("sender-secrets", "sender.crt", "sender.key"),
}
STATUS_QUERY_PROFILE = {
    "status-query": ("status-secrets", "query.crt", "query.key"),
}
STATUS_PUBLISHER_PROFILE = {
    "status-publisher": ("status-secrets", "publisher.crt", "publisher.key"),
}
PROFILES = {
    "status.json": PROFILE,
    "status-query.json": STATUS_QUERY_PROFILE,
    "status-publisher.json": STATUS_PUBLISHER_PROFILE,
}
TRUST = {
    "metrics": (("sender-secrets", "server-ca.crt"),),
    "observer": (("sender-secrets", "local-write-ca.crt"),),
    "central-write": (),
    "local-query": (
        ("observer-secrets", "query-ca.crt"),
        ("sender-secrets", "local-query-ca.crt"),
    ),
    "local-write": (("sender-secrets", "local-write-ca.crt"),),
    "sender": (
        ("observer-secrets", "sender-ca.crt"),
        ("sender-secrets", "server-ca.crt"),
        ("sender-secrets", "local-query-ca.crt"),
    ),
    "status-publisher": (),
    "status-query": (),
}
MARKERS = {
    "observer-secrets": "observation-observer",
    "sender-secrets": "observation-sender",
    "status-secrets": "observation-status",
}


def require(value: bool) -> None:
    if not value:
        raise ValueError("SPIRE_CERTIFICATE_DELIVERY_SCOPE_INVALID")


def object_value(value: object) -> dict[str, Any]:
    require(isinstance(value, dict))
    return cast(dict[str, Any], value)


def list_value(value: object) -> list[object]:
    require(isinstance(value, list))
    return cast(list[object], value)


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def atomic(path: Path, raw: bytes) -> None:
    require(path.parent.is_dir() and not path.is_symlink())
    descriptor, name = tempfile.mkstemp(prefix=".certificate-", dir=path.parent)
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


def certificate_bundle(raw: bytes, *, maximum: int = 8) -> list[x509.Certificate]:
    """Parse only a bounded concatenation of public PEM certificates."""
    require(
        0 < len(raw) <= 65536
        and 1 <= raw.count(b"-----BEGIN CERTIFICATE-----") <= maximum
        and re.fullmatch(
            rb"(?:-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+"
            rb"-----END CERTIFICATE-----\s*)+",
            raw,
        )
        is not None
    )
    values = x509.load_pem_x509_certificates(raw)
    require(1 <= len(values) <= maximum)
    return values


def certificate(raw: bytes) -> x509.Certificate:
    """Parse exactly one leaf certificate."""
    values = certificate_bundle(raw, maximum=1)
    require(len(values) == 1)
    return values[0]


def public_key(value: Any) -> bytes:
    return value.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def extensions(value: x509.Certificate) -> tuple[tuple[str, bool, bytes], ...]:
    return tuple(
        (item.oid.dotted_string, item.critical, item.value.public_bytes())
        for item in value.extensions
        if item.oid
        not in {
            ExtensionOID.AUTHORITY_KEY_IDENTIFIER,
            ExtensionOID.SUBJECT_KEY_IDENTIFIER,
        }
    )


def verify_candidate(path: Path, key_path: Path, raw: bytes, issuer_raw: bytes) -> None:
    require(
        path.is_file()
        and key_path.is_file()
        and not path.is_symlink()
        and not key_path.is_symlink()
    )
    current_raw = path.read_bytes()
    current = certificate(current_raw)
    candidate = certificate(raw)
    issuers = certificate_bundle(issuer_raw)
    matching: list[x509.Certificate] = []
    for issuer in issuers:
        require(
            issuer.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        )
        if candidate.issuer != issuer.subject:
            continue
        try:
            candidate.verify_directly_issued_by(issuer)
        except (InvalidSignature, ValueError):
            continue
        matching.append(issuer)
    private = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    require(
        current.subject == candidate.subject
        and extensions(current) == extensions(candidate)
        and public_key(current.public_key()) == public_key(candidate.public_key())
        and public_key(private.public_key()) == public_key(candidate.public_key())
        and bool(matching)
        and (
            raw == current_raw
            or candidate.not_valid_after_utc > current.not_valid_after_utc
        )
        and candidate.not_valid_after_utc
        < max(issuer.not_valid_after_utc for issuer in matching)
        and candidate.not_valid_after_utc > datetime.now(UTC)
    )


def marker(role: str, root: Path) -> None:
    value = json.loads((root / role / "owner.json").read_bytes())
    require(value == {"owner": OWNER, "role": MARKERS[role]})


def trust_bundle(path: Path, issuer_raw: bytes) -> bytes:
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 262144)
    values = x509.load_pem_x509_certificates(path.read_bytes())
    candidates = certificate_bundle(issuer_raw)
    # Comparing normalized DER keeps this helper independent from PEM formatting.
    existing = {item.public_bytes(serialization.Encoding.DER): item for item in values}
    for candidate in candidates:
        require(
            candidate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        )
        existing[candidate.public_bytes(serialization.Encoding.DER)] = candidate
    require(1 <= len(existing) <= 8)
    return b"".join(
        item.public_bytes(serialization.Encoding.PEM) for item in existing.values()
    )


def run(value: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    require(
        set(value)
        == {
            "owner",
            "schema_version",
            "consumer_owner",
            "installation_id",
            "destinations",
            "report_destination",
            "report",
            "certificates",
        }
        and value["owner"] == "telemetry-certificate-renewal"
        and value["schema_version"] == 2
        and value["consumer_owner"] == OWNER
        and isinstance(value["installation_id"], str)
    )
    target = object_value(value["report_destination"])
    require(
        set(target) == {"volume", "role", "file"}
        and target["role"] == "certificate-renewal-status"
        and target["file"] in PROFILES
    )
    profile = PROFILES[target["file"]]
    destinations = list_value(value["destinations"])
    certificates = list_value(value["certificates"])
    report = object_value(value["report"])
    require(
        len(destinations) == len(profile)
        and report.get("owner") == "telemetry-certificate-renewal"
        and report.get("consumer_owner") == OWNER
        and report.get("schema_version") == 1
        and type(report.get("healthy")) is bool
        and type(report.get("checked_at")) in (int, float)
        and 0 <= time.time() - float(report["checked_at"]) <= 120
    )
    selected: dict[str, dict[str, Any]] = {}
    for raw_item in destinations:
        item = object_value(raw_item)
        require(
            set(item) == {"id", "volume", "role", "file"}
            and item["id"] in profile
            and item["id"] not in selected
            and (item["role"], item["file"]) == profile[item["id"]][:2]
        )
        selected[item["id"]] = item
    require(set(selected) == set(profile))
    for role in {value[0] for value in profile.values()}:
        marker(role, root)
    status = root / "certificate-renewal-status"
    require(status.is_dir() and not status.is_symlink())
    # Independent renewal owners publish disjoint profiles on the same interval.
    # A profile-scoped lock serializes each atomic set without making unrelated
    # status, query and publisher deliveries fail one another.
    lock = os.open(
        status / ("." + target["file"] + ".lock"),
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = status / target["file"]
        if previous.exists():
            old = json.loads(previous.read_bytes())
            require(float(report["checked_at"]) >= float(old.get("checked_at", 0)))
        require(report["healthy"] or not certificates)
        require(not report["healthy"] or len(certificates) == len(profile))
        hashes: dict[str, str] = {}
        prepared: list[tuple[str, bytes, bytes]] = []
        reported = [
            object_value(item) for item in list_value(report.get("certificates", []))
        ]
        report_entries = {item["id"]: item for item in reported}
        for raw_item in certificates:
            item = object_value(raw_item)
            require(
                set(item) == {"id", "certificate_base64", "issuer_certificate_base64"}
                and item["id"] in selected
                and item["id"] not in hashes
            )
            name = item["id"]
            role, filename, key = profile[name]
            raw = base64.b64decode(item["certificate_base64"], validate=True)
            issuer = base64.b64decode(item["issuer_certificate_base64"], validate=True)
            verify_candidate(root / role / filename, root / role / key, raw, issuer)
            digest = hashlib.sha256(raw).hexdigest()
            require(
                report_entries[name]["healthy"] is True
                and report_entries[name]["certificate_sha256"] == digest
            )
            hashes[name] = digest
            prepared.append((name, raw, issuer))
        # Trust is widened before any leaf switches to a new issuer. Old anchors
        # remain for overlap and are never removed by the automatic leaf task.
        for name, _, issuer in prepared:
            for role, filename in TRUST[name]:
                path = root / role / filename
                updated = trust_bundle(path, issuer)
                if updated != path.read_bytes():
                    atomic(path, updated)
        for name, raw, _ in prepared:
            role, filename, _ = profile[name]
            path = root / role / filename
            if path.read_bytes() != raw:
                atomic(path, raw)
        atomic(previous, encoded(report))
        return {
            "code": "CERTIFICATE_DELIVERY_VERIFIED",
            "installation_id": value["installation_id"],
            "report_sha256": hashlib.sha256(encoded(report)).hexdigest(),
            "certificate_sha256": hashes,
        }
    finally:
        os.close(lock)


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        require(len(raw) <= 1024 * 1024)
        result = run(json.loads(raw))
        sys.stdout.buffer.write(encoded(result) + b"\n")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        sys.stderr.write('{"code":"SPIRE_CERTIFICATE_DELIVERY_FAILED"}\n')
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
