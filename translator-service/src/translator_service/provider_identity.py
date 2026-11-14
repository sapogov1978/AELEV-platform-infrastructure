"""Fresh, exact TLS identity using public Python/OpenSSL APIs only."""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import stat
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Never, cast
from urllib.parse import urlsplit

MAX_MATERIAL = 131_072


class IdentityError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PROVIDER_TLS_IDENTITY_UNAVAILABLE")


def read_file(path: Path, maximum: int = MAX_MATERIAL) -> bytes:
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise IdentityError
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise IdentityError
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise IdentityError
    return raw


def object_value(raw: bytes) -> dict[str, Any]:
    value: object = json.loads(raw)
    if not isinstance(value, dict):
        raise IdentityError
    return cast(dict[str, Any], value)


def valid_spiffe(value: object, owner: str) -> bool:
    if not isinstance(value, str) or len(value) > 1024:
        return False
    parsed = urlsplit(value)
    return bool(
        parsed.scheme == "spiffe"
        and value.startswith("spiffe://")
        and parsed.hostname
        and parsed.netloc == parsed.hostname
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", parsed.hostname)
        and len(parsed.hostname) <= 253
        and all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in parsed.hostname.split(".")
        )
        and not parsed.query
        and not parsed.fragment
        and parsed.path.startswith("/" + owner + "/")
        and all(
            re.fullmatch(r"[a-zA-Z0-9_-][a-zA-Z0-9._-]*", part)
            and part not in {".", ".."}
            for part in parsed.path.split("/")[1:]
        )
    )


def leaf_certificate(pem: bytes) -> bool:
    try:
        if not 0 < len(pem) <= MAX_MATERIAL:
            return False
        classification = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        classification.load_verify_locations(cadata=pem.decode("ascii"))
        return not classification.get_ca_certs()
    except (OSError, ValueError):
        return False


def peer_matches(certificate: dict[str, Any], expected: str) -> bool:
    try:
        return [
            value
            for kind, value in certificate.get("subjectAltName", ())
            if kind == "URI"
        ] == [expected] and ssl.cert_time_to_seconds(
            certificate["notBefore"]
        ) <= time.time() < ssl.cert_time_to_seconds(certificate["notAfter"])
    except (KeyError, ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Material:
    cert: Path
    key: Path | None
    bundle: Path
    fingerprint: str
    metadata: dict[str, Any] | None


def reject_password() -> Never:
    raise IdentityError


def verified_self(
    material: Material,
    role: Literal["server", "client"],
) -> dict[str, Any]:
    """Verify the owned leaf in a bounded in-memory TLS handshake, never a socket."""
    if role == "server":
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.check_hostname = False
        client.load_verify_locations(cafile=material.bundle)
        client.verify_flags |= ssl.VERIFY_X509_STRICT
        # Only this local inspection context accepts the absent probe client.
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(material.cert, material.key, password=reject_password)
    else:
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(material.cert, material.key, password=reject_password)
        server.load_verify_locations(cafile=material.bundle)
        server.verify_mode = ssl.CERT_REQUIRED
        server.verify_flags |= ssl.VERIFY_X509_STRICT
        # No peer material is accepted here; both ends are this same owned leaf.
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.check_hostname = False
        client.verify_mode = ssl.CERT_NONE
        client.load_cert_chain(material.cert, material.key, password=reject_password)
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    c = client.wrap_bio(client_in, client_out, server_side=False)
    s = server.wrap_bio(server_in, server_out, server_side=True)
    complete: set[str] = set()
    for _ in range(32):
        for name, endpoint in (("client", c), ("server", s)):
            if name not in complete:
                try:
                    endpoint.do_handshake()
                    complete.add(name)
                except ssl.SSLWantReadError:
                    pass
        server_in.write(client_out.read())
        client_in.write(server_out.read())
        if len(complete) == 2:
            peer = c.getpeercert() if role == "server" else s.getpeercert()
            if not isinstance(peer, dict):
                raise IdentityError
            return cast(dict[str, Any], peer)
    raise IdentityError


class Identity:
    def __init__(
        self, root: Path, owner: str, role: Literal["server", "client"] = "server"
    ) -> None:
        self.root, self.owner = root, owner
        self.role: Literal["server", "client"] = role
        try:
            self.policy_raw = read_file(root / "identity.json", 16_384)
            policy = object_value(self.policy_raw)
            if (
                set(policy)
                != {
                    "schema_version",
                    "environment",
                    "delivery",
                    "server_spiffe_id",
                    "client_spiffe_id",
                }
                or type(policy["schema_version"]) is not int
                or policy["schema_version"] != 1
                or policy["environment"] not in {"local", "test", "production"}
                or policy["delivery"] not in {"spire-file-v1", "pem-files-v1"}
                or (
                    policy["environment"] == "production"
                    and policy["delivery"] != "spire-file-v1"
                )
                or not all(
                    valid_spiffe(policy[key], owner)
                    for key in ("server_spiffe_id", "client_spiffe_id")
                )
                or policy["server_spiffe_id"] == policy["client_spiffe_id"]
                or urlsplit(cast(str, policy["server_spiffe_id"])).netloc
                != urlsplit(cast(str, policy["client_spiffe_id"])).netloc
            ):
                raise IdentityError
            self.policy = policy
        except (OSError, ValueError, TypeError, KeyError):
            raise IdentityError from None
        self.own_id = cast(str, policy[role + "_spiffe_id"])
        self.peer_id = cast(
            str, policy[("client" if role == "server" else "server") + "_spiffe_id"]
        )
        self.lock = threading.Lock()
        self.fingerprint: str | None = None
        self.cached: ssl.SSLContext | None = None
        self.certificate: dict[str, Any] = {}

    def material(self) -> Material:
        if read_file(self.root / "identity.json", 16_384) != self.policy_raw:
            raise IdentityError
        metadata = None
        if self.policy["delivery"] == "spire-file-v1":
            metadata = object_value(read_file(self.root / "current.json", 16_384))
            now = datetime.now(UTC)
            updated = datetime.fromisoformat(metadata["updated_at"])
            expires = datetime.fromisoformat(metadata["expires_at"])
            generation = metadata["generation"]
            if (
                metadata.get("owner") != "spire-svid-delivery"
                or type(metadata.get("schema_version")) is not int
                or metadata["schema_version"] != 1
                or metadata.get("state") != "READY"
                or metadata.get("environment") != self.policy["environment"]
                or metadata.get("spiffe_id") != self.own_id
                or updated.tzinfo is None
                or expires.tzinfo is None
                or not 0 <= (now - updated).total_seconds() <= 20
                or not 1 < (expires - now).total_seconds() <= 3900
                or not isinstance(generation, str)
                or re.fullmatch(r"[0-9a-f]{64}", generation) is None
            ):
                raise IdentityError
            directory = self.root / "generations" / generation
            cert, key, bundle = directory / "tls.pem", None, directory / "bundle.pem"
        else:
            cert = self.root / ("server.crt" if self.role == "server" else "client.crt")
            key = self.root / ("server.key" if self.role == "server" else "client.key")
            bundle = self.root / (
                "client-ca.crt" if self.role == "server" else "ca.crt"
            )
        raw = [read_file(path) for path in (cert, key, bundle) if path is not None]
        if (key or cert).stat().st_mode & 0o077:
            raise IdentityError
        if metadata is not None and (
            hashlib.sha256(raw[0]).hexdigest() != metadata.get("tls_sha256")
            or hashlib.sha256(raw[1]).hexdigest() != metadata.get("bundle_sha256")
        ):
            raise IdentityError
        leaf = re.search(
            rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", raw[0], re.S
        )
        if leaf is None:
            raise IdentityError
        if not leaf_certificate(leaf[0]):
            raise IdentityError
        return Material(
            cert, key, bundle, hashlib.sha256(b"".join(raw)).hexdigest(), metadata
        )

    def current(self) -> ssl.SSLContext:
        try:
            with self.lock:
                material = self.material()
                if self.cached is None or material.fingerprint != self.fingerprint:
                    context = ssl.SSLContext(
                        ssl.PROTOCOL_TLS_SERVER
                        if self.role == "server"
                        else ssl.PROTOCOL_TLS_CLIENT
                    )
                    context.minimum_version = ssl.TLSVersion.TLSv1_2
                    context.verify_mode = ssl.CERT_REQUIRED
                    context.verify_flags |= ssl.VERIFY_X509_STRICT
                    context.load_verify_locations(cafile=material.bundle)
                    context.load_cert_chain(
                        material.cert, material.key, password=reject_password
                    )
                    certificate = verified_self(material, self.role)
                    if self.material().fingerprint != material.fingerprint:
                        raise IdentityError
                    self.cached, self.certificate = context, certificate
                    self.fingerprint = material.fingerprint
                if not peer_matches(self.certificate, self.own_id):
                    raise IdentityError
                if material.metadata is not None and (
                    str(int(self.certificate["serialNumber"], 16))
                    != material.metadata.get("serial")
                    or ssl.cert_time_to_seconds(self.certificate["notAfter"])
                    != datetime.fromisoformat(
                        material.metadata["expires_at"]
                    ).timestamp()
                ):
                    raise IdentityError
                return self.cached
        except (OSError, ValueError, TypeError, KeyError):
            raise IdentityError from None

    def available(self) -> bool:
        try:
            self.current()
            return True
        except IdentityError:
            return False

    def wire_context(self) -> ssl.SSLContext:
        initial = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        initial.minimum_version = ssl.TLSVersion.TLSv1_2
        initial.verify_mode = ssl.CERT_REQUIRED

        def choose(
            connection: ssl.SSLSocket | ssl.SSLObject,
            _name: str | None,
            _initial: ssl.SSLContext,
        ) -> int | None:
            try:
                connection.context = self.current()
            except IdentityError:
                return ssl.ALERT_DESCRIPTION_INTERNAL_ERROR
            return None

        initial.sni_callback = choose
        return initial
