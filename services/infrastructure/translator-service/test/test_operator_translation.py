"""Operator ingress must use the Kafka worker path and preserve failure semantics."""

import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from translator_service import operator_translation as module
from translator_service.contracts import digest, parse, result_bytes

REF = "google/madlad400-3b-mt-q4k@sha256:" + "a" * 64
ACTOR = digest(b"operator")


def configuration() -> dict[str, Any]:
    return {}


def no_sleep(seconds: float) -> None:
    pass


@pytest.mark.parametrize("value", ["", "TRUE", "False", "1"])
def test_invalid_enable_flag_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(module.PREFIX + "API_TRANSLATION_ENABLED", value)
    with pytest.raises(ValueError, match="TRANSLATOR_OPERATOR_ENABLE_FLAG_INVALID"):
        module.enabled()


def test_enable_flag_is_required_and_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(module.PREFIX + "API_TRANSLATION_ENABLED", raising=False)
    with pytest.raises(KeyError):
        module.enabled()
    for value, expected in [("false", False), ("true", True)]:
        monkeypatch.setenv(module.PREFIX + "API_TRANSLATION_ENABLED", value)
        assert module.enabled() is expected


@pytest.mark.parametrize("timeout,limit", [(0, 4), (601, 4), (300, 0), (300, 17)])
def test_operator_limits_are_validated(
    monkeypatch: pytest.MonkeyPatch,
    timeout: int,
    limit: int,
) -> None:
    monkeypatch.setenv(module.PREFIX + "API_TRANSLATION_TIMEOUT_SECONDS", str(timeout))
    monkeypatch.setenv(module.PREFIX + "API_TRANSLATION_MAX_IN_FLIGHT", str(limit))
    with pytest.raises(ValueError, match="LIMITS"):
        module.Settings.environment()


def test_operator_limits_have_no_hidden_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(module.PREFIX + "API_TRANSLATION_TIMEOUT_SECONDS", raising=False)
    with pytest.raises(KeyError):
        module.Settings.environment()


@pytest.fixture
def payload() -> module.TranslationInput:
    return module.TranslationInput(
        source_language="en", target_language="de", queue="normal", text="Hello"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_language": "english"},
        {"target_language": "DE"},
        {"text": " "},
        {"text": "a" * 12001},
        {"queue": "other"},
        {"text": 12},
        {"text": "\ud800"},
        {"request_id": "foreign"},
    ],
)
def test_input_is_bounded_and_not_a_foreign_job_lookup(
    overrides: dict[str, object],
) -> None:
    data = {
        "source_language": "en",
        "target_language": "de",
        "queue": "normal",
        "text": "Hello",
        **overrides,
    }
    with pytest.raises(ValidationError):
        module.TranslationInput.model_validate(data)


def test_event_uses_the_pinned_contract_and_preserves_exact_text(
    payload: module.TranslationInput,
) -> None:
    identity = str(uuid.uuid4())
    raw = module.request_bytes(payload, ACTOR, identity)
    event = parse(raw, "TranslationRequestedV2")
    assert event["id"] == identity == event["data"]["request_id"]
    assert event["time"] == event["data"]["recorded_at"]
    assert event["data"]["source_digest"] == digest(payload.text.encode())
    assert "translator_ref" not in event["data"]
    assert "queue" not in event["data"] and "priority" not in event["data"]
    assert event["data"]["provenance"]["input_refs"][0]["digest"] == ACTOR


@pytest.mark.parametrize(
    "queue,topic",
    [
        ("normal", module.NORMAL_REQUEST_TOPIC_V2),
        ("priority", module.PRIORITY_REQUEST_TOPIC_V2),
    ],
)
def test_selected_topic_and_exact_result_match(
    monkeypatch: pytest.MonkeyPatch,
    payload: module.TranslationInput,
    queue: str,
    topic: str,
) -> None:
    payload = payload.model_copy(update={"queue": queue})
    sent: list[tuple[str, bytes, bytes]] = []
    disposed: list[bool] = []

    class Engine:
        def dispose(self) -> None:
            disposed.append(True)

    class Repo:
        engine = Engine()

        def delivered_result(self, identity: str, request_digest: str) -> bytes:
            raw = sent[0][2]
            assert identity == sent[0][1].decode()
            assert request_digest == digest(raw)
            return result_bytes(
                json.loads(raw),
                request_digest,
                {
                    "status": "SUCCEEDED",
                    "translated_text": "Hallo",
                    "warnings": [],
                    "error_code": "",
                },
            )

    class Producer:
        def __init__(self, config: dict[str, object]) -> None:
            assert config["enable.idempotence"] is True and config["acks"] == "all"

        def produce(
            self,
            topic: str,
            *,
            key: bytes,
            value: bytes,
            on_delivery: Callable[[object, Any], None],
        ) -> None:
            sent.append((topic, key, value))
            on_delivery(None, None)

        def flush(self, timeout: float) -> int:
            return 0

    monkeypatch.setattr(module, "repository", Repo)
    monkeypatch.setattr(module, "Producer", Producer)
    monkeypatch.setattr(module, "kafka_configuration", configuration)
    service = module.OperatorTranslations(module.Settings(1, 1))
    assert service.translate(payload, ACTOR).translated_text == "Hallo"
    assert sent[0][0] == topic
    assert disposed == [True]
    assert service.slots.acquire(blocking=False)
    service.slots.release()


def test_busy_rejects_before_producer_or_storage(
    payload: module.TranslationInput,
) -> None:
    service = module.OperatorTranslations(module.Settings(1, 1))
    assert service.slots.acquire(blocking=False)
    try:
        with pytest.raises(module.OperatorTranslationError) as caught:
            service.translate(payload, ACTOR)
        assert caught.value.status == 429 and caught.value.request_id == ""
    finally:
        service.slots.release()


@pytest.mark.parametrize("result", ["ack-failure", "timeout", "mismatch", "failed"])
def test_unconfirmed_or_failed_work_is_not_reported_as_translation(
    monkeypatch: pytest.MonkeyPatch,
    payload: module.TranslationInput,
    result: str,
) -> None:
    sent: list[bytes] = []
    tick = 0.0

    def clock() -> float:
        nonlocal tick
        tick += 0.6
        return tick

    class Engine:
        def dispose(self) -> None:
            pass

    class Repo:
        engine = Engine()

        def delivered_result(self, identity: str, request_digest: str) -> bytes | None:
            if result == "timeout":
                return None
            raw = sent[0]
            if result == "mismatch":
                request_digest = digest(b"different request")
            return result_bytes(
                json.loads(raw),
                request_digest,
                {
                    "status": "FAILED" if result == "failed" else "SUCCEEDED",
                    "translated_text": "" if result == "failed" else "Hallo",
                    "warnings": [],
                    "error_code": (
                        "TRANSLATOR_EMPTY_OUTPUT" if result == "failed" else ""
                    ),
                },
            )

    class Producer:
        def __init__(self, config: dict[str, object]) -> None:
            pass

        def produce(
            self,
            topic: str,
            *,
            key: bytes,
            value: bytes,
            on_delivery: Callable[[object, Any], None],
        ) -> None:
            sent.append(value)
            if result != "ack-failure":
                on_delivery(None, None)

        def flush(self, timeout: float) -> int:
            return int(result == "ack-failure")

    monkeypatch.setattr(module, "repository", Repo)
    monkeypatch.setattr(module, "Producer", Producer)
    monkeypatch.setattr(module, "kafka_configuration", configuration)
    monkeypatch.setattr(module.time, "monotonic", clock)
    monkeypatch.setattr(module.time, "sleep", no_sleep)
    service = module.OperatorTranslations(module.Settings(1, 1))
    with pytest.raises(module.OperatorTranslationError) as caught:
        service.translate(payload, ACTOR)
    assert (
        caught.value.status
        == {"ack-failure": 503, "timeout": 504, "mismatch": 502, "failed": 422}[result]
    )
    assert caught.value.request_id == json.loads(sent[0])["id"]
    assert len(sent) == 1
    assert service.slots.acquire(blocking=False)
    service.slots.release()
