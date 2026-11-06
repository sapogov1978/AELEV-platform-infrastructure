"""Bounded owner-local retry diagnostics, never exception text or payloads."""

from __future__ import annotations

from typing import cast

import httpx
from jsonschema import ValidationError

RUNTIME_CODES = frozenset(
    {
        "TRANSLATOR_DEADLINE_EXCEEDED",
        "TRANSLATOR_NATIVE_NOT_READY",
        "TRANSLATOR_NATIVE_UNAVAILABLE",
        "TRANSLATOR_NATIVE_RESPONSE_INVALID",
        "TRANSLATOR_NATIVE_TOKENIZATION_INVALID",
        "TRANSLATOR_NATIVE_VERSION_MISMATCH",
        "TRANSLATOR_OUTPUT_LIMIT_EXCEEDED",
        "TRANSLATOR_EXECUTION_NOT_CONFIGURED",
        "TRANSLATOR_EXECUTION_UNAVAILABLE",
        "TRANSLATOR_PRIVATE_RESPONSE_INVALID",
        "TRANSLATOR_SEGMENTS_INVALID",
        "TRANSLATOR_STALE_LEASE",
    }
)


def retry_code(error: Exception) -> str:
    """Classify a transient failure without exposing arbitrary error details."""
    if isinstance(error, TimeoutError | httpx.TimeoutException):
        return "TRANSLATOR_DEADLINE_EXCEEDED"
    if isinstance(error, httpx.ConnectError):
        return "TRANSLATOR_PROVIDER_UNREACHABLE"
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code == 503:
            try:
                if len(error.response.content) <= 1024:
                    body: object = error.response.json()
                    if isinstance(body, dict):
                        code = cast(dict[str, object], body).get("detail")
                        if isinstance(code, str) and code in RUNTIME_CODES:
                            return code
            except (ValueError, httpx.ResponseNotRead):
                pass
        return "TRANSLATOR_PROVIDER_HTTP_ERROR"
    if isinstance(error, httpx.HTTPError):
        return "TRANSLATOR_PROVIDER_TRANSPORT_ERROR"
    if isinstance(error, ValidationError):
        return "TRANSLATOR_RESULT_INVALID"
    if isinstance(error, ValueError):
        return "TRANSLATOR_PRIVATE_RESPONSE_INVALID"
    if isinstance(error, RuntimeError) and str(error) in RUNTIME_CODES:
        return str(error)
    return "TRANSLATOR_UNAVAILABLE"
