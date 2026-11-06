"""Own public health projection, bounded telemetry and presentation adapter."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import signal
import ssl
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import FrameType
from typing import Any, cast
from urllib.parse import urlsplit

from .files import atomic, read, reserve, writer
from .observation_diagnostics import Diagnostics

OWNER = "infrastructure-spire"
PREFIX = "AELEV_INFRASTRUCTURE_SPIRE_"
SOURCE = Path("/run/observation/heartbeat.json")
RENEWAL = Path("/run/certificate-renewal/status.json")
STATUS_RENEWAL_FILE = "status-publisher.json"
STATUS_QUERY_RENEWAL_FILE = "status-query.json"
TELEMETRY_IDENTITIES = {
    "metrics",
    "observer",
    "central-write",
    "local-query",
    "local-write",
    "sender",
}
STATUS_PUBLISHER_IDENTITIES = {"status-publisher"}
STATUS_QUERY_IDENTITIES = {"status-query"}
CAPACITY = 4096


def pending_files(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("*.json") if p.name != "owner.json")


def snapshot(path: Path = SOURCE) -> dict[str, Any]:
    value = read(path)
    age = (
        datetime.now(UTC) - datetime.fromisoformat(value["updated_at"])
    ).total_seconds()
    if (
        value.get("owner") != OWNER
        or type(value.get("ready")) is not bool
        or value.get("role") != "server"
        or not 0 <= age < 15
        or not re.fullmatch(r"SPIRE_[A-Z0-9_]{1,120}", value.get("reason", ""))
    ):
        raise ValueError("SPIRE_OBSERVATION_STALE_OR_INVALID")
    return value


def _renewal_receipt(path: Path, identities: set[str]) -> dict[str, Any]:
    value = read(path)
    now = time.time()
    certificates = value.get("certificates")
    if (
        value.get("owner") != "telemetry-certificate-renewal"
        or value.get("consumer_owner") != OWNER
        or value.get("schema_version") != 1
        or value.get("renewal_healthy") is not True
        or value.get("ready") is not True
        or type(value.get("checked_at")) not in (int, float)
        or not 0 <= now - float(value["checked_at"]) <= 180
        or type(value.get("last_success_at")) not in (int, float)
        or not isinstance(certificates, list)
        or len(certificates) != len(identities)
    ):
        raise ValueError("SPIRE_CERTIFICATE_RENEWAL_NOT_READY")
    names: set[str] = set()
    for item in certificates:
        if (
            not isinstance(item, dict)
            or item.get("id") not in identities
            or item["id"] in names
            or item.get("healthy") is not True
            or type(item.get("certificate_expires_at")) not in (int, float)
            or type(item.get("ca_expires_at")) not in (int, float)
            or min(float(item["certificate_expires_at"]), float(item["ca_expires_at"]))
            - now
            <= 30 * 86400
        ):
            raise ValueError("SPIRE_CERTIFICATE_RENEWAL_NOT_READY")
        names.add(item["id"])
    if names != identities:
        raise ValueError("SPIRE_CERTIFICATE_RENEWAL_NOT_READY")
    return value


def renewal_snapshot(path: Path = RENEWAL) -> dict[str, Any]:
    """Require both telemetry and status-client receipts without issuer access."""
    telemetry = _renewal_receipt(path, TELEMETRY_IDENTITIES)
    status_query = _renewal_receipt(
        path.with_name(STATUS_QUERY_RENEWAL_FILE), STATUS_QUERY_IDENTITIES
    )
    status_publisher = _renewal_receipt(
        path.with_name(STATUS_RENEWAL_FILE), STATUS_PUBLISHER_IDENTITIES
    )
    return {
        **telemetry,
        "last_success_at": min(
            float(telemetry["last_success_at"]),
            float(status_query["last_success_at"]),
            float(status_publisher["last_success_at"]),
        ),
        "certificates": (
            telemetry["certificates"]
            + status_query["certificates"]
            + status_publisher["certificates"]
        ),
    }


def metrics(
    path: Path = SOURCE, queue: Path | None = None, renewal_path: Path = RENEWAL
) -> bytes:
    try:
        status = snapshot(path)
        fresh, ready = 1, int(status["ready"])
        age = (
            datetime.now(UTC) - datetime.fromisoformat(status["updated_at"])
        ).total_seconds()
    except (OSError, ValueError, KeyError, TypeError):
        fresh, ready, age = 0, 0, 15.0
    values = {"source_fresh": fresh, "native_ready": ready, "source_age_seconds": age}
    try:
        renewal = renewal_snapshot(renewal_path)
        renewal_ready = 1
        renewal_age = time.time() - float(renewal["last_success_at"])
        certificates = renewal["certificates"]
    except (OSError, ValueError, KeyError, TypeError):
        renewal_ready, renewal_age, certificates = 0, 181.0, []
    values.update(
        certificate_renewal_healthy=renewal_ready,
        certificate_renewal_ready=renewal_ready,
        certificate_renewal_last_success_age_seconds=renewal_age,
    )
    if queue is not None:
        values["telemetry_pending"] = len(pending_files(queue))
    result = "".join(
        f"# TYPE aelev_spire_{name} gauge\naelev_spire_{name} {value}\n"
        for name, value in values.items()
    )
    for item in certificates:
        identity = item["id"]
        result += (
            'aelev_spire_certificate_expires_at{identity="'
            + identity
            + '"} '
            + str(item["certificate_expires_at"])
            + "\n"
            + 'aelev_spire_certificate_ca_expires_at{identity="'
            + identity
            + '"} '
            + str(item["ca_expires_at"])
            + "\n"
        )
    return result.encode()


def packet(status: dict[str, Any], started: int, ended: int) -> dict[str, bytes]:
    attributes = [{"key": "service.name", "value": {"stringValue": OWNER}}]
    instance = os.environ.get(PREFIX + "UI_MODULE_ID")
    if instance:
        if str(uuid.UUID(instance)) != instance or uuid.UUID(instance).int == 0:
            raise ValueError("SPIRE_OBSERVATION_MODULE_ID_INVALID")
        attributes.append(
            {"key": "service.instance.id", "value": {"stringValue": instance}}
        )
    resource = {"attributes": attributes}
    code = status["reason"]
    occurred = (
        int(datetime.fromisoformat(status["updated_at"]).timestamp() * 10**9)
        if "updated_at" in status
        else ended
    )
    log = {
        "resourceLogs": [
            {
                "resource": resource,
                "scopeLogs": [
                    {
                        "scope": {"name": "aelev.spire"},
                        "logRecords": [
                            {
                                "timeUnixNano": str(occurred),
                                "observedTimeUnixNano": str(ended),
                                "severityNumber": 9 if status["ready"] else 17,
                                "severityText": "INFO" if status["ready"] else "ERROR",
                                "body": {"stringValue": code},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    trace = {
        "resourceSpans": [
            {
                "resource": resource,
                "scopeSpans": [
                    {
                        "scope": {"name": "aelev.spire"},
                        "spans": [
                            {
                                "traceId": uuid.uuid4().hex,
                                "spanId": uuid.uuid4().hex[:16],
                                "name": "spire.observe",
                                "kind": 1,
                                "startTimeUnixNano": str(started),
                                "endTimeUnixNano": str(ended),
                                "status": {"code": 1 if status["ready"] else 2},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    return {
        "logs": json.dumps(log, separators=(",", ":")).encode(),
        "traces": json.dumps(trace, separators=(",", ":")).encode(),
    }


class Outgoing:
    """Only this adapter's pending OTLP samples; local ACK precedes removal."""

    def __init__(
        self,
        root: Path,
        endpoint: str,
        secrets: Path,
        phase: Callable[[str], None] = lambda _phase: None,
        completed: Callable[[], None] = lambda: None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("SPIRE_TELEMETRY_HTTPS_ORIGIN_REQUIRED")
        self.root, self.endpoint, self.secrets = root, parsed, secrets
        self.phase = phase
        self.completed = completed
        self.host = parsed.hostname
        reserve(root, {"owner": OWNER, "role": "observation"})

    def capture(self, value: dict[str, Any], started: int, ended: int) -> None:
        self.phase("SCAN_PENDING")
        if len(pending_files(self.root)) >= CAPACITY:
            raise ValueError("SPIRE_TELEMETRY_CAPACITY_EXHAUSTED")
        identifier = str(uuid.uuid4())
        self.phase("CAPTURE")
        atomic(
            self.root / (str(ended).zfill(20) + "-" + identifier + ".json"),
            {
                "owner": OWNER,
                "id": identifier,
                "packets": {
                    kind: raw.decode()
                    for kind, raw in packet(value, started, ended).items()
                },
            },
        )
        self.completed()

    def flush_one(self) -> None:
        self.phase("SCAN_PENDING")
        candidates = pending_files(self.root)
        self.completed()
        if not candidates:
            return
        path = candidates[0]
        self.phase("READ_PACKET")
        value = read(path)
        if value.get("owner") != OWNER or str(uuid.UUID(value["id"])) != value["id"]:
            raise ValueError("SPIRE_TELEMETRY_PENDING_INVALID")
        self.completed()
        for kind in ("logs", "traces"):
            if kind not in value["packets"]:
                continue
            # Fresh trust and client material per request: atomic directory
            # replacement is visible without restarting this long-lived process.
            self.phase("TLS_CONTEXT")
            context = ssl.create_default_context(
                cafile=str(self.secrets / "sender-ca.crt")
            )
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(
                self.secrets / "observer.crt", self.secrets / "observer.key"
            )
            self.completed()
            client = http.client.HTTPSConnection(
                self.host,
                self.endpoint.port or 443,
                timeout=2,
                context=context,
            )
            identifier = str(uuid.uuid5(uuid.UUID(value["id"]), kind))
            payload = value["packets"][kind].encode()
            digest = hashlib.sha256(
                json.dumps(
                    [kind, {"Content-Type": "application/json"}],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                + b"\x00"
                + payload
            ).hexdigest()
            try:
                self.phase("HTTP_CONNECT")
                client.request(
                    "POST",
                    "/v1/" + kind,
                    payload,
                    {
                        "Content-Type": "application/json",
                        "X-Aelev-Delivery-Id": identifier,
                    },
                )
                self.completed()
                self.phase("HTTP_REPLY")
                reply = client.getresponse()
                self.completed()
                self.phase("HTTP_BODY")
                body = reply.read(65537)
                self.completed()
                if reply.status != 200:
                    raise ValueError("SPIRE_TELEMETRY_HTTP_" + str(reply.status))
                self.phase("ACK_VALIDATE")
                proof = json.loads(body) if len(body) <= 65536 else None
                if not isinstance(proof, dict):
                    raise ValueError("SPIRE_TELEMETRY_ACK_REQUIRED")
                proof = cast(dict[str, Any], proof)
                if (
                    reply.status != 200
                    or proof != {"id": identifier, "sha256": digest, "durable": True}
                    or proof.get("durable") is not True
                ):
                    raise ValueError("SPIRE_TELEMETRY_ACK_REQUIRED")
            finally:
                client.close()
            del value["packets"][kind]
            self.phase("JOURNAL_ACK")
            atomic(path, value)  # Crash after ACK may repeat only identical bytes/ID.
            self.completed()
        self.phase("JOURNAL_DELETE")
        path.unlink()
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.completed()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        address: tuple[str, int],
        source: Path,
        secrets: Path,
        queue: Path | None = None,
        renewal: Path = RENEWAL,
    ) -> None:
        self.source = source
        self.secrets = secrets
        self.queue = queue
        self.renewal = renewal
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)
        self.timeout = 0.5

    def get_request(self) -> tuple[ssl.SSLSocket, Any]:
        sock, address = super().get_request()
        sock.settimeout(2)
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(cafile=str(self.secrets / "query-ca.crt"))
            context.load_cert_chain(
                self.secrets / "metrics.crt", self.secrets / "metrics.key"
            )
            return (
                context.wrap_socket(
                    sock, server_side=True, do_handshake_on_connect=False
                ),
                address,
            )
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
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        print('{"code":"SPIRE_OBSERVATION_REQUEST_FAILED"}', flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = cast(Server, self.server)
        code, content_type = 200, "application/json"
        if self.path == "/healthz":
            body = b'{"status":"ok"}'
        elif self.path == "/readyz":
            try:
                ready = snapshot(server.source)["ready"]
                renewal_snapshot(server.renewal)
                if (
                    server.queue is not None
                    and len(pending_files(server.queue)) >= CAPACITY
                ):
                    ready = False
            except (OSError, ValueError, KeyError, TypeError):
                ready = False
            code = 200 if ready else 503
            body = b'{"status":"ready"}' if ready else b'{"code":"SPIRE_NOT_READY"}'
        elif self.path == "/metrics":
            body, content_type = (
                metrics(server.source, server.queue, server.renewal),
                "text/plain; version=0.0.4",
            )
        else:
            code, body = 404, b"{}"
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def worker_failure(worker: threading.Thread, completed_at: float) -> str | None:
    """Only this worker's completed work proves progress, never phase logging."""
    if not worker.is_alive():
        return "SPIRE_OBSERVATION_WORKER_STOPPED"
    if time.monotonic() - completed_at >= 15:
        return "SPIRE_OBSERVATION_WORKER_STALLED"
    return None


def supervise_worker(
    worker: threading.Thread,
    progress: list[float],
    diagnostics: Diagnostics,
    cycle_started: float,
    error: BaseException | None,
) -> bool:
    """A recovered worker must not exit on a pre-diagnostic observation."""
    reason = worker_failure(worker, progress[0])
    if reason is None:
        return True
    diagnostics.record(
        reason,
        worker,
        progress_age=time.monotonic() - progress[0],
        supervisor_cycle=time.monotonic() - cycle_started,
        error=error,
    )
    confirmed = worker_failure(worker, progress[0])
    if confirmed == reason:
        raise ValueError(reason)
    return confirmed is None  # A different failure needs its own observation.


def serve(root: Path, source: Path, secrets: Path, endpoint: str) -> None:
    diagnostics = Diagnostics(root)
    progress = [time.monotonic()]

    def completed() -> None:
        progress[0] = time.monotonic()

    outgoing = Outgoing(root, endpoint, secrets, diagnostics.mark, completed)
    stopping = threading.Event()

    def stop(_number: int, _frame: FrameType | None) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    failure: list[BaseException] = []

    def collect() -> None:
        next_sample = 0.0
        try:
            while not stopping.wait(1):
                progress[0] = time.monotonic()
                started = time.time_ns()
                if time.monotonic() >= next_sample:
                    diagnostics.mark("SNAPSHOT")
                    next_sample = time.monotonic() + 15
                    try:
                        value = snapshot(source)
                    except (OSError, ValueError, KeyError, TypeError):
                        value = {
                            "ready": False,
                            "reason": "SPIRE_OBSERVATION_STALE_OR_INVALID",
                        }
                    completed()
                    if len(pending_files(root)) < CAPACITY:
                        outgoing.capture(value, started, time.time_ns())
                try:
                    outgoing.flush_one()
                except (OSError, ValueError, http.client.HTTPException) as error:
                    # Existing packet remains. No external address/error text.
                    reason = str(error)
                    code = (
                        reason
                        if re.fullmatch(r"SPIRE_[A-Z0-9_]{1,120}", reason)
                        else (
                            "SPIRE_TELEMETRY_TLS_FAILED"
                            if isinstance(error, ssl.SSLError)
                            else (
                                "SPIRE_TELEMETRY_TIMEOUT"
                                if isinstance(error, TimeoutError)
                                else "SPIRE_TELEMETRY_UNAVAILABLE"
                            )
                        )
                    )
                    print(json.dumps({"code": code}), flush=True)
                progress[0] = time.monotonic()
                diagnostics.mark("WAIT")
        except BaseException as error:
            failure.append(error)
            stopping.set()

    with writer(root), Server(("0.0.0.0", 8083), source, secrets, root) as server:
        worker = threading.Thread(target=collect, daemon=True)
        worker.start()
        while not stopping.is_set():
            cycle_started = time.monotonic()
            server.handle_request()
            if not supervise_worker(
                worker,
                progress,
                diagnostics,
                cycle_started,
                failure[0] if failure else None,
            ):
                continue
            atomic(
                Path("/run/spire/heartbeat.json"),
                {
                    "owner": OWNER,
                    "pid": os.getpid(),
                    "ready": True,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
        worker.join(timeout=6)
        if worker.is_alive() or failure:
            diagnostics.record(
                "SPIRE_OBSERVATION_WORKER_STOPPED",
                worker,
                progress_age=time.monotonic() - progress[0],
                supervisor_cycle=0,
                error=failure[0] if failure else None,
            )
            raise ValueError("SPIRE_OBSERVATION_WORKER_STOPPED")


def publish(action: str, source: Path) -> None:
    from aelev_platform_ui_client import Settings, aggregate, provision, run
    from aelev_platform_ui_client import observe as measure

    cfg = Settings.environment(PREFIX)
    if action == "provision":
        with writer(cfg.secret_root):
            provision(cfg, OWNER, Path("/run/secrets/status-admin"))
        return
    description = json.dumps(
        {
            "schema_version": 1,
            "module_id": cfg.module_id,
            "revision": 2,
            "title": {
                "ru": "Удостоверения рабочих нагрузок",
                "en": "Workload identities",
            },
            "service_kind": "INFRASTRUCTURE",
            "sections": [],
            "components": [
                {"id": "server", "title": {"ru": "SPIRE Server", "en": "SPIRE Server"}},
                {
                    "id": "publisher",
                    "title": {"ru": "Издатель состояния", "en": "Status publisher"},
                },
                {
                    "id": "observer",
                    "title": {"ru": "Наблюдение SPIRE", "en": "SPIRE observation"},
                },
                {
                    "id": "telemetry",
                    "title": {"ru": "Доставка телеметрии", "en": "Telemetry delivery"},
                },
                {
                    "id": "certificate-renewal",
                    "title": {
                        "ru": "Продление сертификатов",
                        "en": "Certificate renewal",
                    },
                },
            ],
        },
        separators=(",", ":"),
    ).encode()

    def observe() -> tuple[str, list[dict[str, str]]]:
        try:
            value = snapshot(source)
            state = "ONLINE" if value["ready"] else "OFFLINE"
            observed_at = value["updated_at"]
        except (OSError, ValueError, KeyError, TypeError):
            state, observed_at = "UNKNOWN", datetime.now(UTC).isoformat()
        components = [{"id": "server", "state": state, "observed_at": observed_at}]

        def check(host: str, port: int) -> str:
            context = ssl.create_default_context(
                cafile=str(cfg.secret_root / "query-ca.crt")
            )
            context.load_cert_chain(
                cfg.secret_root / "query.crt", cfg.secret_root / "query.key"
            )
            client = http.client.HTTPSConnection(
                host, port, timeout=cfg.timeout, context=context
            )
            try:
                client.request("GET", "/readyz")
                reply = client.getresponse()
                raw = reply.read(65537)
                if reply.status == 200 and raw == b'{"status":"ready"}':
                    return "ONLINE"
                return "OFFLINE" if reply.status == 503 else "UNKNOWN"
            except (OSError, ValueError, http.client.HTTPException):
                return "UNKNOWN"
            finally:
                client.close()

        components += [
            measure("observer", lambda: check("spire-observer", 8083)),
            measure("telemetry", lambda: check("spire-telemetry-sender", 13134)),
            measure(
                "certificate-renewal",
                lambda: "ONLINE" if renewal_snapshot()["ready"] else "OFFLINE",
            ),
        ]
        return aggregate(components), components

    with writer(cfg.state_path.parent):
        # The facade's publish/source arguments are not the public SDK's CLI.
        previous = sys.argv
        try:
            sys.argv = [previous[0], "run"]
            run(cfg, OWNER, description, observe)
        finally:
            sys.argv = previous


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "publish", "provision"))
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--root", type=Path, default=Path("/var/lib/observation"))
    parser.add_argument(
        "--secrets", type=Path, default=Path("/run/secrets/observation")
    )
    args = parser.parse_args()
    if args.action == "serve":
        instance = os.environ.get(PREFIX + "UI_MODULE_ID", "")
        if (
            not instance
            or str(uuid.UUID(instance)) != instance
            or uuid.UUID(instance).int == 0
        ):
            raise ValueError("SPIRE_OBSERVATION_MODULE_ID_REQUIRED")
        serve(
            args.root, args.source, args.secrets, os.environ[PREFIX + "OTLP_ENDPOINT"]
        )
    else:
        publish(args.action, args.source)


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        http.client.HTTPException,
    ) as error:
        code = str(error)
        print(
            json.dumps(
                {
                    "code": (
                        code
                        if re.fullmatch(r"SPIRE_[A-Z0-9_]{1,120}", code)
                        else "SPIRE_OBSERVATION_FAILED"
                    )
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None
