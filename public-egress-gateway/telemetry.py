"""Owner-controlled OTLP logs and traces for Public Egress Gateway."""

from __future__ import annotations

import logging
import socket
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from typing import cast

from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import extract
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind


class GatewayLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith("aelev.public_egress_gateway")


def _regular_file(path: str) -> str:
    value = Path(path)
    if value.is_symlink() or not value.is_file():
        raise RuntimeError("EGRESS_TELEMETRY_CREDENTIAL_MISSING")
    return str(value)


def configure(
    endpoint: str,
    *,
    environment: str,
    ca_file: str,
    client_cert_file: str,
    client_key_file: str,
) -> TracerProvider | None:
    normalized = endpoint.strip().rstrip("/")
    if environment not in {"local", "production"}:
        raise RuntimeError("EGRESS_ENVIRONMENT_INVALID")
    if not normalized:
        if environment != "local":
            raise RuntimeError("EGRESS_TELEMETRY_REQUIRED")
        return None
    if not normalized.startswith("https://"):
        raise RuntimeError("EGRESS_TELEMETRY_HTTPS_REQUIRED")
    ca = _regular_file(ca_file)
    certificate = _regular_file(client_cert_file)
    key = _regular_file(client_key_file)
    resource = Resource.create(
        {
            "service.name": "public-egress-gateway",
            "service.namespace": "aelev.infrastructure",
            "service.version": "1.0.0",
            "service.instance.id": socket.gethostname(),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=normalized + "/v1/traces",
                certificate_file=ca,
                client_certificate_file=certificate,
                client_key_file=key,
            )
        )
    )
    trace.set_tracer_provider(provider)
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(
                endpoint=normalized + "/v1/logs",
                certificate_file=ca,
                client_certificate_file=certificate,
                client_key_file=key,
            )
        )
    )
    set_logger_provider(logger_provider)
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    handler.addFilter(GatewayLogFilter())
    logging.getLogger().addHandler(handler)
    return provider


@contextmanager
def request_span(
    headers: Mapping[str, str] | Message[str, str], route: str
) -> Generator[Span]:
    carrier: dict[str, str] = {}
    traceparent = cast(str | None, headers.get("traceparent", ""))
    traceparent = (traceparent or "").strip()
    if traceparent:
        carrier["traceparent"] = traceparent
    context: Context = extract(carrier)
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span(
        "public-egress-gateway.fetch",
        context=context,
        kind=SpanKind.SERVER,
        record_exception=False,
        set_status_on_exception=False,
        attributes={"http.route": route, "http.request.method": "POST"},
    ) as span:
        yield span
