"""Read-only operational API; translation remains Kafka-only."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

import os
from collections.abc import Mapping
from typing import Protocol, cast

from confluent_kafka.admin import AdminClient
from fastapi import FastAPI, HTTPException, Response

from translator_service.contracts import (
    NORMAL_REQUEST_TOPIC,
    NORMAL_REQUEST_TOPIC_V2,
    PRIORITY_REQUEST_TOPIC,
    PRIORITY_REQUEST_TOPIC_V2,
    RESULT_TOPIC,
    RESULT_TOPIC_V2,
)
from translator_service.observability import instrument_api
from translator_service.runtime import kafka_configuration, repository

app = FastAPI(
    title="DRP translator operations",
    docs_url=None,
    redoc_url=None,
    openapi_tags=[
        {
            "name": "default",
            "description": "Service liveness, readiness, and runtime diagnostics.",
        },
        {
            "name": "translator",
            "description": "Operator translation operations.",
        },
        {"name": "metrics", "description": "Prometheus metrics."},
    ],
)
instrument_api(app)


class _TopicMetadata(Protocol):
    error: object | None


class _ClusterMetadata(Protocol):
    brokers: Mapping[int, object]
    topics: Mapping[str, _TopicMetadata]


def read_status() -> dict[str, object]:
    repo = repository()
    try:
        return repo.status()
    finally:
        repo.engine.dispose()


def verify_kafka() -> None:
    metadata = cast(
        _ClusterMetadata,
        AdminClient(kafka_configuration()).list_topics(timeout=3),
    )
    if not metadata.brokers:
        raise RuntimeError("TRANSLATOR_KAFKA_BROKER_UNAVAILABLE")
    required = {
        PRIORITY_REQUEST_TOPIC,
        NORMAL_REQUEST_TOPIC,
        RESULT_TOPIC,
        PRIORITY_REQUEST_TOPIC_V2,
        NORMAL_REQUEST_TOPIC_V2,
        RESULT_TOPIC_V2,
    }
    missing = required.difference(metadata.topics)
    if missing:
        raise RuntimeError(f"TRANSLATOR_TOPICS_UNAVAILABLE:{','.join(sorted(missing))}")
    unavailable = sorted(
        name for name in required if metadata.topics[name].error is not None
    )
    if unavailable:
        raise RuntimeError(f"TRANSLATOR_TOPICS_UNAVAILABLE:{','.join(unavailable)}")


@app.get("/healthz", tags=["default"], summary="Check API process liveness")
async def health() -> dict[str, str]:
    """Confirm that the API responds; do not check Kafka, storage, or provider."""
    return {"status": "ok"}


@app.get("/readyz", tags=["default"], summary="Check storage and Kafka readiness")
def ready() -> dict[str, str]:
    """Check the own database, Kafka broker, and all six required topics.

    Return 503 when a dependency is unavailable. This probe does not prove provider
    readiness or that workers are making progress.
    """
    try:
        read_status()
        verify_kafka()
    except Exception as error:
        raise HTTPException(status_code=503, detail="not ready") from error
    return {"status": "ready"}


@app.get("/v1/status", tags=["default"], summary="Read aggregate queue state")
def status() -> dict[str, object]:
    """Read desired state, control revision, priority streak, and job counts.

    Counts are grouped by job state and normal/priority queue. No request text,
    request identifiers, or consumer identities are returned.
    """
    return read_status()


@app.get("/v1/components", tags=["default"], summary="Read runtime component presence")
def components() -> dict[str, object]:
    """Read heartbeat observations for consumers, worker, outbox, and provider.

    Missing or expired observations mean UNKNOWN. A fresh heartbeat proves
    presence, not queue progress; provider observation uses only the opaque port.
    No customer identifiers or request text are returned.
    """
    from translator_service.presence import read

    repo = repository()
    try:
        return {
            "components": read(
                repo.engine, int(os.environ["AELEV_TRANSLATOR_PRESENCE_TTL_SECONDS"])
            )
        }
    finally:
        repo.engine.dispose()


@app.get(
    "/metrics",
    tags=["metrics"],
    summary="Read Prometheus queue metrics",
    response_class=Response,
)
def metrics() -> Response:
    """Export job counts by state and queue in Prometheus text format."""
    state = read_status()
    jobs = cast(dict[str, int], state["jobs"])
    lines = ["# TYPE aelev_translator_jobs gauge"]
    for key, value in sorted(jobs.items()):
        state, queue = key.split(":")
        lines.append(
            f'aelev_translator_jobs{{state="{state}",queue="{queue}"}} {value}'
        )
    return Response("\n".join(lines) + "\n", media_type="text/plain")
