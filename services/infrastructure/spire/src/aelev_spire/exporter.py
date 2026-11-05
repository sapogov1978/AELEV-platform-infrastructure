"""Atomic, short-lived file delivery from the real SPIFFE Workload API."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from .files import atomic, read, reserve
from .settings import AGENT, AGENT_SOCKET, environment, identity


def verify(
    chain_raw: bytes, key_raw: bytes, bundle_raw: bytes, expected: str
) -> x509.Certificate:
    if any(not 0 < len(raw) <= 65536 for raw in (chain_raw, key_raw, bundle_raw)):
        raise ValueError("SPIRE_SVID_SIZE_INVALID")
    chain = x509.load_pem_x509_certificates(chain_raw)
    bundle = x509.load_pem_x509_certificates(bundle_raw)
    if not chain or not bundle:
        raise ValueError("SPIRE_SVID_CHAIN_INVALID")
    key = serialization.load_pem_private_key(key_raw, password=None)
    if not isinstance(key, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey):
        raise ValueError("SPIRE_SVID_KEY_INVALID")

    def public(value: Any) -> bytes:
        return value.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    cert = chain[0]
    now = datetime.now(UTC)
    if (
        public(key.public_key()) != public(cert.public_key())
        or cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        or cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.UniformResourceIdentifier)
        != [expected]
        or not cert.not_valid_before_utc <= now < cert.not_valid_after_utc
        or cert.not_valid_after_utc.timestamp() - now.timestamp() > 3900
    ):
        raise ValueError("SPIRE_SVID_SCOPE_INVALID")
    for index, child in enumerate(chain):
        parents = chain[index + 1 : index + 2] or [
            parent for parent in bundle if parent.subject == child.issuer
        ]
        if not parents:
            raise ValueError("SPIRE_SVID_CHAIN_INVALID")
        verified = False
        for parent in parents:
            if (
                not parent.extensions.get_extension_for_class(
                    x509.BasicConstraints
                ).value.ca
                or not parent.not_valid_before_utc <= now < parent.not_valid_after_utc
            ):
                continue
            try:
                child.verify_directly_issued_by(parent)
                verified = True
                break
            except (InvalidSignature, ValueError):
                continue
        if not verified:
            raise ValueError("SPIRE_SVID_CHAIN_INVALID")
    return cert


class Exporter:
    def __init__(self, root: Path, plan: dict[str, Any]) -> None:
        self.root = root
        self.environment, self.trust_domain, _ = environment(plan)
        self.spiffe_id = identity(plan.get("spiffe_id"), self.trust_domain)
        reserve(
            root,
            {
                "owner": "spire-svid-delivery",
                "schema_version": 1,
                "spiffe_id": self.spiffe_id,
                "environment": self.environment,
            },
        )

    def unavailable(self, code: str) -> None:
        atomic(self.root / "current.json", self.metadata("UNAVAILABLE", code=code))

    def metadata(self, state: str, **extra: Any) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "owner": "spire-svid-delivery",
            "state": state,
            "spiffe_id": self.spiffe_id,
            "environment": self.environment,
            "updated_at": datetime.now(UTC).isoformat(),
            **extra,
        }

    def publish(self, chain: bytes, key: bytes, bundle: bytes) -> dict[str, Any]:
        cert = verify(chain, key, bundle, self.spiffe_id)
        fingerprint = hashlib.sha256(chain + key + bundle).hexdigest()
        current = self.root / "current.json"
        generation = fingerprint
        target = self.root / "generations" / generation
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        pem = key.rstrip() + b"\n" + chain
        value = self.metadata(
            "READY",
            generation=generation,
            material_digest=fingerprint,
            tls_sha256=hashlib.sha256(pem).hexdigest(),
            bundle_sha256=hashlib.sha256(bundle).hexdigest(),
            expires_at=cert.not_valid_after_utc.isoformat(),
            serial=str(cert.serial_number),
        )
        marker = target / "owner.json"
        if marker.exists():
            previous_marker = read(marker)
            if any(
                previous_marker.get(name) != value[name]
                for name in value
                if name not in {"updated_at", "state"}
            ):
                raise ValueError("SPIRE_SVID_GENERATION_COLLISION")
        else:
            if any(target.iterdir()):
                raise ValueError("SPIRE_SVID_UNKNOWN_GENERATION_PRESERVED")
            atomic(marker, {**value, "state": "BUILDING"})
        for name, raw, mode in (("tls.pem", pem, 0o600), ("bundle.pem", bundle, 0o644)):
            path = target / name
            if path.exists():
                if path.is_symlink() or path.read_bytes() != raw:
                    raise ValueError("SPIRE_SVID_GENERATION_COLLISION")
            else:
                atomic(path, raw, mode)
        atomic(target / "owner.json", value)
        atomic(current, value)
        self.prune(generation)
        return value

    def prune(self, current: str) -> None:
        """Only expired, exact owned ephemeral generations; never other data."""
        for directory in (self.root / "generations").iterdir():
            if directory.name == current:
                continue
            if directory.is_symlink() or not re.fullmatch(
                r"[0-9a-f]{64}", directory.name
            ):
                raise ValueError("SPIRE_SVID_UNKNOWN_GENERATION_PRESERVED")
            marker = read(directory / "owner.json")
            if (
                marker.get("owner") != "spire-svid-delivery"
                or marker.get("spiffe_id") != self.spiffe_id
                or marker.get("generation") != directory.name
            ):
                raise ValueError("SPIRE_SVID_UNKNOWN_GENERATION_PRESERVED")
            if datetime.fromisoformat(marker["expires_at"]) >= datetime.now(UTC):
                continue
            if not {path.name for path in directory.iterdir()} <= {
                "tls.pem",
                "bundle.pem",
                "owner.json",
            } or any(path.is_symlink() for path in directory.iterdir()):
                raise ValueError("SPIRE_SVID_UNKNOWN_GENERATION_PRESERVED")
            for name, digest_key in (
                ("tls.pem", "tls_sha256"),
                ("bundle.pem", "bundle_sha256"),
            ):
                path = directory / name
                if (
                    path.exists()
                    and hashlib.sha256(path.read_bytes()).hexdigest()
                    != marker[digest_key]
                ):
                    raise ValueError("SPIRE_SVID_UNKNOWN_GENERATION_PRESERVED")
            for name in ("tls.pem", "bundle.pem", "owner.json"):
                (directory / name).unlink(missing_ok=True)
            directory.rmdir()

    def once(self) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(
            prefix="spire-fetch-", dir="/run/spire"
        ) as temporary:
            os.chmod(temporary, 0o700)
            result = subprocess.run(
                [
                    AGENT,
                    "api",
                    "fetch",
                    "x509",
                    "-socketPath",
                    AGENT_SOCKET,
                    "-timeout",
                    "3s",
                    "-silent",
                    "-write",
                    temporary,
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if result.returncode:
                code = (
                    "SPIRE_WORKLOAD_NOT_AUTHORIZED"
                    if b"PermissionDenied" in result.stderr
                    else "SPIRE_WORKLOAD_UNAVAILABLE"
                )
                self.unavailable(code)
                raise ValueError(code)
            root = Path(temporary)
            if {path.name for path in root.iterdir()} != {
                "svid.0.pem",
                "svid.0.key",
                "bundle.0.pem",
            }:
                raise ValueError("SPIRE_EXACTLY_ONE_SVID_REQUIRED")
            return self.publish(
                (root / "svid.0.pem").read_bytes(),
                (root / "svid.0.key").read_bytes(),
                (root / "bundle.0.pem").read_bytes(),
            )
