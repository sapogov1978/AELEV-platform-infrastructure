"""Bounded operator requests through the existing Kafka and durable worker path."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Literal

from confluent_kafka import KafkaException, Producer
from jsonschema import ValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from translator_service.contracts import (
    NORMAL_REQUEST_TOPIC_V2,
    PRIORITY_REQUEST_TOPIC_V2,
    digest,
    parse,
)
from translator_service.operator_auth import PREFIX
from translator_service.protocol import LANGUAGE_PATTERN
from translator_service.runtime import kafka_configuration, repository

LOGGER = logging.getLogger(__name__)


def enabled() -> bool:
    value = os.environ[PREFIX + "API_TRANSLATION_ENABLED"]
    if value not in {"true", "false"}:
        raise ValueError("TRANSLATOR_OPERATOR_ENABLE_FLAG_INVALID")
    return value == "true"


class TranslationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_language: str = Field(
        pattern=LANGUAGE_PATTERN, max_length=35, examples=["en"]
    )
    target_language: str = Field(
        pattern=LANGUAGE_PATTERN, max_length=35, examples=["de"]
    )
    queue: Literal["normal", "priority"]
    text: str = Field(min_length=1, max_length=12000, examples=["Hello, world!"])

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("nonblank text required")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("valid UTF-8 text required") from error
        return value


class TranslationOutput(BaseModel):
    request_id: str
    source_language: str
    target_language: str
    queue: Literal["normal", "priority"]
    translated_text: str
    warnings: list[str]


class OperatorTranslationError(Exception):
    def __init__(self, status: int, code: str, request_id: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.request_id = request_id


@dataclass(frozen=True)
class Settings:
    timeout_seconds: int
    max_in_flight: int

    @classmethod
    def environment(cls) -> Settings:
        result = cls(
            int(os.environ[PREFIX + "API_TRANSLATION_TIMEOUT_SECONDS"]),
            int(os.environ[PREFIX + "API_TRANSLATION_MAX_IN_FLIGHT"]),
        )
        if (
            not 1 <= result.timeout_seconds <= 600
            or not 1 <= result.max_in_flight <= 16
        ):
            raise ValueError("TRANSLATOR_OPERATOR_LIMITS_INVALID")
        return result


def request_bytes(payload: TranslationInput, actor: str, identity: str) -> bytes:
    """Create one intent's immutable bytes, checked by the pinned public schema."""
    timestamp = datetime.now(UTC).isoformat()
    event = {
        "specversion": "1.0",
        "id": identity,
        "source": "urn:aelev:translator-service:operator",
        "type": "com.aelev.translation-requested.v2",
        "time": timestamp,
        "subject": "translation/" + identity,
        "datacontenttype": "application/json",
        "dataschema": "https://schemas.aelev.dev/events/v1#/$defs/TranslationRequestedV2",
        "correlationid": identity,
        "causationid": identity,
        "traceparent": "00-" + uuid.uuid4().hex + "-" + uuid.uuid4().hex[:16] + "-01",
        "data": {
            "schema_version": "2.0.0",
            "recorded_at": timestamp,
            "provenance": {
                "owner_context": "TRANSLATOR_SERVICE",
                "producer_id": "translator-service-operator",
                "producer_version": "0.1.0",
                "input_refs": [
                    {
                        "id": "urn:aelev:operator:" + actor.removeprefix("sha256:"),
                        "version": "oidc-subject-sha256-v1",
                        "digest": actor,
                    }
                ],
            },
            "request_id": identity,
            "text": payload.text,
            "source_digest": digest(payload.text.encode()),
            "source_language": payload.source_language,
            "target_language": payload.target_language,
        },
    }
    raw = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = raw.encode()
    parse(encoded, "TranslationRequestedV2")
    return encoded


class OperatorTranslations:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.slots = threading.BoundedSemaphore(settings.max_in_flight)

    def translate(self, payload: TranslationInput, actor: str) -> TranslationOutput:
        """Publish once, then read only this exact own request after its Kafka ACK."""
        if not self.slots.acquire(blocking=False):
            raise OperatorTranslationError(429, "TRANSLATOR_OPERATOR_BUSY", "")
        identity = str(uuid.uuid4())
        deadline = time.monotonic() + self.settings.timeout_seconds
        repo = None
        try:
            raw = request_bytes(payload, actor, identity)
            request = parse(raw, "TranslationRequestedV2")
            repo = repository()
            topic = (
                PRIORITY_REQUEST_TOPIC_V2
                if payload.queue == "priority"
                else NORMAL_REQUEST_TOPIC_V2
            )
            producer = Producer(
                {
                    **kafka_configuration(),
                    "enable.idempotence": True,
                    "acks": "all",
                    "message.timeout.ms": min(
                        10000, self.settings.timeout_seconds * 1000
                    ),
                    "queue.buffering.max.messages": 1,
                    "queue.buffering.max.kbytes": 256,
                }
            )
            failures: list[object] = []

            def delivered(error: object, _message: Any) -> None:
                failures.append(error)

            producer.produce(
                topic, key=identity.encode(), value=raw, on_delivery=delivered
            )
            outstanding = producer.flush(min(10, self.settings.timeout_seconds))
            if outstanding or failures != [None]:
                raise OperatorTranslationError(
                    503, "TRANSLATOR_KAFKA_DELIVERY_UNCONFIRMED", identity
                )
            LOGGER.info(
                "operator_translation_queued",
                extra={
                    "request_id": identity,
                    "actor_digest": actor,
                    "queue": payload.queue,
                },
            )
            while time.monotonic() < deadline:
                result = repo.delivered_result(identity, digest(raw))
                if result is not None:
                    event = parse(result, "TranslationCompletedV2")
                    data = event["data"]
                    if (
                        data["request_id"] != identity
                        or data["request_digest"] != digest(raw)
                        or event["causationid"] != identity
                        or event["correlationid"] != request["correlationid"]
                        or any(
                            data[key] != request["data"][key]
                            for key in (
                                "source_digest",
                                "source_language",
                                "target_language",
                            )
                        )
                    ):
                        raise OperatorTranslationError(
                            502, "TRANSLATOR_RESULT_MISMATCH", identity
                        )
                    if data["status"] != "SUCCEEDED":
                        raise OperatorTranslationError(
                            422, data["error_code"], identity
                        )
                    return TranslationOutput(
                        request_id=identity,
                        source_language=data["source_language"],
                        target_language=data["target_language"],
                        queue=payload.queue,
                        translated_text=data["translated_text"],
                        warnings=data["warnings"],
                    )
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
            raise OperatorTranslationError(504, "TRANSLATOR_RESULT_TIMEOUT", identity)
        except (KafkaException, BufferError) as error:
            raise OperatorTranslationError(
                503, "TRANSLATOR_KAFKA_DELIVERY_UNCONFIRMED", identity
            ) from error
        except SQLAlchemyError as error:
            raise OperatorTranslationError(
                503, "TRANSLATOR_STORAGE_UNAVAILABLE", identity
            ) from error
        except (ValidationError, ValueError) as error:
            raise OperatorTranslationError(
                502, "TRANSLATOR_RESULT_INVALID", identity
            ) from error
        finally:
            if repo is not None:
                repo.engine.dispose()
            self.slots.release()


@lru_cache(maxsize=1)
def service() -> OperatorTranslations:
    return OperatorTranslations(Settings.environment())
