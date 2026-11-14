"""Real network authentication before source text is sent; no model/API calls."""

from __future__ import annotations

import json
import ssl
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from provider_tls_fixtures import CLIENT, SERVER, Authority, delivered

from translator_service import presence, provider_transport, runtime
from translator_service.retry import retry_code

PAYLOAD = {
    "schema_version": "1.0.0",
    "source_language": "en",
    "target_language": "ru",
    "text": "Hello",
}
RESULT = {
    "schema_version": "1.0.0",
    "translated_text": "Привет",
    "warnings": ["MACHINE_TRANSLATION_UNVERIFIED"],
}


@contextmanager
def server(
    authority: Authority,
    *,
    identity: str = SERVER,
    result: object = RESULT,
    status: int = 200,
    body_delay: float = 0,
) -> Generator[tuple[int, list[bytes]]]:
    calls: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def handle(self) -> None:
            with suppress(ConnectionResetError, ssl.SSLError):
                super().handle()

        def do_POST(self) -> None:
            calls.append(self.rfile.read(int(self.headers["Content-Length"])))
            raw = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Location", "http://untrusted.invalid/")
            self.end_headers()
            time.sleep(body_delay)
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(raw)

        def do_GET(self) -> None:
            calls.append(("GET " + self.path).encode())
            raw = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*authority.leaf([identity]))
    context.load_verify_locations(cafile=authority.cert)
    context.verify_mode = ssl.CERT_REQUIRED
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_port, calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Authority, Path]:
    root = tmp_path.resolve()
    authority = Authority(root / "issuer")
    client_root = root / "client"
    delivered(
        client_root, authority, *authority.leaf([CLIENT]), own=CLIENT, role="client"
    )
    monkeypatch.setenv("AELEV_ENVIRONMENT", "test")
    monkeypatch.setenv("AELEV_TRANSLATOR_PROVIDER_TRANSPORT", "mtls")
    monkeypatch.setenv("AELEV_TRANSLATOR_PROVIDER_TLS_ROOT", str(client_root))
    monkeypatch.setattr(runtime.health_heartbeat, "progress", Mock())
    return authority, client_root


def test_real_worker_network_call_preserves_opaque_fields_and_result(
    client: tuple[Authority, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _ = client
    with server(authority) as (port, calls):
        monkeypatch.setenv("AELEV_TRANSLATOR_PROVIDER_URL", f"https://localhost:{port}")
        assert runtime.compute({**PAYLOAD, "request_id": "must-stay-with-owner"}) == {
            "status": "SUCCEEDED",
            "error_code": "",
            **{k: RESULT[k] for k in ("translated_text", "warnings")},
        }
    assert [json.loads(raw) for raw in calls] == [PAYLOAD]


@pytest.mark.parametrize("kind", ["uri", "hostname", "ca"])
def test_wrong_provider_is_rejected_before_any_http_request_or_text(
    client: tuple[Authority, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
) -> None:
    authority, _ = client
    if kind == "ca":
        authority = Authority(tmp_path.resolve() / "foreign")
    with server(authority, identity=CLIENT if kind == "uri" else SERVER) as (
        port,
        calls,
    ):
        host = "127.0.0.1" if kind == "hostname" else "localhost"
        monkeypatch.setenv("AELEV_TRANSLATOR_PROVIDER_URL", f"https://{host}:{port}")
        with pytest.raises(RuntimeError, match="TRANSLATOR_PROVIDER_TLS_UNAVAILABLE"):
            runtime.compute(PAYLOAD)
    assert calls == []


def test_withdrawn_client_identity_cannot_send_text(
    client: tuple[Authority, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, root = client
    marker = json.loads((root / "current.json").read_text())
    marker["state"] = "UNAVAILABLE"
    (root / "current.json").write_text(json.dumps(marker))
    with server(authority) as (port, calls):
        monkeypatch.setenv("AELEV_TRANSLATOR_PROVIDER_URL", f"https://localhost:{port}")
        with pytest.raises(RuntimeError, match="TRANSLATOR_PROVIDER_TLS_UNAVAILABLE"):
            runtime.compute(PAYLOAD)
    assert calls == []


def test_identity_renewal_during_handshake_requires_fresh_connection_before_text(
    client: tuple[Authority, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, root = client
    original = provider_transport.http.client.HTTPSConnection.connect

    def renewing(connection: provider_transport.http.client.HTTPSConnection) -> None:
        original(connection)
        delivered(root, authority, *authority.leaf([CLIENT]), own=CLIENT, role="client")

    monkeypatch.setattr(
        provider_transport.http.client.HTTPSConnection, "connect", renewing
    )
    with server(authority) as (port, calls):
        monkeypatch.setenv("AELEV_TRANSLATOR_PROVIDER_URL", f"https://localhost:{port}")
        with pytest.raises(RuntimeError, match="TRANSLATOR_PROVIDER_TLS_UNAVAILABLE"):
            runtime.compute(PAYLOAD)
    assert calls == []


@pytest.mark.parametrize(
    "result",
    [
        [],
        {**RESULT, "model": "forbidden"},
        {**RESULT, "translated_text": "x" * 1_048_576},
    ],
)
def test_invalid_or_oversized_network_response_is_technical_failure(
    client: tuple[Authority, Path], monkeypatch: pytest.MonkeyPatch, result: object
) -> None:
    authority, _ = client
    with server(authority, result=result) as (port, _calls):
        monkeypatch.setenv("AELEV_TRANSLATOR_PROVIDER_URL", f"https://localhost:{port}")
        with pytest.raises(RuntimeError, match="TRANSLATOR_PRIVATE_RESPONSE_INVALID"):
            runtime.compute(PAYLOAD)


def test_redirect_is_not_followed(
    client: tuple[Authority, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _ = client
    with server(authority, status=307) as (port, calls):
        monkeypatch.setenv("AELEV_TRANSLATOR_PROVIDER_URL", f"https://localhost:{port}")
        with pytest.raises(httpx.HTTPStatusError):
            runtime.compute(PAYLOAD)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost",
        "https://user:password@localhost",
        "https://localhost/path",
        "https://localhost?other",
        "https://localhost#other",
        "https://localhost:0",
        "https://localhost\n",
    ],
)
def test_invalid_origin_never_connects(
    client: tuple[Authority, Path], monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    connection = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(
        provider_transport.http.client.HTTPSConnection, "connect", connection
    )
    monkeypatch.setenv("AELEV_TRANSLATOR_PROVIDER_URL", origin)
    with pytest.raises(RuntimeError, match="TRANSLATOR_PROVIDER_CONFIG_INVALID"):
        runtime.compute(PAYLOAD)
    connection.assert_not_called()


def test_network_configuration_does_not_fall_back_to_uds(
    client: tuple[Authority, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AELEV_TRANSLATOR_PROVIDER_TRANSPORT")
    with pytest.raises(RuntimeError, match="TRANSLATOR_PROVIDER_CONFIG_INVALID"):
        runtime.compute(PAYLOAD)


def test_presence_uses_authenticated_non_inference_network_probe(
    client: tuple[Authority, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, root = client
    with server(authority) as (port, calls):
        monkeypatch.setenv("AELEV_TRANSLATOR_PROVIDER_URL", f"https://localhost:{port}")
        assert presence.observe_provider() == "ONLINE"
        assert calls == [b"GET /healthz"]
        marker = json.loads((root / "current.json").read_text())
        marker["state"] = "UNAVAILABLE"
        (root / "current.json").write_text(json.dumps(marker))
        assert presence.observe_provider() == "UNKNOWN"
        assert calls == [b"GET /healthz"]


def test_total_deadline_interrupts_response_body_and_keeps_retry_classification(
    client: tuple[Authority, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _ = client
    with server(authority, body_delay=2) as (port, calls):
        monkeypatch.setenv("AELEV_TRANSLATOR_PROVIDER_URL", f"https://localhost:{port}")
        started = time.monotonic()
        with pytest.raises(TimeoutError) as error:
            provider_transport.request(PAYLOAD, timeout=0.2)
        assert time.monotonic() - started < 1
        assert retry_code(error.value) == "TRANSLATOR_DEADLINE_EXCEEDED"
        assert len(calls) == 1


@pytest.mark.parametrize(
    "code",
    [
        "TRANSLATOR_PROVIDER_TLS_UNAVAILABLE",
        "TRANSLATOR_PROVIDER_CONFIG_INVALID",
        "TRANSLATOR_PROVIDER_TRANSPORT_ERROR",
    ],
)
def test_network_failure_retains_owner_job_through_existing_retry(code: str) -> None:
    repo = Mock()
    lease = {"request_id": "preserved-id", "lease_token": "preserved-token"}
    envelope = {"correlationid": "preserved-correlation", "traceparent": ""}
    error = RuntimeError(code)
    assert retry_code(error) == code
    runtime.retry_translation(repo, lease, envelope, error)
    repo.retry.assert_called_once_with(lease, code)
