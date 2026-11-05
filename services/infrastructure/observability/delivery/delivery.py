"""Durable technical telemetry delivery; no consumer catalogue or domain models."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from delivery_health import clear as clear_heartbeat, publish as publish_heartbeat

PATHS = {
    "/v1/logs": "logs",
    "/v1/traces": "traces",
    "/v1/metrics": "otlp_metrics",
    "/api/v1/write": "metrics",
}
HEADER_NAMES = (
    "Content-Type",
    "Content-Encoding",
    "X-Prometheus-Remote-Write-Version",
)
LOG = logging.getLogger("delivery")


class CapacityError(Exception):
    """New data cannot be durably accepted; existing data stays intact."""


def record_failure(settings: Settings, failure: dict[str, str | float]) -> None:
    """Persist only bounded internal reason codes, never request or error text."""
    payload = json.dumps(
        {
            "owner": "telemetry-delivery",
            "mode": settings.mode,
            "recorded_at": datetime.now(UTC).isoformat(),
            **failure,
        },
        sort_keys=True,
    ).encode()
    LOG.error(payload.decode())
    try:
        with (settings.journal / "failure-history.jsonl").open("ab") as history:
            os.fchmod(history.fileno(), 0o600)
            history.write(payload + b"\n")
            history.flush()
            os.fsync(history.fileno())
        with tempfile.NamedTemporaryFile(dir=settings.journal, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, settings.journal / "last-failure.json")
        descriptor = os.open(settings.journal, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        LOG.error('{"event":"delivery_failure_report_unavailable"}')


@dataclass(frozen=True)
class Record:
    sequence: int
    identifier: str
    kind: str
    headers: dict[str, str]
    body: bytes
    digest: str


def digest_of(kind: str, headers: dict[str, str], body: bytes) -> str:
    metadata = json.dumps([kind, headers], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(metadata.encode() + b"\x00" + body).hexdigest()


class Journal:
    """A single writer, FULL-synchronous WAL, bounded admission, no pending TTL."""

    def __init__(self, directory: Path, capacity: int) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "delivery.sqlite3"
        self.capacity = capacity
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS records ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,"
                "kind TEXT NOT NULL, headers TEXT NOT NULL, body BLOB NOT NULL,"
                "digest TEXT NOT NULL, projected INTEGER NOT NULL DEFAULT 0,"
                "created_ns INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,"
                "error TEXT NOT NULL DEFAULT '')"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS pending ON records(projected, kind, sequence)"
            )
            self.prepare_counters(db)
        self.path.chmod(0o600)
        # Persist the new directory entry before the first admission ACK.
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def prepare_counters(db: sqlite3.Connection) -> None:
        """Atomically backfill once; triggers also cover compatible old writers."""
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='record_counters_v1'"
        ).fetchone()
        if exists is None:
            db.execute(
                "CREATE TABLE record_counters_v1 (kind TEXT PRIMARY KEY,"
                "records INTEGER NOT NULL CHECK(records>=0),"
                "pending INTEGER NOT NULL CHECK(pending BETWEEN 0 AND records),"
                "bytes INTEGER NOT NULL CHECK(bytes>=0),"
                "failed INTEGER NOT NULL CHECK(failed BETWEEN 0 AND records))"
            )
            db.execute(
                "INSERT INTO record_counters_v1 "
                "SELECT kind,COUNT(*),SUM(projected=0),SUM(length(body)+1024),"
                "SUM(error!='') FROM records GROUP BY projected,kind "
                "ON CONFLICT(kind) DO UPDATE SET records=records+excluded.records,"
                "pending=pending+excluded.pending,bytes=bytes+excluded.bytes,"
                "failed=failed+excluded.failed"
            )
        for action, changes in (
            ("INSERT", (("NEW", 1),)),
            ("DELETE", (("OLD", -1),)),
            ("UPDATE", (("OLD", -1), ("NEW", 1))),
        ):
            statements = []
            for row, sign in changes:
                if sign == 1:
                    statements.append(
                        "INSERT INTO record_counters_v1 VALUES("
                        f"{row}.kind,1,({row}.projected=0),length({row}.body)+1024,"
                        f"({row}.error!='')) ON CONFLICT(kind) DO UPDATE SET "
                        "records=records+excluded.records,"
                        "pending=pending+excluded.pending,bytes=bytes+excluded.bytes,"
                        "failed=failed+excluded.failed;"
                    )
                else:
                    statements.append(
                        "UPDATE record_counters_v1 SET records=records-1,"
                        f"pending=pending-({row}.projected=0),"
                        f"bytes=bytes-(length({row}.body)+1024),"
                        f"failed=failed-({row}.error!='') WHERE kind={row}.kind;"
                    )
            db.execute(
                f"CREATE TRIGGER IF NOT EXISTS record_counters_v1_{action.lower()} "
                f"AFTER {action} ON records BEGIN {''.join(statements)} END"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def accept(
        self, kind: str, headers: dict[str, str], body: bytes, identifier: str
    ) -> str:
        uuid.UUID(identifier)
        digest = digest_of(kind, headers, body)
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT digest FROM records WHERE id=?", (identifier,)
            ).fetchone()
            if previous:
                if previous[0] != digest:
                    raise ValueError(
                        "delivery identity conflicts with immutable contents"
                    )
                return digest
            size = db.execute(
                "SELECT COALESCE(SUM(bytes),0) FROM record_counters_v1"
            ).fetchone()[0]
            free = os.statvfs(self.path.parent)
            if (
                size + len(body) + 1024 > self.capacity
                or free.f_bavail * free.f_frsize < len(body) + 1048576
            ):
                raise CapacityError("journal capacity exhausted")
            db.execute(
                "INSERT INTO records(id,kind,headers,body,digest,created_ns) VALUES(?,?,?,?,?,?)",
                (
                    identifier,
                    kind,
                    json.dumps(headers, sort_keys=True),
                    body,
                    digest,
                    time.time_ns(),
                ),
            )
        return digest

    def first(self, kind: str) -> Record | None:
        with self.lock, self.connect() as db:
            row = db.execute(
                "SELECT sequence,id,kind,headers,body,digest FROM records "
                "WHERE projected=0 AND kind=? ORDER BY sequence LIMIT 1",
                (kind,),
            ).fetchone()
        if row is None:
            return None
        record = Record(row[0], row[1], row[2], json.loads(row[3]), row[4], row[5])
        if digest_of(record.kind, record.headers, record.body) != record.digest:
            raise ValueError("journal integrity check failed")
        return record

    def complete(self, record: Record, *, archive: bool) -> None:
        with self.lock, self.connect() as db:
            if archive:
                db.execute(
                    "UPDATE records SET projected=1,error='' WHERE id=?",
                    (record.identifier,),
                )
            else:
                db.execute("DELETE FROM records WHERE id=?", (record.identifier,))

    def failed(self, record: Record, reason: str) -> None:
        with self.lock, self.connect() as db:
            db.execute(
                "UPDATE records SET attempts=attempts+1,error=? WHERE id=?",
                (reason, record.identifier),
            )

    def status(self) -> dict[str, int]:
        # WAL readers must not wait for the process-local admission/writer lock.
        with self.connect() as db:
            total, pending, size, failed = db.execute(
                "SELECT COALESCE(SUM(records),0),COALESCE(SUM(pending),0),"
                "COALESCE(SUM(bytes),0),COALESCE(SUM(failed),0) FROM record_counters_v1"
            ).fetchone()
        return {
            "records": total,
            "pending": pending,
            "bytes": size,
            "failed": failed,
            "capacity": self.capacity,
        }

    def blocking_errors(self) -> dict[str, str]:
        """Read only the indexed head of each signal; never expose stored payloads."""
        result = {}
        with self.connect() as db:
            for kind in PATHS.values():
                row = db.execute(
                    "SELECT error FROM records WHERE projected=0 AND kind=? "
                    "ORDER BY sequence LIMIT 1",
                    (kind,),
                ).fetchone()
                if row and row[0]:
                    result[kind] = safe_reason(row[0])
        return result

    def replay(self) -> None:
        """Reset only projection checkpoints; immutable archive bytes are unchanged."""
        with self.lock, self.connect() as db:
            db.execute("UPDATE records SET projected=0,error=''")

    def admission_available(self, request_bytes: int) -> bool:
        free = os.statvfs(self.path.parent)
        return (
            self.status()["bytes"] + request_bytes + 1024 <= self.capacity
            and free.f_bavail * free.f_frsize >= request_bytes + 1048576
        )


def safe_reason(reason: str) -> str:
    """Only fixed technical codes may leave the journal through diagnostics."""
    if reason in {
        "transport_failure",
        "transport_timeout",
        "tls_failure",
        "incomplete_ack",
    }:
        return reason
    if re.fullmatch(r"http_[1-5][0-9]{2}(_or_incomplete_ack)?", reason):
        return reason
    return "delivery_failed"


def failure_reason(error: BaseException) -> str:
    """Categorize without exception messages, URLs, credentials or payloads."""
    if isinstance(error, sqlite3.Error):
        return "journal_failure"
    if isinstance(error, ssl.SSLError):
        return "tls_failure"
    if isinstance(error, TimeoutError):
        return "transport_timeout"
    if isinstance(error, OSError | http.client.HTTPException):
        return "transport_failure"
    return "integrity_failure"


@dataclass(frozen=True)
class Settings:
    mode: str
    listen_host: str
    listen_port: int
    health_port: int
    journal: Path
    capacity_bytes: int
    request_bytes: int
    retry_seconds: float
    timeout_seconds: float
    server_cert: str
    server_key: str
    client_ca: str
    ca: str
    client_cert: str
    client_key: str
    health_ca: str
    health_cert: str
    health_key: str
    destinations: dict[str, str]
    prometheus_command: list[str]

    @classmethod
    def read(cls, path: Path) -> Settings:
        data = json.loads(path.read_text())
        if set(data) != set(cls.__dataclass_fields__):
            raise ValueError(
                "delivery config must contain exactly the documented settings"
            )
        data["journal"] = Path(data["journal"])
        result = cls(**data)
        if result.mode not in {"sender", "archive"}:
            raise ValueError("mode must be sender or archive")
        if set(result.destinations) != set(PATHS.values()):
            raise ValueError("all four technical destinations are required")
        for url in result.destinations.values():
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "destinations must be explicit HTTPS URLs without credentials"
                )
        if (
            not 1 <= result.listen_port <= 65535
            or not 1 <= result.health_port <= 65535
            or result.listen_port == result.health_port
            or min(
                result.capacity_bytes,
                result.request_bytes,
                result.retry_seconds,
                result.timeout_seconds,
            )
            <= 0
        ):
            raise ValueError("invalid delivery limits")
        if result.capacity_bytes <= result.request_bytes + 1024:
            raise ValueError("journal capacity must exceed one bounded request")
        if result.prometheus_command and result.mode != "sender":
            raise ValueError("source Prometheus may only run inside its owner's sender")
        if (
            result.prometheus_command
            and result.prometheus_command[0] != "/bin/prometheus"
        ):
            raise ValueError(
                "the supervised process must be the pinned Prometheus binary"
            )
        return result


def client_tls(settings: Settings) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=settings.ca)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(settings.client_cert, settings.client_key)
    return context


class ReloadingTLS:
    """Adopt an atomically replaced leaf; never silently change key or trust."""

    def __init__(
        self, certificate: str, key: str, ca: str, factory: Callable[[], ssl.SSLContext]
    ) -> None:
        self.paths = tuple(Path(value) for value in (certificate, key, ca))
        self.factory = factory
        self.lock = threading.Lock()
        initial = self.material()
        self.bound = initial[1:]
        self.context = factory()
        if self.material() != initial:
            raise ssl.SSLError("TLS_MATERIAL_CHANGED_DURING_LOAD")
        self.certificate = initial[0]

    def material(self) -> tuple[str, ...]:
        """Hash bounded local files, without parsing requests or exposing bytes."""
        result: list[str] = []
        for path in self.paths:
            if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                raise ssl.SSLError("TLS_MATERIAL_INVALID")
            with path.open("rb") as stream:
                value = stream.read(1024 * 1024 + 1)
            if len(value) > 1024 * 1024:
                raise ssl.SSLError("TLS_MATERIAL_INVALID")
            result.append(hashlib.sha256(value).hexdigest())
        return tuple(result)

    def get(self) -> ssl.SSLContext:
        """Keep accepted connections; reject bad replacement on new handshakes."""
        with self.lock:
            current = self.material()
            if current[1:] != self.bound:
                raise ssl.SSLError("TLS_BOUND_KEY_OR_TRUST_CHANGED")
            if current[0] != self.certificate:
                context = self.factory()
                if self.material() != current:
                    raise ssl.SSLError("TLS_MATERIAL_CHANGED_DURING_LOAD")
                self.context = context
                self.certificate = current[0]
            return self.context


def backend_ack(kind: str, body: bytes) -> bool:
    """OTLP partial success is not an ACK. Unknown protobuf response is retained."""
    if not body or (kind != "metrics" and body == b"\x0a\x00"):
        return True
    if kind == "metrics":
        return False
    try:
        response = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(response, dict):
        return False
    partial = response.get("partialSuccess", response.get("partial_success"))
    if set(response) - {"partialSuccess", "partial_success"}:
        return False
    if not partial:
        return True
    if not isinstance(partial, dict):
        return False
    fields = {
        "logs": ("rejectedLogRecords", "rejected_log_records"),
        "traces": ("rejectedSpans", "rejected_spans"),
        "otlp_metrics": ("rejectedDataPoints", "rejected_data_points"),
    }[kind]
    if set(partial) - {*fields, "errorMessage", "error_message"}:
        return False
    counters = [partial[field] for field in fields if field in partial]
    # An explicitly zero rejection count means full acceptance, even with a warning.
    return bool(counters) and all(
        (type(value) is int and value == 0) or value == "0" for value in counters
    )


class Delivery:
    """Retry every rejection without deleting data or logging its contents."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.journal.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lease = (settings.journal / ".delivery.lock").open("a")
        fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.journal = Journal(settings.journal, settings.capacity_bytes)
        with self.journal.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS metadata (mode TEXT NOT NULL)")
            mode = db.execute("SELECT mode FROM metadata").fetchone()
            if mode is None:
                db.execute("INSERT INTO metadata(mode) VALUES(?)", (settings.mode,))
            elif mode[0] != settings.mode:
                raise ValueError(
                    "journal mode cannot be changed; archive ownership is immutable"
                )
        self.outbound_tls = ReloadingTLS(
            settings.client_cert,
            settings.client_key,
            settings.ca,
            lambda: client_tls(settings),
        )
        self.stop = threading.Event()
        self.pressure = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_progress = time.monotonic()
        self.phase = "STARTING"
        self.worker_error = "NONE"
        if not self.journal.admission_available(settings.request_bytes):
            self.pressure.set()

    def forward(self, record: Record) -> bool:
        url = urlsplit(self.settings.destinations[record.kind])
        try:
            context = self.outbound_tls.get()
        except (OSError, ValueError):
            self.journal.failed(record, "tls_failure")
            return False
        connection = http.client.HTTPSConnection(
            url.hostname or "",
            url.port,
            timeout=self.settings.timeout_seconds,
            context=context,
        )
        headers = dict(record.headers)
        if self.settings.mode == "sender":
            headers["X-Aelev-Delivery-Id"] = record.identifier
        try:
            connection.request("POST", url.path, body=record.body, headers=headers)
            response = connection.getresponse()
            body = response.read(65537)
            accepted = 200 <= response.status < 300
            if accepted and self.settings.mode == "sender":
                try:
                    receipt = json.loads(body)
                    accepted = (
                        isinstance(receipt, dict)
                        and receipt.get("durable") is True
                        and receipt
                        == {
                            "id": record.identifier,
                            "sha256": record.digest,
                            "durable": True,
                        }
                    )
                except (ValueError, UnicodeDecodeError):
                    accepted = False
            elif accepted:
                accepted = backend_ack(record.kind, body)
            if not accepted:
                self.journal.failed(
                    record,
                    (
                        "incomplete_ack"
                        if 200 <= response.status < 300
                        else f"http_{response.status}"
                    ),
                )
                return False
            self.journal.complete(record, archive=self.settings.mode == "archive")
            return True
        except (OSError, http.client.HTTPException) as error:
            self.journal.failed(record, failure_reason(error))
            return False
        finally:
            connection.close()

    def worker_alive(self) -> bool:
        """Distinguish a progressing retry loop from a dead or stalled worker."""
        return (
            self.worker is not None
            and self.worker.is_alive()
            and not self.stop.is_set()
            and time.monotonic() - self.last_progress
            <= self.settings.timeout_seconds * 4 + self.settings.retry_seconds + 5
        )

    def work(self) -> None:
        try:
            while not self.stop.is_set():
                progressed = False
                for kind in PATHS.values():
                    self.last_progress = time.monotonic()
                    self.phase = "JOURNAL_READ_" + kind.upper()
                    record = self.journal.first(kind)
                    if record is not None:
                        self.phase = "FORWARD_" + kind.upper()
                        progressed = self.forward(record) or progressed
                    self.last_progress = time.monotonic()
                self.phase = "CAPACITY_CHECK"
                if self.journal.admission_available(self.settings.request_bytes):
                    self.pressure.clear()
                else:
                    self.pressure.set()
                if not progressed:
                    self.phase = "RETRY_WAIT"
                    self.stop.wait(self.settings.retry_seconds)
        except (OSError, sqlite3.Error, ValueError) as error:
            self.worker_error = failure_reason(error)
            LOG.error(
                json.dumps(
                    {"event": "delivery_worker_failed", "reason": failure_reason(error)}
                )
            )
            self.pressure.set()
            self.stop.set()


class DeliveryServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, delivery: Delivery, *, health_only: bool = False) -> None:
        self.delivery = delivery
        self.health_only = health_only
        self.last_progress = time.monotonic()
        self.listener_phase = ("INITIALIZING", self.last_progress)
        port = (
            delivery.settings.health_port
            if health_only
            else delivery.settings.listen_port
        )
        super().__init__((delivery.settings.listen_host, port), Handler)
        # select() readiness can disappear before accept(), e.g. a client RST.
        # Never block the accept-loop heartbeat on that race. Accepted sockets
        # still receive the bounded TLS/request timeout in get_request().
        self.socket.setblocking(False)

        def factory() -> ssl.SSLContext:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            tls.verify_mode = ssl.CERT_REQUIRED
            tls.load_verify_locations(
                delivery.settings.health_ca
                if health_only
                else delivery.settings.client_ca
            )
            tls.load_cert_chain(
                delivery.settings.server_cert, delivery.settings.server_key
            )
            return tls

        self.tls = ReloadingTLS(
            delivery.settings.server_cert,
            delivery.settings.server_key,
            delivery.settings.health_ca if health_only else delivery.settings.client_ca,
            factory,
        )
        self.slots = threading.BoundedSemaphore(16)

    def service_actions(self) -> None:
        """Expose accept-loop progress to this process's supervisor only."""
        self.last_progress = time.monotonic()
        self.listener_phase = ("POLL_WAIT", self.last_progress)

    def get_request(self) -> tuple[ssl.SSLSocket, tuple[str, int]]:
        self.listener_phase = ("ACCEPT", time.monotonic())
        connection, address = super().get_request()
        connection.settimeout(self.delivery.settings.timeout_seconds)
        self.listener_phase = ("TLS_SETUP", time.monotonic())
        try:
            return (
                self.tls.get().wrap_socket(
                    connection, server_side=True, do_handshake_on_connect=False
                ),
                address,
            )
        except (OSError, ValueError):
            connection.close()
            raise

    def process_request(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any
    ) -> None:
        self.listener_phase = ("SLOT_CHECK", time.monotonic())
        if not self.slots.acquire(blocking=False):
            self.listener_phase = ("SLOT_REJECT_CLOSE", time.monotonic())
            self.shutdown_request(request)
            return
        try:
            self.listener_phase = ("REQUEST_DISPATCH", time.monotonic())
            super().process_request(request, client_address)
            self.listener_phase = ("REQUEST_DISPATCHED", time.monotonic())
        except RuntimeError:
            self.slots.release()
            raise

    def process_request_thread(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server: DeliveryServer

    def log_message(self, format: str, *args: Any) -> None:
        # No request paths, headers, payloads, certificates or user identifiers in logs.
        return

    def reply(self, status: int, body: Mapping[str, object]) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        delivery = self.server.delivery
        if not self.server.health_only:
            self.reply(403, {"error": "ingestion identity cannot inspect the journal"})
        elif self.path == "/healthz":
            self.reply(200, {"status": "ok"})
        elif self.path in {"/health", "/status", "/readyz"}:
            status: dict[str, object] = dict(delivery.journal.status())
            status["worker_alive"] = int(delivery.worker_alive())
            status["admission_pressure"] = int(delivery.pressure.is_set())
            status["blocking_errors"] = delivery.journal.blocking_errors()
            ready = (
                status["worker_alive"]
                and not status["failed"]
                and not delivery.pressure.is_set()
            )
            if self.path == "/readyz":
                self.reply(
                    200 if ready else 503,
                    {"status": "ready"} if ready else {"error": "delivery_not_ready"},
                )
            else:
                self.reply(200 if ready or self.path == "/status" else 503, status)
        elif self.path == "/metrics":
            counters = delivery.journal.status()
            content = "".join(
                f"aelev_telemetry_delivery_{key} {value}\n"
                for key, value in counters.items()
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.reply(404, {"error": "unknown technical endpoint"})

    def do_POST(self) -> None:
        if self.server.health_only:
            self.reply(403, {"error": "query identity cannot publish"})
            return
        delivery = self.server.delivery
        settings = delivery.settings
        kind = PATHS.get(self.path)
        if kind is None:
            self.reply(404, {"error": "unknown technical endpoint"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if (
                self.headers.get("Transfer-Encoding")
                or not 0 < length <= settings.request_bytes
            ):
                delivery.pressure.set()
                self.reply(
                    503, {"error": "explicit bounded Content-Length required; retry"}
                )
                return
            headers = {
                name: self.headers[name]
                for name in HEADER_NAMES
                if name in self.headers
            }
            if headers.get("Content-Type", "").split(";", 1)[0] not in {
                "application/json",
                "application/x-protobuf",
            }:
                delivery.pressure.set()
                self.reply(
                    503,
                    {
                        "error": "standard OTLP or remote_write content type required; retry"
                    },
                )
                return
            self.connection.settimeout(settings.timeout_seconds)
            body = self.rfile.read(length)
            if len(body) != length:
                self.reply(400, {"error": "incomplete body"})
                return
            identifier = self.headers.get("X-Aelev-Delivery-Id", str(uuid.uuid4()))
            digest = delivery.journal.accept(kind, headers, body, identifier)
            receipt = {"id": identifier, "sha256": digest, "durable": True}
            # Native SDKs receive the standard empty OTLP response; relay peers
            # explicitly request the durable receipt with their stable batch ID.
            self.reply(200, receipt if "X-Aelev-Delivery-Id" in self.headers else {})
        except ValueError:
            self.reply(409, {"error": "invalid or conflicting delivery identity"})
        except (CapacityError, OSError, sqlite3.Error) as error:
            delivery.pressure.set()
            LOG.warning(
                json.dumps(
                    {
                        "event": "durable_admission_failed",
                        "reason": (
                            "capacity_exhausted"
                            if isinstance(error, CapacityError)
                            else failure_reason(error)
                        ),
                    }
                )
            )
            self.reply(
                503,
                {"error": "durable admission unavailable; retry without discarding"},
            )


def stop_prometheus(child: subprocess.Popen[bytes]) -> None:
    """Bounded shutdown of this cell's child only, including pressure handling."""
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        LOG.warning('{"event":"source_prometheus_forced_stop"}')
        child.kill()
        child.wait(timeout=5)


def stop_listener(server: DeliveryServer) -> None:
    """Do not block process recovery forever on a stalled accept loop."""
    shutdown = threading.Thread(target=server.shutdown, daemon=True)
    shutdown.start()
    shutdown.join(timeout=2)
    if shutdown.is_alive():
        LOG.error('{"event":"delivery_listener_shutdown_timeout"}')
    server.server_close()


def failure_context(
    delivery: Delivery,
    listener: threading.Thread,
    health_listener: threading.Thread,
    server: DeliveryServer,
    health: DeliveryServer,
    supervisor_gap: float,
) -> dict[str, str | float]:
    """Snapshot three owned threads without locals, source lines or paths."""
    now = time.monotonic()
    result: dict[str, str | float] = {
        "supervisor_gap_seconds": round(max(0, supervisor_gap), 3),
        "worker_phase": delivery.phase,
        "worker_progress_age_seconds": round(max(0, now - delivery.last_progress), 3),
    }
    for label, endpoint in (("ingestion", server), ("health", health)):
        phase, since = endpoint.listener_phase
        result[label + "_phase"] = phase
        result[label + "_phase_age_seconds"] = round(max(0, now - since), 3)
        result[label + "_progress_age_seconds"] = round(
            max(0, now - endpoint.last_progress), 3
        )
    # Names are static code locations, not formatted traceback text. Exclude
    # every other thread and never inspect frame locals, arguments or source.
    allowed = {
        "delivery.py",
        "socket.py",
        "ssl.py",
        "socketserver.py",
        "threading.py",
        "selectors.py",
        "client.py",
        "server.py",
        "contextlib.py",
    }
    stacks: dict[str, list[dict[str, str | int]]] = {}
    frames = sys._current_frames()
    try:
        for label, thread in (
            ("worker", delivery.worker),
            ("ingestion", listener),
            ("health", health_listener),
        ):
            locations: list[dict[str, str | int]] = []
            identity = thread.ident if thread is not None else None
            frame = frames.get(identity) if identity is not None else None
            while frame is not None and len(locations) < 8:
                name = Path(frame.f_code.co_filename).name
                own = name in allowed
                locations.append(
                    {
                        "file": name if own else "EXTERNAL",
                        "function": frame.f_code.co_name[:80] if own else "EXTERNAL",
                        "line": frame.f_lineno if own else 0,
                    }
                )
                frame = frame.f_back
            stacks[label] = locations
        result["thread_stacks_json"] = json.dumps(stacks, sort_keys=True)
    finally:
        del frames
    return result


def critical_failure(
    delivery: Delivery,
    listener: threading.Thread,
    health_listener: threading.Thread,
    server: DeliveryServer,
    health: DeliveryServer,
) -> dict[str, str | float] | None:
    """Explain an observed failure without exposing packet or destination data."""
    now = time.monotonic()
    if not delivery.worker_alive():
        return {
            "code": (
                "WORKER_STOPPED"
                if delivery.worker is None or not delivery.worker.is_alive()
                else "WORKER_STALLED"
            ),
            "phase": delivery.phase,
            "worker_error": delivery.worker_error,
            "progress_age_seconds": round(max(0, now - delivery.last_progress), 3),
        }
    for label, thread, endpoint in (
        ("INGESTION", listener, server),
        ("HEALTH", health_listener, health),
    ):
        if not thread.is_alive():
            return {"code": label + "_LISTENER_STOPPED"}
        age = max(0, now - endpoint.last_progress)
        if age > 5:
            return {
                "code": label + "_LISTENER_STALLED",
                "progress_age_seconds": round(age, 3),
            }
    return None


def run(settings: Settings) -> None:
    delivery = Delivery(settings)
    clear_heartbeat()
    server = DeliveryServer(delivery)
    health = DeliveryServer(delivery, health_only=True)
    worker = threading.Thread(target=delivery.work, daemon=True)
    delivery.worker = worker
    listener = threading.Thread(target=server.serve_forever, daemon=True)
    health_listener = threading.Thread(target=health.serve_forever, daemon=True)
    worker.start()
    listener.start()
    health_listener.start()
    child: subprocess.Popen[bytes] | None = None
    stop_requested = False

    def finish(signum: int, frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True
        delivery.stop.set()

    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    failed_since: float | None = None
    supervisor_at = time.monotonic()
    failure_max_gap = 0.0
    heartbeat_at = 0.0
    try:
        while not delivery.stop.wait(0.25):
            now = time.monotonic()
            supervisor_gap, supervisor_at = max(0, now - supervisor_at), now
            failure = critical_failure(
                delivery, listener, health_listener, server, health
            )
            if failure is not None:
                failure_max_gap = max(failure_max_gap, supervisor_gap)
                if failed_since is None:
                    failed_since = time.monotonic()
                elif time.monotonic() - failed_since >= 3:
                    failure.update(
                        failure_context(
                            delivery,
                            listener,
                            health_listener,
                            server,
                            health,
                            failure_max_gap,
                        )
                    )
                    # Diagnostics can yield to the worker. Never terminate it
                    # using a failure that disappeared while collecting context.
                    confirmed = critical_failure(
                        delivery, listener, health_listener, server, health
                    )
                    if confirmed is None or confirmed["code"] != failure["code"]:
                        failed_since = None
                        failure_max_gap = 0.0
                        continue
                    failure.update(confirmed)
                    record_failure(settings, failure)
                    # Durable failure history may itself wait for storage. Keep
                    # that history, but do not kill a loop that has since resumed.
                    confirmed = critical_failure(
                        delivery, listener, health_listener, server, health
                    )
                    if confirmed is None or confirmed["code"] != failure["code"]:
                        failed_since = None
                        failure_max_gap = 0.0
                        continue
                    raise RuntimeError(
                        "delivery critical loop stopped or stalled: "
                        + str(failure["code"])
                    )
            else:
                failed_since = None
                failure_max_gap = 0.0
                if time.monotonic() - heartbeat_at >= 1:
                    try:
                        publish_heartbeat(settings.mode)
                    except OSError:
                        record_failure(
                            settings, {"code": "LIVENESS_HEARTBEAT_WRITE_FAILED"}
                        )
                        raise RuntimeError(
                            "delivery liveness signal unavailable"
                        ) from None
                    heartbeat_at = time.monotonic()
            # Stop only telemetry collection before the remote_write WAL can expire.
            # Business processes never run in this container and are never stopped.
            if settings.prometheus_command:
                if child is not None and (
                    delivery.pressure.is_set() or not worker.is_alive()
                ):
                    stop_prometheus(child)
                    child = None
                if (
                    child is None
                    and not delivery.pressure.is_set()
                    and worker.is_alive()
                ):
                    child = subprocess.Popen(settings.prometheus_command)
                if child is not None and child.poll() is not None:
                    record_failure(settings, {"code": "SOURCE_PROMETHEUS_EXITED"})
                    raise RuntimeError(
                        "source Prometheus exited; refusing unsupervised delivery"
                    )
        if not stop_requested:
            record_failure(
                settings,
                {
                    "code": "WORKER_FAILED",
                    "phase": delivery.phase,
                    "worker_error": delivery.worker_error,
                },
            )
            raise RuntimeError("delivery worker failed without an operator stop")
    finally:
        try:
            clear_heartbeat()
        except OSError:
            LOG.error('{"event":"delivery_liveness_cleanup_failed"}')
        delivery.stop.set()
        if child is not None:
            stop_prometheus(child)
        stop_listener(server)
        stop_listener(health)
        worker.join(timeout=settings.timeout_seconds + 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "replay", "health", "alive"))
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings.read(args.config)
    if args.action == "run":
        run(settings)
    elif args.action == "replay":
        if settings.mode != "archive":
            raise ValueError(
                "only the central immutable archive supports projection replay"
            )
        Journal(settings.journal, settings.capacity_bytes).replay()
    else:
        context = ssl.create_default_context(cafile=settings.health_ca)
        context.load_cert_chain(settings.health_cert, settings.health_key)
        connection = http.client.HTTPSConnection(
            "localhost",
            settings.health_port,
            context=context,
            timeout=settings.timeout_seconds,
        )
        try:
            connection.request(
                "GET", "/healthz" if args.action == "alive" else "/health"
            )
            response = connection.getresponse()
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                raise RuntimeError("delivery probe unavailable")
            if args.action == "alive" and body != b'{"status":"ok"}':
                raise RuntimeError("delivery liveness response invalid")
        finally:
            connection.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
