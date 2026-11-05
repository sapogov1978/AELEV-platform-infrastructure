"""Validate only the generated, pinned public contract artifact."""

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

NORMAL_REQUEST_TOPIC = "translation.requests.v1"
PRIORITY_REQUEST_TOPIC = "translation.requests.priority.v1"
RESULT_TOPIC = "translation.results.v1"
NORMAL_REQUEST_TOPIC_V2 = "translation.requests.v2"
PRIORITY_REQUEST_TOPIC_V2 = "translation.requests.priority.v2"
RESULT_TOPIC_V2 = "translation.results.v2"


def event_name(raw: bytes, base: str) -> str:
    """Resolve only the two supported public contract generations."""
    if len(raw) > 262144:
        raise ValueError("TRANSLATION_EVENT_TOO_LARGE")
    value: Any = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("TRANSLATION_EVENT_INVALID")
    document = cast(dict[str, Any], value)
    if not isinstance(document.get("data"), dict):
        raise ValueError("TRANSLATION_EVENT_INVALID")
    return base + "V2" if document["data"].get("schema_version") == "2.0.0" else base


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@lru_cache(maxsize=1)
def validators() -> dict[str, Draft202012Validator]:
    root = Path(
        os.environ.get("AELEV_TRANSLATION_CONTRACT_ROOT", "/opt/aelev/contracts")
    )
    raw = (root / "translation-v1.json").read_bytes()
    if (
        hashlib.sha256(raw).hexdigest()
        != (root / "translation-v1.sha256").read_text().strip()
    ):
        raise RuntimeError("TRANSLATION_CONTRACT_DIGEST_MISMATCH")
    return {
        name: Draft202012Validator(schema, format_checker=FormatChecker())
        for name, schema in json.loads(raw)["schemas"].items()
    }


def parse(raw: bytes, name: str) -> dict[str, Any]:
    if len(raw) > 262144:
        raise ValueError("TRANSLATION_EVENT_TOO_LARGE")
    try:
        event = json.loads(raw)
        # jsonschema's untyped validate API is isolated at this library boundary.
        validator = cast(Any, validators()[name])
        validator.validate(event)
        data = event["data"]
        if event["time"] != data["recorded_at"]:
            raise ValueError("TRANSLATION_EVENT_TIME_MISMATCH")
        if name in {"TranslationRequested", "TranslationRequestedV2"}:
            if digest(data["text"].encode()) != data["source_digest"]:
                raise ValueError("TRANSLATION_SOURCE_DIGEST_MISMATCH")
            if data.get("operation") == "PROFILE_UNIT" and len(data["text"]) > 500:
                raise ValueError("TRANSLATION_PROFILE_UNIT_TOO_LARGE")
            if event["id"] != data["request_id"]:
                raise ValueError("TRANSLATION_REQUEST_ID_MISMATCH")
        else:
            if event["source"] != "urn:aelev:translator-service":
                raise ValueError("TRANSLATION_RESULT_OWNER_MISMATCH")
            if event["id"] != str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL, "translation-result:" + data["request_id"]
                )
            ):
                raise ValueError("TRANSLATION_RESULT_ID_MISMATCH")
            if data["status"] == "SUCCEEDED":
                if not data["translated_text"].strip() or data["error_code"]:
                    raise ValueError("TRANSLATION_RESULT_INVALID")
            elif data["translated_text"] or not data["error_code"]:
                raise ValueError("TRANSLATION_RESULT_INVALID")
        return event
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("TRANSLATION_EVENT_INVALID") from error


def result_bytes(
    request: dict[str, Any], request_digest: str, response: dict[str, Any]
) -> bytes:
    data = request["data"]
    v2 = data["schema_version"] == "2.0.0"
    name = "TranslationCompletedV2" if v2 else "TranslationCompleted"
    major = 2 if v2 else 1
    timestamp = datetime.now(UTC).isoformat()
    event_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, "translation-result:" + data["request_id"])
    )
    event = {
        "specversion": "1.0",
        "id": event_id,
        "source": "urn:aelev:translator-service",
        "type": f"com.aelev.translation-completed.v{major}",
        "time": timestamp,
        "subject": "translation/" + data["request_id"],
        "datacontenttype": "application/json",
        "dataschema": f"https://schemas.aelev.dev/events/v1#/$defs/{name}",
        "correlationid": request["correlationid"],
        "causationid": request["id"],
        "traceparent": request["traceparent"],
        "data": {
            "schema_version": f"{major}.0.0",
            "recorded_at": timestamp,
            "provenance": {
                "owner_context": "TRANSLATOR_SERVICE",
                "producer_id": "translator-service",
                "producer_version": "0.1.0",
                "input_refs": [
                    {
                        "id": "urn:aelev:translation-request:" + data["request_id"],
                        "version": f"{major}.0.0",
                        "digest": request_digest,
                    }
                ],
            },
            **{
                key: data[key]
                for key in (
                    "request_id",
                    "source_language",
                    "target_language",
                    "source_digest",
                )
            },
            **({"translator_ref": data["translator_ref"]} if not v2 else {}),
            "request_digest": request_digest,
            **response,
        },
    }
    raw = json.dumps(
        event, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    parse(raw, name)
    return raw
