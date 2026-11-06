"""Independent Kafka consumer, opaque-provider worker and durable outbox."""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Any

import httpx
from confluent_kafka import Consumer, KafkaError, Producer
from jsonschema import ValidationError
from opentelemetry.trace import SpanKind
from sqlalchemy.exc import SQLAlchemyError

from translator_service import health_heartbeat, observability
from translator_service.contracts import RESULT_TOPIC, RESULT_TOPIC_V2, digest
from translator_service.environment import database_sslmode, database_url
from translator_service.repository import Repository
from translator_service.retry import retry_code

LOGGER = logging.getLogger(__name__)
PROVIDER_CHECKPOINT_SCOPE = "opaque-translation-provider-v1"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = {"level": record.levelname, "event": record.getMessage()}
        for key in (
            "request_id",
            "correlationid",
            "traceparent",
            "duration_ms",
            "outcome",
            "actor_digest",
            "queue",
            "error_code",
        ):
            if hasattr(record, key):
                fields[key] = getattr(record, key)
        return json.dumps(fields, sort_keys=True)


def kafka_configuration() -> dict[str, Any]:
    config = {
        "bootstrap.servers": os.environ["AELEV_KAFKA_BOOTSTRAP_SERVERS"],
        "client.id": "translator-service-" + os.environ.get("HOSTNAME", "local"),
    }
    security = os.environ.get("AELEV_KAFKA_SECURITY_PROTOCOL", "SSL")
    local = os.environ.get("AELEV_ENVIRONMENT", "production") == "local"
    if security not in {"SSL", "PLAINTEXT"} or (not local and security != "SSL"):
        raise RuntimeError("TRANSLATOR_KAFKA_TRANSPORT_INVALID")
    config["security.protocol"] = security
    if security == "SSL":
        for target, source in {
            "ssl.ca.location": "AELEV_KAFKA_CA_FILE",
            "ssl.certificate.location": "AELEV_KAFKA_CLIENT_CERT_FILE",
            "ssl.key.location": "AELEV_KAFKA_CLIENT_KEY_FILE",
        }.items():
            filename = os.environ[source]
            if (
                filename == "NONE"
                or not filename
                or os.path.basename(filename) != filename
            ):
                raise RuntimeError("TRANSLATOR_KAFKA_IDENTITY_MISSING")
            config[target] = os.path.join(
                os.environ["AELEV_TRANSLATOR_KAFKA_SECRET_ROOT"], filename
            )
    return config


def repository() -> Repository:
    if (
        os.environ.get("AELEV_ENVIRONMENT", "production") != "local"
        and database_sslmode() != "verify-full"
    ):
        raise RuntimeError("TRANSLATOR_DATABASE_TLS_REQUIRED")
    return Repository(database_url(), execution_ref=PROVIDER_CHECKPOINT_SCOPE)


def compute(data: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "schema_version": "1.0.0",
        **{key: data[key] for key in ("source_language", "target_language")},
        "text": data["text"],
    }
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=os.environ["AELEV_TRANSLATOR_SOCKET_PATH"]),
        base_url="http://translation-provider",
        trust_env=False,
        timeout=240,
    ) as client:
        health_heartbeat.progress("worker", "provider")
        try:
            response = client.post("/v1/translate", json=payload)
        finally:
            health_heartbeat.progress("worker")
    if response.status_code == 422:
        code = response.json().get("detail", "TRANSLATOR_REQUEST_INVALID")
        permitted = {
            "TRANSLATOR_EMPTY_OUTPUT",
            "TRANSLATOR_CONTROL_TOKEN_LEAK",
            "TRANSLATOR_UNSUPPORTED_TARGET_LANGUAGE",
            "TRANSLATOR_UNSUPPORTED_SOURCE_LANGUAGE",
        }
        return {
            "status": "FAILED",
            "translated_text": "",
            "warnings": [],
            "error_code": code if code in permitted else "TRANSLATOR_REQUEST_INVALID",
        }
    response.raise_for_status()
    result = response.json()
    if result.get("schema_version") != payload["schema_version"]:
        raise RuntimeError("TRANSLATOR_PRIVATE_RESPONSE_INVALID")
    translated = result.get("translated_text")
    warnings = result.get("warnings", [])
    if (
        not isinstance(translated, str)
        or not translated.strip()
        or len(translated) > 48000
        or not isinstance(warnings, list)
        or len(warnings) > 32
        or any(
            not isinstance(warning, str) or len(warning) > 128 for warning in warnings
        )
    ):
        raise RuntimeError("TRANSLATOR_PRIVATE_RESPONSE_INVALID")
    return {
        "status": "SUCCEEDED",
        "translated_text": translated,
        "warnings": warnings,
        "error_code": "",
    }


def retry_translation(
    repo: Repository,
    lease: dict[str, Any],
    envelope: dict[str, Any],
    error: Exception,
) -> None:
    """Retain the job and record only a bounded cause and correlation."""
    code = retry_code(error)
    repo.retry(lease, code)
    LOGGER.warning(
        "translation_provider_retry",
        extra={
            "request_id": lease["request_id"],
            "correlationid": envelope["correlationid"],
            "traceparent": envelope["traceparent"],
            "error_code": code,
        },
    )


def _translation_segments(text: str) -> tuple[str, ...]:
    segments: list[str] = []
    for paragraph in text.splitlines():
        remaining = paragraph.strip()
        while remaining:
            if len(remaining) <= 800:
                segments.append(remaining)
                break
            boundary = remaining.rfind(" ", 0, 801)
            if boundary < 400:
                boundary = 800
            segments.append(remaining[:boundary].strip())
            remaining = remaining[boundary:].strip()
    return tuple(segments)


def execute_translation(
    lease: dict[str, Any], repo: Repository | None = None
) -> tuple[dict[str, Any], int]:
    """Measure only one execution, before any storage or result publication."""
    data = json.loads(lease["payload"])["data"]
    started = time.perf_counter()
    segments = (
        _translation_segments(data["text"])
        if repo is not None
        and data.get("operation", "TRANSLATE_TEXT") == "TRANSLATE_TEXT"
        else ()
    )
    result: dict[str, Any]
    if not segments:
        result = compute(data)
    else:
        assert repo is not None
        completed = repo.prepare_segments(lease, segments, PROVIDER_CHECKPOINT_SCOPE)
        translated: list[str] = []
        warnings: list[str] = []
        result = {}
        for index, segment in enumerate(segments):
            checkpoint = completed.get(index)
            if checkpoint is None:
                segment_data = {
                    **data,
                    "text": segment,
                    "source_digest": digest(segment.encode()),
                }
                segment_result = compute(segment_data)
                if segment_result["status"] != "SUCCEEDED":
                    result = segment_result
                    break
                checkpoint = (
                    segment_result["translated_text"],
                    list(segment_result.get("warnings", [])),
                )
                if not repo.complete_segment(
                    lease, index, checkpoint[0], checkpoint[1]
                ):
                    raise RuntimeError("TRANSLATOR_STALE_LEASE")
            translated.append(checkpoint[0])
            warnings.extend(checkpoint[1])
        else:
            result = {
                "status": "SUCCEEDED",
                "translated_text": "\n".join(translated),
                "warnings": list(dict.fromkeys(warnings)),
                "error_code": "",
            }
    duration_ms = round(1000 * (time.perf_counter() - started))
    if data["schema_version"] == "2.0.0":
        result["execution_duration_ms"] = duration_ms
    return result, duration_ms


def publish_once(repo: Repository, producer: Producer) -> None:
    for row in repo.pending():
        health_heartbeat.progress("outbox")
        envelope = json.loads(row["payload"])
        failures: list[KafkaError | None] = []

        def delivered(
            error: KafkaError | None,
            _message: Any,
            collected: list[KafkaError | None] = failures,
        ) -> None:
            collected.append(error)

        with observability.event_span(
            "publish-result", envelope.get("traceparent", ""), kind=SpanKind.PRODUCER
        ):
            producer.produce(
                (
                    RESULT_TOPIC_V2
                    if envelope["data"]["schema_version"] == "2.0.0"
                    else RESULT_TOPIC
                ),
                key=row["request_id"].encode(),
                value=bytes(row["payload"]),
                on_delivery=delivered,
            )
            outstanding = producer.flush(10)
            if outstanding or len(failures) != 1 or failures[0] is not None:
                raise RuntimeError("TRANSLATOR_KAFKA_PUBLISH_FAILED")
            repo.published(row["request_id"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "role",
        choices=(
            "consumer",
            "worker",
            "outbox",
            "health",
            "status",
            "control",
            "verify-restored-state",
        ),
    )
    parser.add_argument("--action", choices=("PAUSE", "RESUME", "RESET"))
    parser.add_argument("--command-id")
    parser.add_argument("--actor")
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--rationale")
    args = parser.parse_args()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    observability.configure(args.role)
    repo = repository()
    if args.role in {"health", "status"}:
        if args.role == "status":
            sys.stdout.write(json.dumps(repo.status()))
        return
    if args.role == "control":
        sys.stdout.write(
            json.dumps(
                {
                    "revision": repo.control(
                        args.action,
                        command_id=args.command_id,
                        actor=args.actor,
                        expected_revision=args.expected_revision,
                        rationale=args.rationale,
                    )
                }
            )
        )
        return
    if args.role == "verify-restored-state":
        from translator_service.restore import verify

        sys.stdout.write(json.dumps(verify(repo)))
        return
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: stopped.set())
    client: Any = None
    if args.role == "consumer":
        client = Consumer(
            {
                **kafka_configuration(),
                "group.id": os.environ["AELEV_TRANSLATOR_KAFKA_GROUP"],
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
                "isolation.level": "read_committed",
            }
        )
        topic = os.environ["AELEV_TRANSLATOR_REQUEST_TOPIC"]
        client.subscribe([topic, topic.removesuffix(".v1") + ".v2"])
    if args.role == "outbox":
        client = Producer(
            {**kafka_configuration(), "enable.idempotence": True, "acks": "all"}
        )
    from translator_service.presence import start

    presence_thread = start(repo.engine, args.role, stopped)
    try:
        while not stopped.is_set():
            try:
                health_heartbeat.progress(args.role)
                if args.role == "consumer":
                    health_heartbeat.progress("consumer", "kafka")
                    message = client.poll(1)
                    health_heartbeat.progress("consumer")
                    if message is None:
                        continue
                    if message.error():
                        if message.error().code() == KafkaError._PARTITION_EOF:
                            continue
                        raise RuntimeError("TRANSLATOR_KAFKA_READ_FAILED")
                    raw = message.value() or b""
                    try:
                        traceparent = json.loads(raw).get("traceparent", "")
                    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                        traceparent = ""
                    with observability.event_span(
                        "consume-request", traceparent, kind=SpanKind.CONSUMER
                    ):
                        try:
                            repo.accept(
                                raw,
                                int(os.environ["AELEV_TRANSLATOR_REQUEST_PRIORITY"]),
                            )
                        except (ValueError, ValidationError):
                            repo.quarantine(
                                f"{message.topic()}:{message.partition()}:"
                                f"{message.offset()}",
                                raw,
                                "TRANSLATION_CONTRACT_INVALID",
                            )
                    health_heartbeat.progress("consumer", "kafka")
                    client.commit(message=message, asynchronous=False)
                    health_heartbeat.progress("consumer")
                elif args.role == "outbox":
                    publish_once(repo, client)
                    stopped.wait(1)
                else:
                    lease = repo.lease(
                        int(os.environ["AELEV_TRANSLATOR_HIGH_PRIORITY_BURST"])
                    )
                    if lease is None:
                        stopped.wait(1)
                        continue
                    envelope = json.loads(lease["payload"])
                    try:
                        with observability.event_span(
                            "translate",
                            envelope.get("traceparent", ""),
                            kind=SpanKind.CONSUMER,
                        ) as span:
                            result, duration_ms = execute_translation(lease, repo)
                            span.set_attribute("aelev.outcome", result["status"])
                            span.set_attribute("aelev.duration_ms", duration_ms)
                            if not repo.complete(lease, result):
                                LOGGER.warning("translation_stale_lease")
                                continue
                            LOGGER.info(
                                "translation_completed",
                                extra={
                                    "request_id": lease["request_id"],
                                    "correlationid": envelope["correlationid"],
                                    "traceparent": envelope["traceparent"],
                                    "outcome": result["status"],
                                    "duration_ms": duration_ms,
                                },
                            )
                    except (
                        httpx.HTTPError,
                        TimeoutError,
                        RuntimeError,
                        ValueError,
                        ValidationError,
                    ) as error:
                        retry_translation(repo, lease, envelope, error)
            except SQLAlchemyError:
                if args.role == "consumer":
                    # Do not poll past an unpersisted record: restart from the
                    # last committed Kafka offset after the storage outage.
                    raise RuntimeError("TRANSLATOR_STORAGE_UNAVAILABLE") from None
                LOGGER.warning("translation_storage_retry")
                stopped.wait(5)
    finally:
        if health_heartbeat.enabled(args.role):
            health_heartbeat.withdraw(args.role)
        stopped.set()
        presence_thread.join(timeout=10)
        if args.role == "consumer":
            client.close()
        repo.engine.dispose()


if __name__ == "__main__":
    main()
