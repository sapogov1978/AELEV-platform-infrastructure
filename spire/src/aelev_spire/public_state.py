"""Authenticated owner-only public projection; no native admin API or keys."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import socket
import ssl
import stat
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.x509.oid import NameOID

MAXIMUM = 16 * 1024
OWNER = "infrastructure-spire"


def timestamp(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError("SPIRE_CERTIFICATE_RENEWAL_NOT_READY")
    result = float(cast(int | float, value))
    if not math.isfinite(result):
        raise ValueError("SPIRE_CERTIFICATE_RENEWAL_NOT_READY")
    return result


def heartbeat(value: dict[str, Any]) -> dict[str, Any]:
    age = (
        datetime.now(UTC) - datetime.fromisoformat(value["updated_at"])
    ).total_seconds()
    if (
        value.get("owner") != OWNER
        or value.get("role") != "server"
        or type(value.get("ready")) is not bool
        or not 0 <= age < 15
        or not re.fullmatch(r"SPIRE_[A-Z0-9_]{1,120}", value.get("reason", ""))
    ):
        raise ValueError("SPIRE_OBSERVATION_STALE_OR_INVALID")
    return {
        key: value[key] for key in ("owner", "role", "ready", "reason", "updated_at")
    }


def renewal(value: dict[str, Any], identities: set[str]) -> dict[str, Any]:
    now = time.time()
    if (
        value.get("owner") != "telemetry-certificate-renewal"
        or value.get("consumer_owner") != OWNER
        or value.get("schema_version") != 1
        or value.get("renewal_healthy") is not True
        or value.get("ready") is not True
    ):
        raise ValueError("SPIRE_CERTIFICATE_RENEWAL_NOT_READY")
    for name in ("checked_at", "last_success_at"):
        number = timestamp(value.get(name))
        if number < 0 or number > now or (name == "checked_at" and now - number > 180):
            raise ValueError("SPIRE_CERTIFICATE_RENEWAL_NOT_READY")
    raw = value.get("certificates")
    if not isinstance(raw, list):
        raise ValueError("SPIRE_CERTIFICATE_RENEWAL_NOT_READY")
    items = cast(list[object], raw)
    if len(items) != len(identities):
        raise ValueError("SPIRE_CERTIFICATE_RENEWAL_NOT_READY")
    certificates: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw_item in items:
        if not isinstance(raw_item, dict):
            raise ValueError("SPIRE_CERTIFICATE_RENEWAL_NOT_READY")
        item = cast(dict[str, Any], raw_item)
        if (
            not isinstance(item.get("id"), str)
            or item["id"] not in identities
            or item["id"] in names
            or item.get("healthy") is not True
        ):
            raise ValueError("SPIRE_CERTIFICATE_RENEWAL_NOT_READY")
        for name in ("certificate_expires_at", "ca_expires_at"):
            number = timestamp(item.get(name))
            if number - now <= 30 * 86400:
                raise ValueError("SPIRE_CERTIFICATE_RENEWAL_NOT_READY")
        names.add(item["id"])
        certificates.append(
            {
                key: item[key]
                for key in ("id", "healthy", "certificate_expires_at", "ca_expires_at")
            }
        )
    if names != identities:
        raise ValueError("SPIRE_CERTIFICATE_RENEWAL_NOT_READY")
    return {
        **{
            key: value[key]
            for key in (
                "owner",
                "consumer_owner",
                "schema_version",
                "renewal_healthy",
                "ready",
                "checked_at",
                "last_success_at",
            )
        },
        "certificates": certificates,
    }


def material(path: Path, *, private: bool = False) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError("SPIRE_PUBLIC_SOURCE_MATERIAL_INVALID")
        if private and (
            info.st_mode & 0o027 or info.st_mode & 0o040 and info.st_gid != os.getgid()
        ):
            raise ValueError("SPIRE_PUBLIC_SOURCE_KEY_PERMISSIONS_INVALID")
        value = source.read(65537)
    if not 0 < len(value) <= 65536:
        raise ValueError("SPIRE_PUBLIC_SOURCE_MATERIAL_INVALID")
    return value


def source_renewal(path: Path, identity: PeerTLS) -> dict[str, Any]:
    """Bind a fresh issuer receipt to this source's actual mounted leaf."""
    identity.current()
    return leaf_renewal(path, {"public-source": identity.root / identity.certificate})


def leaf_renewal(path: Path, leaves: dict[str, Path]) -> dict[str, Any]:
    """A role reads only its own receipt and installed public certificates."""
    raw = material(path)
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError("SPIRE_CERTIFICATE_RENEWAL_NOT_READY")
    value = cast(dict[str, Any], decoded)
    sanitized = renewal(value, set(leaves))
    for item in value["certificates"]:
        certificate_raw = material(leaves[item["id"]])
        certificate = x509.load_pem_x509_certificate(certificate_raw)
        if (
            item.get("certificate_sha256")
            != hashlib.sha256(certificate_raw).hexdigest()
            or item["certificate_expires_at"]
            != certificate.not_valid_after_utc.timestamp()
        ):
            raise ValueError("SPIRE_CERTIFICATE_RENEWAL_NOT_READY")
    return sanitized


def peer_matches(certificate: object, identities: tuple[str, ...]) -> bool:
    if not isinstance(certificate, dict):
        return False
    certificate = cast(dict[str, Any], certificate)
    names = [
        value
        for group in certificate.get("subject", ())
        for kind, value in group
        if kind == "commonName"
    ]
    uris = [
        value for kind, value in certificate.get("subjectAltName", ()) if kind == "URI"
    ]
    return any(
        (identity.startswith("cn:") and names == [identity[3:]])
        or (identity.startswith("spiffe://") and uris == [identity])
        for identity in identities
    )


def own_matches(certificate: x509.Certificate, expected: str) -> bool:
    if expected.startswith("cn:"):
        return [
            name.value
            for name in certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        ] == [expected[3:]]
    try:
        return expected.startswith(
            "spiffe://"
        ) and certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.UniformResourceIdentifier) == [expected]
    except x509.ExtensionNotFound:
        return False


def reject_password() -> str:
    raise ValueError("SPIRE_PUBLIC_SOURCE_ENCRYPTED_KEY_FORBIDDEN")


def principal(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("SPIRE_PUBLIC_SOURCE_IDENTITIES_REQUIRED")
    if value.startswith("cn:") and value[3:].strip() and value[3:] == value[3:].strip():
        return value
    parsed = urlsplit(value)
    if (
        parsed.scheme == "spiffe"
        and parsed.hostname
        and parsed.netloc == parsed.hostname
        and re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", parsed.path)
        and not any(item in {".", ".."} for item in parsed.path.split("/"))
        and not parsed.query
        and not parsed.fragment
    ):
        return value
    raise ValueError("SPIRE_PUBLIC_SOURCE_IDENTITIES_REQUIRED")


class PeerTLS:
    def __init__(
        self,
        root: Path,
        certificate: str,
        key: str,
        authority: str,
        self_identity: str,
        peer_identities: tuple[str, ...],
        *,
        server: bool,
    ) -> None:
        if not peer_identities:
            raise ValueError("SPIRE_PUBLIC_SOURCE_IDENTITIES_REQUIRED")
        for identity in (self_identity, *peer_identities):
            principal(identity)
        if any(Path(name).name != name for name in (certificate, key, authority)):
            raise ValueError("SPIRE_PUBLIC_SOURCE_MATERIAL_PATH_INVALID")
        self.root, self.certificate, self.key, self.authority = (
            root,
            certificate,
            key,
            authority,
        )
        self.self_identity, self.peer_identities, self.server = (
            self_identity,
            peer_identities,
            server,
        )
        self.lock = threading.Lock()
        self.context: ssl.SSLContext | None = None
        self.fingerprint: bytes | None = None

    def current(self) -> ssl.SSLContext:
        with self.lock:
            paths = (
                self.root / self.certificate,
                self.root / self.key,
                self.root / self.authority,
            )
            raw = (
                material(paths[0]),
                material(paths[1], private=True),
                material(paths[2]),
            )
            chain = x509.load_pem_x509_certificates(raw[0])
            authorities = x509.load_pem_x509_certificates(raw[2])
            now = datetime.now(UTC)
            if (
                not chain
                or not authorities
                or not own_matches(chain[0], self.self_identity)
                or any(
                    not certificate.not_valid_before_utc
                    <= now
                    < certificate.not_valid_after_utc
                    for certificate in [*chain, *authorities]
                )
            ):
                raise ValueError("SPIRE_PUBLIC_SOURCE_IDENTITY_INVALID")
            fingerprint = hashlib.sha256(b"\0".join(raw)).digest()
            if fingerprint == self.fingerprint and self.context is not None:
                return self.context
            context = ssl.SSLContext(
                ssl.PROTOCOL_TLS_SERVER if self.server else ssl.PROTOCOL_TLS_CLIENT
            )
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.verify_mode = ssl.CERT_REQUIRED
            context.verify_flags |= ssl.VERIFY_X509_STRICT
            context.load_verify_locations(cadata=raw[2].decode("ascii"))
            context.load_cert_chain(paths[0], paths[1], password=reject_password)
            if raw != (
                material(paths[0]),
                material(paths[1], private=True),
                material(paths[2]),
            ):
                raise ValueError("SPIRE_PUBLIC_SOURCE_MATERIAL_CHANGED")
            self.context, self.fingerprint = context, fingerprint
            return context


def origin(value: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise ValueError("SPIRE_PUBLIC_SOURCE_HTTPS_REQUIRED")
    return parsed.hostname, parsed.port or 443


class RemoteSource:
    def __init__(self, url: str, identity: PeerTLS, timeout: float = 2) -> None:
        self.host, self.port = origin(url)
        if not 0 < timeout <= 5 or identity.server:
            raise ValueError("SPIRE_PUBLIC_SOURCE_CLIENT_INVALID")
        self.identity, self.timeout = identity, timeout

    def read(self) -> dict[str, Any]:
        context = self.identity.current()
        connection = http.client.HTTPSConnection(
            self.host, self.port, timeout=self.timeout, context=context
        )
        started = time.monotonic()
        timer: threading.Timer | None = None
        try:
            connection.connect()
            sock = connection.sock
            if (
                sock is None
                or sock.context is not self.identity.current()
                or not peer_matches(sock.getpeercert(), self.identity.peer_identities)
            ):
                raise ValueError("SPIRE_PUBLIC_SOURCE_PEER_INVALID")
            remaining = self.timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise ValueError("SPIRE_PUBLIC_SOURCE_UNAVAILABLE")
            # A socket timeout alone permits an endless slow trickle. Bound the
            # whole header/body exchange as well as individual socket calls.
            timer = threading.Timer(remaining, interrupt, (sock,))
            timer.daemon = True
            timer.start()
            connection.request("GET", "/v1/public-state")
            response = connection.getresponse()
            raw = response.read(MAXIMUM + 1)
            if (
                response.status != 200
                or len(raw) > MAXIMUM
                or time.monotonic() - started > self.timeout
            ):
                raise ValueError("SPIRE_PUBLIC_SOURCE_UNAVAILABLE")
            if sock.context is not self.identity.current():
                raise ValueError("SPIRE_PUBLIC_SOURCE_MATERIAL_CHANGED")
            value: object = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("SPIRE_PUBLIC_SOURCE_RESPONSE_INVALID")
            value = cast(dict[str, Any], value)
            if (
                set(value) != {"schema_version", "owner", "heartbeat", "renewal"}
                or value.get("schema_version") != 1
                or value.get("owner") != OWNER
                or not isinstance(value.get("heartbeat"), dict)
            ):
                raise ValueError("SPIRE_PUBLIC_SOURCE_RESPONSE_INVALID")
            return value
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()


def interrupt(sock: socket.socket) -> None:
    with suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


class PublicServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        address: tuple[str, int],
        identity: PeerTLS,
        capture: Callable[[], dict[str, Any]],
    ) -> None:
        if not identity.server:
            raise ValueError("SPIRE_PUBLIC_SOURCE_SERVER_INVALID")
        identity.current()  # Reject an invalid identity before opening the port.
        self.identity, self.capture = identity, capture
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, PublicHandler)
        self.timeout = 0.5

    def get_request(self) -> tuple[ssl.SSLSocket, Any]:
        sock, address = super().get_request()
        sock.settimeout(2)
        try:
            return self.identity.current().wrap_socket(
                sock, server_side=True, do_handshake_on_connect=False
            ), address
        except BaseException:
            sock.close()
            raise

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        timer = threading.Timer(2, interrupt, (request,))
        timer.daemon = True
        timer.start()
        try:
            request.do_handshake()
            if request.context is not self.identity.current() or not peer_matches(
                request.getpeercert(), self.identity.peer_identities
            ):
                self.shutdown_request(request)
                return
            super().process_request_thread(request, client_address)
        except (OSError, ValueError):
            self.shutdown_request(request)
        finally:
            timer.cancel()
            self.slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        return  # Never emit peer certificate, input path or request text.


class PublicHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = cast(PublicServer, self.server)
        code, body = 404, b"{}"
        if self.path == "/v1/public-state":
            try:
                value = server.capture()
                # capture owns sanitization; serialization is bounded before IO.
                body = json.dumps(
                    value, allow_nan=False, separators=(",", ":")
                ).encode()
                if len(body) > MAXIMUM:
                    raise ValueError("SPIRE_PUBLIC_SOURCE_RESPONSE_INVALID")
                if (
                    cast(ssl.SSLSocket, self.connection).context
                    is not server.identity.current()
                ):
                    raise ValueError("SPIRE_PUBLIC_SOURCE_MATERIAL_CHANGED")
                code = 200
            except (OSError, ValueError, KeyError, TypeError):
                code, body = 503, b'{"code":"SPIRE_PUBLIC_SOURCE_UNAVAILABLE"}'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return
