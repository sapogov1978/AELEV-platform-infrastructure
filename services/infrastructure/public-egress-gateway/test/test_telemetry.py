from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.trace import INVALID_SPAN, SpanKind, get_current_span

import telemetry


def test_local_mode_keeps_telemetry_optional() -> None:
    assert (
        telemetry.configure(
            "",
            environment="local",
            ca_file="",
            client_cert_file="",
            client_key_file="",
        )
        is None
    )


def test_production_requires_https_durable_sender(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="TELEMETRY_REQUIRED"):
        telemetry.configure(
            "",
            environment="production",
            ca_file="",
            client_cert_file="",
            client_key_file="",
        )
    with pytest.raises(RuntimeError, match="HTTPS_REQUIRED"):
        telemetry.configure(
            "http://sender:4318",
            environment="production",
            ca_file=str(tmp_path / "ca"),
            client_cert_file=str(tmp_path / "cert"),
            client_key_file=str(tmp_path / "key"),
        )
    with pytest.raises(RuntimeError, match="CREDENTIAL_MISSING"):
        telemetry.configure(
            "https://sender:4318",
            environment="production",
            ca_file=str(tmp_path / "ca"),
            client_cert_file=str(tmp_path / "cert"),
            client_key_file=str(tmp_path / "key"),
        )


def test_request_span_accepts_trace_context(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class StubTracer:
        @contextmanager
        def start_as_current_span(self, name: str, **kwargs: Any) -> Any:
            captured["name"] = name
            captured.update(kwargs)
            yield INVALID_SPAN

    def fake_get_tracer(_name: str) -> StubTracer:
        return StubTracer()

    monkeypatch.setattr(telemetry.trace, "get_tracer", fake_get_tracer)
    with telemetry.request_span(
        {"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"},
        "/v1/fetch",
    ):
        pass
    assert captured["name"] == "public-egress-gateway.fetch"
    assert captured["kind"] is SpanKind.SERVER
    parent = get_current_span(captured["context"]).get_span_context()
    assert parent.trace_id == int("0123456789abcdef0123456789abcdef", 16)
