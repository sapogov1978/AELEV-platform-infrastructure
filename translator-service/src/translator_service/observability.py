"""Owner-controlled OTLP logs and traces for translator-service."""

from __future__ import annotations

import logging
import os
import socket
import stat
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI
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
from starlette.requests import Request
from starlette.responses import Response


class TranslatorLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith("translator_service") or record.name == "__main__"


def _credential(path: str) -> str:
    value = Path(path)
    if value.is_symlink() or not value.is_file():
        raise RuntimeError("TRANSLATOR_TELEMETRY_CREDENTIAL_MISSING")
    return str(value)


def configure(role: str) -> TracerProvider | None:
    endpoint = os.environ.get("AELEV_TRANSLATOR_OTLP_HTTP_ENDPOINT", "").strip()
    environment = os.environ.get("AELEV_ENVIRONMENT", "production")
    if environment not in {"local", "production"}:
        raise RuntimeError("TRANSLATOR_ENVIRONMENT_INVALID")
    if not endpoint:
        if environment != "local":
            raise RuntimeError("TRANSLATOR_TELEMETRY_REQUIRED")
        return None
    if not endpoint.startswith("https://"):
        raise RuntimeError("TRANSLATOR_TELEMETRY_HTTPS_REQUIRED")
    ca = _credential(os.environ["AELEV_TRANSLATOR_TELEMETRY_CA_FILE"])
    certificate = _credential(os.environ["AELEV_TRANSLATOR_TELEMETRY_CLIENT_CERT_FILE"])
    key = _credential(os.environ["AELEV_TRANSLATOR_TELEMETRY_CLIENT_KEY_FILE"])
    if stat.S_IMODE(Path(key).stat().st_mode) & 0o007:
        raise RuntimeError("TRANSLATOR_TELEMETRY_PRIVATE_KEY_EXPOSED")
    resource = Resource.create(
        {
            "service.name": "translator-service",
            "service.namespace": "aelev.infrastructure",
            "service.version": "0.1.0",
            "service.instance.id": socket.gethostname(),
            "aelev.service.role": role,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=endpoint.rstrip("/") + "/v1/traces",
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
                endpoint=endpoint.rstrip("/") + "/v1/logs",
                certificate_file=ca,
                client_certificate_file=certificate,
                client_key_file=key,
            )
        )
    )
    set_logger_provider(logger_provider)
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    handler.addFilter(TranslatorLogFilter())
    logging.getLogger().addHandler(handler)
    return provider


@contextmanager
def event_span(
    operation: str,
    traceparent: str,
    *,
    kind: SpanKind,
) -> Generator[Span]:
    carrier: dict[str, str] = {}
    if traceparent:
        carrier["traceparent"] = traceparent
    context: Context = extract(carrier)
    with trace.get_tracer(__name__).start_as_current_span(
        "translator-service." + operation,
        context=context,
        kind=kind,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        span.set_attribute("messaging.system", "kafka")
        span.set_attribute("aelev.operation", operation)
        yield span


def instrument_api(application: FastAPI) -> None:
    """Trace HTTP metadata without tokens, request bodies, or URL values."""

    @application.middleware("http")
    async def traced_request(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        parent: Context = extract(request.headers)
        with trace.get_tracer(__name__).start_as_current_span(
            "translator-service.http",
            context=parent,
            kind=SpanKind.SERVER,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("http.request.method", request.method.upper())
            try:
                response = await call_next(request)
            except Exception as error:
                span.set_attribute("http.response.status_code", 500)
                span.set_attribute("error.type", type(error).__name__)
                raise
            route = request.scope.get("route")
            path = getattr(route, "path", None)
            if isinstance(path, str):
                span.set_attribute("http.route", path)
            span.set_attribute("http.response.status_code", response.status_code)
            return response
