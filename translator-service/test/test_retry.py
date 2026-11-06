"""Retry diagnostics remain bounded through provider, worker and structured logs."""

import json
from typing import Any, cast
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import ValidationError

from translator_service import index_translation_provider as provider
from translator_service import runtime
from translator_service.repository import Repository
from translator_service.retry import retry_code


@pytest.mark.parametrize(
    "error,code",
    [
        (TimeoutError("private text"), "TRANSLATOR_DEADLINE_EXCEEDED"),
        (httpx.ReadTimeout("private text"), "TRANSLATOR_DEADLINE_EXCEEDED"),
        (httpx.ConnectError("private text"), "TRANSLATOR_MODEL_UNREACHABLE"),
        (httpx.ReadError("private text"), "TRANSLATOR_MODEL_TRANSPORT_ERROR"),
        (ValidationError("private text"), "TRANSLATOR_RESULT_INVALID"),
        (ValueError("private text"), "TRANSLATOR_PRIVATE_RESPONSE_INVALID"),
        (RuntimeError("TRANSLATOR_NATIVE_NOT_READY"), "TRANSLATOR_NATIVE_NOT_READY"),
        (RuntimeError("private text"), "TRANSLATOR_UNAVAILABLE"),
    ],
)
def test_retry_cause_never_contains_arbitrary_error_text(
    error: Exception, code: str
) -> None:
    assert retry_code(error) == code


@pytest.mark.parametrize(
    "status,body,code",
    [
        (
            503,
            {"detail": "TRANSLATOR_DEADLINE_EXCEEDED"},
            "TRANSLATOR_DEADLINE_EXCEEDED",
        ),
        (503, {"detail": "TRANSLATOR_NATIVE_NOT_READY"}, "TRANSLATOR_NATIVE_NOT_READY"),
        (
            503,
            {"detail": "TRANSLATOR_NATIVE_UNAVAILABLE"},
            "TRANSLATOR_NATIVE_UNAVAILABLE",
        ),
        (503, {"detail": "private text"}, "TRANSLATOR_MODEL_HTTP_ERROR"),
        (503, {"detail": {"text": "private"}}, "TRANSLATOR_MODEL_HTTP_ERROR"),
        (503, {"detail": "x" * 1025}, "TRANSLATOR_MODEL_HTTP_ERROR"),
        (503, [], "TRANSLATOR_MODEL_HTTP_ERROR"),
        (500, {"detail": "TRANSLATOR_NATIVE_NOT_READY"}, "TRANSLATOR_MODEL_HTTP_ERROR"),
    ],
)
def test_private_http_error_accepts_only_known_bounded_503_codes(
    status: int, body: object, code: str
) -> None:
    response = httpx.Response(
        status, json=body, request=httpx.Request("POST", "http://private-model")
    )
    with pytest.raises(httpx.HTTPStatusError) as failure:
        response.raise_for_status()
    assert retry_code(failure.value) == code


def test_non_json_model_error_is_safe() -> None:
    response = httpx.Response(
        503, text="private text", request=httpx.Request("POST", "http://private-model")
    )
    with pytest.raises(httpx.HTTPStatusError) as failure:
        response.raise_for_status()
    assert retry_code(failure.value) == "TRANSLATOR_MODEL_HTTP_ERROR"


def test_worker_persists_cause_and_logs_only_correlation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Mock(spec=Repository)
    logger = Mock()
    monkeypatch.setattr(runtime, "LOGGER", logger)
    lease = {"request_id": "own-request", "payload": b"private text"}
    envelope = {"correlationid": "own-correlation", "traceparent": "own-trace"}
    runtime.retry_translation(repo, lease, envelope, httpx.ReadTimeout("private text"))
    repo.retry.assert_called_once_with(lease, "TRANSLATOR_DEADLINE_EXCEEDED")
    _, arguments = logger.warning.call_args
    record = runtime.logging.LogRecord(
        "test",
        runtime.logging.WARNING,
        __file__,
        0,
        "translation_model_retry",
        (),
        None,
    )
    record.__dict__.update(arguments["extra"])
    rendered = runtime.JsonFormatter().format(record)
    assert json.loads(rendered) == {
        "level": "WARNING",
        "event": "translation_model_retry",
        "request_id": "own-request",
        "correlationid": "own-correlation",
        "traceparent": "own-trace",
        "error_code": "TRANSLATOR_DEADLINE_EXCEEDED",
    }
    assert "private text" not in rendered


@pytest.mark.parametrize(
    "error,code",
    [
        (TimeoutError("private text"), "TRANSLATOR_DEADLINE_EXCEEDED"),
        (RuntimeError("TRANSLATOR_NATIVE_NOT_READY"), "TRANSLATOR_NATIVE_NOT_READY"),
        (RuntimeError("private text"), "TRANSLATOR_UNAVAILABLE"),
    ],
)
def test_provider_preserves_bounded_transient_cause(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    engine = Mock(spec=provider.TranslationEngine)
    engine.translate_text.side_effect = error
    engine.translator_ref = "own-execution"
    parsed = Mock(
        source_language="en", target_language="de", source_digest="own-digest"
    )
    monkeypatch.setattr(
        provider, "parse_evidence_translation_request", Mock(return_value=parsed)
    )
    with TestClient(provider.create_app(cast(Any, engine))) as connection:
        client = cast(Any, connection)
        response = client.post("/v1/translate-text", json={})
    assert response.status_code == 503
    assert response.json() == {"detail": code}
