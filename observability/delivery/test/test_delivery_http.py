"""Real TLS fault injection for the delivery boundary, not replacement backends."""

from __future__ import annotations

import http.client
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest

from delivery import (
    Delivery,
    DeliveryServer,
    Journal,
    Settings,
    critical_failure,
    failure_context,
    record_failure,
    ReloadingTLS,
)
import prepare
from aelev_observability_tools import certificate, generate_tls, read_env


def renewed(tls: Path, certificate: Path, destination: Path) -> None:
    """Reissue an isolated fixture with the same key, signer and SAN."""
    request = destination.with_suffix(".csr")
    subprocess.run(
        [
            "openssl",
            "x509",
            "-x509toreq",
            "-in",
            str(certificate),
            "-signkey",
            str(certificate.with_suffix(".key")),
            "-copy_extensions",
            "copyall",
            "-out",
            str(request),
        ],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(request),
            "-copy_extensions",
            "copyall",
            "-CA",
            str(tls.parent / "issuer/internal-ca.crt"),
            "-CAkey",
            str(tls.parent / "issuer/internal-ca.key"),
            "-set_serial",
            str(uuid4().int),
            "-days",
            "60",
            "-sha256",
            "-out",
            str(destination),
        ],
        capture_output=True,
        check=True,
    )


def test_outbound_adopts_new_leaf_without_restarting_sender(
    delivery_setup: tuple[Settings, FaultServer],
    tmp_path: Path,
) -> None:
    settings, backend = delivery_setup
    leaf, key = tmp_path / "rotating.crt", tmp_path / "rotating.key"
    shutil.copyfile(settings.client_cert, leaf)
    shutil.copyfile(settings.client_key, key)
    agent = Delivery(replace(settings, client_cert=str(leaf), client_key=str(key)))
    try:
        agent.journal.accept("logs", {}, b"first", str(uuid4()))
        first = agent.journal.first("logs")
        assert first is not None
        assert agent.forward(first)
        old = backend.last_peer_sha256
        fresh = tmp_path / "candidate.crt"
        renewed(Path(settings.client_cert).parent, leaf, fresh)
        os.replace(fresh, leaf)
        agent.journal.accept("logs", {}, b"second", str(uuid4()))
        second = agent.journal.first("logs")
        assert second is not None
        assert agent.forward(second)
        assert backend.last_peer_sha256 != old
    finally:
        agent.lease.close()


def test_listener_adopts_new_leaf_without_restarting_server(
    delivery_setup: tuple[Settings, FaultServer],
    tmp_path: Path,
) -> None:
    settings, _ = delivery_setup
    leaf, key = tmp_path / "rotating.crt", tmp_path / "rotating.key"
    shutil.copyfile(settings.server_cert, leaf)
    shutil.copyfile(settings.server_key, key)
    agent = Delivery(replace(settings, server_cert=str(leaf), server_key=str(key)))
    server = DeliveryServer(agent, health_only=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def peer() -> str:
        context = ssl.create_default_context(cafile=settings.health_ca)
        context.load_cert_chain(settings.health_cert, settings.health_key)
        connection = http.client.HTTPSConnection(
            "localhost", server.server_port, context=context, timeout=2
        )
        try:
            connection.connect()
            assert isinstance(connection.sock, ssl.SSLSocket)
            digest = hashlib.sha256(
                connection.sock.getpeercert(binary_form=True) or b""
            ).hexdigest()
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            assert response.status == 200
            response.read()
            return digest
        finally:
            connection.close()

    try:
        old = peer()
        fresh = tmp_path / "candidate.crt"
        renewed(Path(settings.server_cert).parent, leaf, fresh)
        os.replace(fresh, leaf)
        assert peer() != old
        assert thread.is_alive()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        agent.lease.close()


@pytest.mark.parametrize("field", ["key", "ca", "certificate"])
def test_tls_reload_never_falls_back_after_invalid_material(
    tls: Path,
    tmp_path: Path,
    field: str,
) -> None:
    paths = {name: tmp_path / name for name in ("certificate", "key", "ca")}
    for name, source in zip(paths, ("server.crt", "server.key", "ca.crt"), strict=True):
        shutil.copyfile(tls / source, paths[name])

    def context() -> ssl.SSLContext:
        result = ssl.create_default_context(cafile=str(paths["ca"]))
        result.load_cert_chain(paths["certificate"], paths["key"])
        return result

    reloader = ReloadingTLS(
        str(paths["certificate"]), str(paths["key"]), str(paths["ca"]), context
    )
    reloader.get()
    paths[field].write_bytes(b"isolated-invalid-material")
    with pytest.raises(ssl.SSLError):
        reloader.get()
    with pytest.raises(ssl.SSLError):
        reloader.get()


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class FaultServer(ThreadingHTTPServer):
    response_status = 200
    response_body = b"{}"
    last_peer_sha256 = ""


class FaultHandler(BaseHTTPRequestHandler):
    server: FaultServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        assert isinstance(self.connection, ssl.SSLSocket)
        self.server.last_peer_sha256 = hashlib.sha256(
            self.connection.getpeercert(binary_form=True) or b""
        ).hexdigest()
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(self.server.response_status)
        self.send_header("Content-Length", str(len(self.server.response_body)))
        self.end_headers()
        self.wfile.write(self.server.response_body)


@pytest.fixture(scope="module")
def tls(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("delivery-tls")
    spec = json.loads((prepare.ROOT / "deployment.json").read_text())
    env = read_env(prepare.ROOT / ".env.example", spec["prefix"])
    env.update(
        SECRET_ROOT="secrets", RUNTIME_ROOT="runtime", SECRET_GID=str(os.getgid())
    )
    issuer = root / "issuer"
    generate_tls(root, env, spec, issuer)
    secrets = root / "secrets"
    certificate(secrets, "publisher", issuer / "publisher-ca", "clientAuth", [])
    return secrets


@pytest.fixture
def delivery_setup(tls: Path, tmp_path: Path) -> Iterator[tuple[Settings, FaultServer]]:
    backend = FaultServer(("127.0.0.1", 0), FaultHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tls / "server.crt", tls / "server.key")
    context.load_verify_locations(tls / "ca.crt")
    context.verify_mode = ssl.CERT_REQUIRED
    backend.socket = context.wrap_socket(backend.socket, server_side=True)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    root = tls
    settings = Settings(
        mode="archive",
        listen_host="127.0.0.1",
        listen_port=free_port(),
        health_port=free_port(),
        journal=tmp_path / "journal",
        capacity_bytes=100_000,
        request_bytes=4096,
        retry_seconds=0.1,
        timeout_seconds=1,
        server_cert=str(root / "server.crt"),
        server_key=str(root / "server.key"),
        client_ca=str(root / "publisher-ca.crt"),
        ca=str(root / "ca.crt"),
        client_cert=str(root / "client.crt"),
        client_key=str(root / "client.key"),
        health_ca=str(root / "ca.crt"),
        health_cert=str(root / "client.crt"),
        health_key=str(root / "client.key"),
        destinations={
            kind: f"https://localhost:{backend.server_port}/sink"
            for kind in ("logs", "traces", "metrics", "otlp_metrics")
        },
        prometheus_command=[],
    )
    try:
        yield settings, backend
    finally:
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "status,body",
    [
        (400, b"invalid"),
        (403, b"forbidden"),
        (429, b"limited"),
        (500, b"failed"),
        (200, b'{"partialSuccess":{"rejectedLogRecords":"1"}}'),
    ],
)
def test_all_errors_retain_exact_batch_until_full_ack(
    delivery_setup: tuple[Settings, FaultServer],
    status: int,
    body: bytes,
) -> None:
    settings, backend = delivery_setup
    agent = Delivery(settings)
    try:
        agent.journal.accept(
            "logs",
            {"Content-Type": "application/json"},
            b'{"resourceLogs":[]}',
            str(uuid4()),
        )
        record = agent.journal.first("logs")
        assert record is not None
        backend.response_status, backend.response_body = status, body
        assert not agent.forward(record)
        assert agent.journal.first("logs") == record
        backend.response_status, backend.response_body = 200, b"{}"
        assert agent.forward(record)
        assert agent.journal.first("logs") is None
        assert agent.journal.status()["records"] == 1  # immutable central copy remains
    finally:
        agent.lease.close()


def test_health_reports_bounded_safe_failure_codes(
    delivery_setup: tuple[Settings, FaultServer],
    tls: Path,
) -> None:
    settings, backend = delivery_setup
    agent = Delivery(settings)
    agent.journal.accept("logs", {}, b"private payload", str(uuid4()))
    record = agent.journal.first("logs")
    assert record is not None
    backend.response_status = 403
    assert not agent.forward(record)
    server = DeliveryServer(agent, health_only=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = ssl.create_default_context(cafile=str(tls / "ca.crt"))
    context.load_cert_chain(tls / "client.crt", tls / "client.key")
    connection = http.client.HTTPSConnection(
        "localhost", settings.health_port, context=context, timeout=2
    )
    try:
        for path, status in (("/health", 503), ("/status", 200)):
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == status
            body = response.read()
            result = json.loads(body)
            assert result["blocking_errors"] == {"logs": "http_403"}
            assert result["failed"] == result["pending"] == 1
            assert b"private payload" not in body
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        agent.lease.close()


def test_liveness_never_reads_journal_or_probes_destinations(
    delivery_setup: tuple[Settings, FaultServer],
    tls: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _ = delivery_setup
    agent = Delivery(settings)
    status = Mock(side_effect=AssertionError("liveness must not read journal"))
    monkeypatch.setattr(agent.journal, "status", status)
    agent.stop.set()  # The HTTP process is still alive while shutting down.
    server = DeliveryServer(agent, health_only=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = ssl.create_default_context(cafile=str(tls / "ca.crt"))
    context.load_cert_chain(tls / "client.crt", tls / "client.key")
    connection = http.client.HTTPSConnection(
        "localhost", settings.health_port, context=context, timeout=2
    )
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        assert response.status == 200 and response.read() == b'{"status":"ok"}'
        status.assert_not_called()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        agent.lease.close()


@pytest.mark.parametrize("alive", [False, True])
def test_canonical_readiness_requires_real_worker_presence(
    delivery_setup: tuple[Settings, FaultServer],
    tls: Path,
    alive: bool,
) -> None:
    settings, _ = delivery_setup
    agent = Delivery(settings)
    agent.worker = Mock(is_alive=Mock(return_value=alive))
    server = DeliveryServer(agent, health_only=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = ssl.create_default_context(cafile=str(tls / "ca.crt"))
    context.load_cert_chain(tls / "client.crt", tls / "client.key")
    connection = http.client.HTTPSConnection(
        "localhost", settings.health_port, context=context, timeout=2
    )
    try:
        connection.request("GET", "/readyz")
        response = connection.getresponse()
        assert response.status == (200 if alive else 503)
        assert json.loads(response.read()) == (
            {"status": "ready"} if alive else {"error": "delivery_not_ready"}
        )
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        agent.lease.close()


@pytest.mark.parametrize(
    "fault",
    [None, "missing", "stale", "hash", "foreign", "duplicate", "expiry", "future"],
)
def test_own_renewal_receipt_controls_real_tls_readiness(
    delivery_setup: tuple[Settings, FaultServer],
    tls: Path,
    tmp_path: Path,
    fault: str | None,
) -> None:
    settings, _ = delivery_setup
    certificate = tmp_path / "client.crt"
    renewed(tls, tls / "client.crt", certificate)
    expires = ssl.cert_time_to_seconds(
        getattr(ssl, "_ssl")._test_decode_cert(str(certificate))["notAfter"]
    )
    report = tmp_path / "own-renewal.json"
    now = time.time()
    value: dict[str, Any] = {
        "schema_version": 1,
        "owner": "telemetry-certificate-renewal",
        "consumer_owner": "fixture-owner",
        "ready": True,
        "renewal_healthy": True,
        "checked_at": now,
        "last_success_at": now,
        "certificates": [
            {
                "id": "own-client",
                "healthy": True,
                "certificate_sha256": hashlib.sha256(
                    certificate.read_bytes()
                ).hexdigest(),
                "certificate_expires_at": expires,
                "ca_expires_at": expires + 86400,
            }
        ],
    }
    if fault == "stale":
        value["checked_at"] -= 181
    elif fault == "hash":
        value["certificates"][0]["certificate_sha256"] = "0" * 64
    elif fault == "foreign":
        value["consumer_owner"] = "foreign-owner"
    elif fault == "duplicate":
        value["certificates"].append(value["certificates"][0])
    elif fault == "expiry":
        value["certificates"][0]["certificate_expires_at"] = now + 86400
    elif fault == "future":
        value["checked_at"] += 3600
    if fault != "missing":
        report.write_text(json.dumps(value))
    settings = replace(
        settings,
        client_cert=str(certificate),
        certificate_renewal={
            "consumer_owner": "fixture-owner",
            "report": str(report),
            "certificates": {"own-client": str(certificate)},
        },
    )
    agent = Delivery(settings)
    agent.worker = Mock(is_alive=Mock(return_value=True))
    server = DeliveryServer(agent, health_only=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = ssl.create_default_context(cafile=str(tls / "ca.crt"))
    context.load_cert_chain(tls / "client.crt", tls / "client.key")
    try:
        for path in ("/readyz", "/healthz", "/metrics"):
            connection = http.client.HTTPSConnection(
                "localhost", settings.health_port, context=context, timeout=2
            )
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                body = response.read()
                assert response.status == (
                    503 if path == "/readyz" and fault is not None else 200
                )
                if path == "/metrics":
                    assert (
                        "aelev_telemetry_delivery_certificate_renewal_ready "
                        + str(int(fault is None))
                    ).encode() in body
            finally:
                connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        agent.lease.close()


@pytest.mark.parametrize(
    "failure", ["dead", "stalled", "ingestion_dead", "health_dead", "handled_error"]
)
def test_dead_or_stalled_worker_terminates_entire_cell(
    delivery_setup: tuple[Settings, FaultServer],
    tmp_path: Path,
    failure: str,
) -> None:
    settings, _ = delivery_setup
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({**asdict(settings), "journal": str(settings.journal)})
    )
    code = """
import sys, time
from pathlib import Path
from delivery import Delivery, DeliveryServer, Settings, run
def broken(self):
    if sys.argv[2] == 'handled_error':
        self.stop.set()
    if sys.argv[2] == 'stalled':
        self.last_progress -= 100
        time.sleep(30)
if sys.argv[2] in ('stalled', 'dead', 'handled_error'):
    Delivery.work = broken
else:
    original = DeliveryServer.serve_forever
    def listener(self, *args, **kwargs):
        if self.health_only == (sys.argv[2] == 'health_dead'):
            return
        original(self, *args, **kwargs)
    DeliveryServer.serve_forever = listener
run(Settings.read(Path(sys.argv[1])))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(config), failure],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert (
        b"delivery worker failed without an operator stop"
        if failure == "handled_error"
        else b"delivery critical loop stopped or stalled"
    ) in result.stderr
    failure_codes = {
        "dead": "WORKER_STOPPED",
        "stalled": "WORKER_STALLED",
        "ingestion_dead": "INGESTION_LISTENER_STOPPED",
        "health_dead": "HEALTH_LISTENER_STOPPED",
        "handled_error": "WORKER_FAILED",
    }
    report = json.loads((settings.journal / "last-failure.json").read_text())
    assert report["code"] == failure_codes[failure]
    if failure != "handled_error":
        assert report["supervisor_gap_seconds"] >= 0
        assert set(json.loads(report["thread_stacks_json"])) == {
            "worker",
            "ingestion",
            "health",
        }
        assert "ingestion_phase" in report and "health_phase" in report
    history = (settings.journal / "failure-history.jsonl").read_text().splitlines()
    assert len(history) == 1 and json.loads(history[0]) == report
    assert (settings.journal / "last-failure.json").stat().st_mode & 0o777 == 0o600


def test_accept_readiness_race_cannot_block_listener(
    delivery_setup: tuple[Settings, FaultServer],
) -> None:
    settings, _ = delivery_setup
    agent = Delivery(settings)
    server = DeliveryServer(agent)
    try:
        assert server.socket.gettimeout() == 0
        with pytest.raises(BlockingIOError):
            server.get_request()
        with socket.create_connection((settings.listen_host, settings.listen_port)):
            accepted, _address = server.get_request()
            try:
                assert accepted.gettimeout() == settings.timeout_seconds
            finally:
                accepted.close()
    finally:
        server.server_close()
        agent.lease.close()


def test_blocked_dispatch_is_reported_without_locals_or_other_threads(
    delivery_setup: tuple[Settings, FaultServer], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _ = delivery_setup
    agent = Delivery(settings)
    server, health = DeliveryServer(agent), DeliveryServer(agent, health_only=True)
    listener = threading.Thread(target=server.serve_forever, daemon=True)
    entered, release = threading.Event(), threading.Event()
    original = threading.Thread.start
    credential_fixture = "private-local-must-not-appear"

    def blocked_start(thread: threading.Thread) -> None:
        if threading.current_thread() is listener:
            entered.set()
            assert release.wait(3)
        original(thread)

    listener.start()
    connection: socket.socket | None = None
    try:
        monkeypatch.setattr(threading.Thread, "start", blocked_start)
        connection = socket.create_connection(
            (settings.listen_host, settings.listen_port)
        )
        assert entered.wait(2)
        assert server.listener_phase[0] == "REQUEST_DISPATCH"
        context = failure_context(
            agent, listener, threading.current_thread(), server, health, 12.5
        )
        assert context["ingestion_phase"] == "REQUEST_DISPATCH"
        assert context["supervisor_gap_seconds"] == 12.5
        text = json.dumps(context)
        assert credential_fixture not in text and str(settings.journal) not in text
        stacks = json.loads(str(context["thread_stacks_json"]))
        assert set(stacks) == {"worker", "ingestion", "health"}
        assert stacks["worker"] == []
        assert 0 < len(stacks["ingestion"]) <= 8
        assert any(
            row["file"] == "delivery.py" and row["function"] == "process_request"
            for row in stacks["ingestion"]
        )
        assert all(
            set(row) == {"file", "function", "line"} for row in stacks["ingestion"]
        )
        record_failure(settings, {"code": "INGESTION_LISTENER_STALLED", **context})
        persisted = json.loads((settings.journal / "last-failure.json").read_text())
        assert persisted["thread_stacks_json"] == context["thread_stacks_json"]
    finally:
        release.set()
        if connection is not None:
            connection.close()
        server.shutdown()
        server.server_close()
        health.server_close()
        listener.join(timeout=2)
        agent.lease.close()


def test_idle_tls_peer_does_not_stall_accept_loop(
    delivery_setup: tuple[Settings, FaultServer],
) -> None:
    settings, _ = delivery_setup
    agent = Delivery(settings)
    server = DeliveryServer(agent)
    listener = threading.Thread(target=server.serve_forever, daemon=True)
    listener.start()
    try:
        with socket.create_connection((settings.listen_host, settings.listen_port)):
            started = server.last_progress
            deadline = time.monotonic() + 2
            while server.last_progress <= started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.last_progress > started
            assert server.listener_phase[0] == "POLL_WAIT"
    finally:
        server.shutdown()
        server.server_close()
        listener.join(timeout=2)
        agent.lease.close()


def test_failure_history_survives_restart_without_touching_packets(
    delivery_setup: tuple[Settings, FaultServer],
) -> None:
    settings, _ = delivery_setup
    agent = Delivery(settings)
    identifier = str(uuid4())
    agent.journal.accept("logs", {}, b"immutable diagnostic fixture", identifier)
    before = agent.journal.first("logs")
    record_failure(settings, {"code": "HEALTH_LISTENER_STALLED"})
    agent.lease.close()
    again = Delivery(settings)
    try:
        assert again.journal.first("logs") == before
        report = json.loads((settings.journal / "last-failure.json").read_text())
        assert report["code"] == "HEALTH_LISTENER_STALLED"
        record_failure(settings, {"code": "WORKER_STALLED", "phase": "FORWARD_LOGS"})
        history = (settings.journal / "failure-history.jsonl").read_text().splitlines()
        assert len(history) == 2
        assert json.loads(history[0]) == report
    finally:
        again.lease.close()


def test_supervisor_distinguishes_stalled_listener_and_worker_phase(
    delivery_setup: tuple[Settings, FaultServer],
) -> None:
    settings, _ = delivery_setup
    agent = Delivery(settings)
    alive = Mock(spec=threading.Thread)
    alive.is_alive.return_value = True
    agent.worker = alive
    ingestion, health = Mock(spec=DeliveryServer), Mock(spec=DeliveryServer)
    ingestion.last_progress = health.last_progress = time.monotonic()
    try:
        assert critical_failure(agent, alive, alive, ingestion, health) is None
        health.last_progress -= 10
        report = critical_failure(agent, alive, alive, ingestion, health)
        assert report is not None and report["code"] == "HEALTH_LISTENER_STALLED"
        agent.phase = "FORWARD_TRACES"
        agent.last_progress -= 100
        report = critical_failure(agent, alive, alive, ingestion, health)
        assert report is not None and report["code"] == "WORKER_STALLED"
        assert report["phase"] == "FORWARD_TRACES"
    finally:
        agent.lease.close()


def test_sender_never_trusts_ordinary_200_or_wrong_receipt(
    delivery_setup: tuple[Settings, FaultServer],
) -> None:
    settings, backend = delivery_setup
    agent = Delivery(replace(settings, mode="sender"))
    try:
        agent.journal.accept("logs", {}, b"exact", str(uuid4()))
        record = agent.journal.first("logs")
        assert record is not None
        assert not agent.forward(record)
        backend.response_body = json.dumps(
            {"id": record.identifier, "sha256": "wrong", "durable": True}
        ).encode()
        assert not agent.forward(record)
        backend.response_body = json.dumps(
            {"id": record.identifier, "sha256": record.digest, "durable": True}
        ).encode()
        assert agent.forward(record)
        assert agent.journal.status()["records"] == 0
    finally:
        agent.lease.close()


def test_public_ack_is_after_commit_and_query_cannot_publish(
    delivery_setup: tuple[Settings, FaultServer],
    tls: Path,
) -> None:
    settings, _ = delivery_setup
    agent = Delivery(settings)
    server = DeliveryServer(agent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        publisher = ssl.create_default_context(cafile=str(tls / "ca.crt"))
        publisher.load_cert_chain(tls / "publisher.crt", tls / "publisher.key")
        connection = http.client.HTTPSConnection(
            "localhost", settings.listen_port, context=publisher, timeout=2
        )
        identifier = str(uuid4())
        connection.request(
            "POST",
            "/v1/logs",
            b"{}",
            {"Content-Type": "application/json", "X-Aelev-Delivery-Id": identifier},
        )
        response = connection.getresponse()
        assert response.status == 200
        receipt = json.loads(response.read())
        stored = Journal(settings.journal, settings.capacity_bytes).first("logs")
        assert stored is not None
        assert receipt == {"id": identifier, "sha256": stored.digest, "durable": True}
        connection.close()
        operator = ssl.create_default_context(cafile=str(tls / "ca.crt"))
        operator.load_cert_chain(tls / "client.crt", tls / "client.key")
        forbidden = http.client.HTTPSConnection(
            "localhost", settings.listen_port, context=operator, timeout=2
        )
        with pytest.raises((OSError, http.client.HTTPException)):
            forbidden.request(
                "POST", "/v1/logs", b"{}", {"Content-Type": "application/json"}
            )
            forbidden.getresponse()
        forbidden.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        agent.lease.close()


def test_committed_batch_survives_abrupt_process_exit(tmp_path: Path) -> None:
    code = (
        "import os,sys; from pathlib import Path; from uuid import uuid4; "
        "from delivery import Journal; "
        "Journal(Path(sys.argv[1]),100000).accept('logs',{},b'exact',str(uuid4())); "
        "os._exit(9)"
    )
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=False)
    assert result.returncode == 9
    journal = Journal(tmp_path, 100_000)
    record = journal.first("logs")
    assert record is not None and record.body == b"exact"


def test_disk_failure_never_returns_durable_ack(
    delivery_setup: tuple[Settings, FaultServer],
    tls: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _ = delivery_setup
    agent = Delivery(settings)
    agent.journal.accept("logs", {}, b"preexisting", str(uuid4()))

    def disk_full(*args: Any) -> str:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(agent.journal, "accept", disk_full)
    server = DeliveryServer(agent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection: http.client.HTTPSConnection | None = None
    try:
        context = ssl.create_default_context(cafile=str(tls / "ca.crt"))
        context.load_cert_chain(tls / "publisher.crt", tls / "publisher.key")
        connection = http.client.HTTPSConnection(
            "localhost", settings.listen_port, context=context, timeout=2
        )
        connection.request(
            "POST", "/v1/logs", b"{}", {"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        assert response.status == 503
        assert "durable" not in json.loads(response.read())
        assert agent.journal.status()["records"] == 1
        assert agent.pressure.is_set()
    finally:
        if connection is not None:
            connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        agent.lease.close()
