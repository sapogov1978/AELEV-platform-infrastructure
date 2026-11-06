"""Owner-local technical observations, separate from jobs and canonical results."""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

LOGGER = logging.getLogger("translator.presence")
COMPONENTS = ("priority-consumer", "normal-consumer", "worker", "outbox", "provider")


def observe_provider() -> str:
    """Only a worker can inspect its opaque provider socket."""
    from translator_service.provider_healthcheck import main

    try:
        main()
        return "ONLINE"
    except httpx.ConnectError:
        # A refused or missing socket proves only that this cell cannot reach
        # its opaque provider; it does not inspect the provider implementation.
        return "OFFLINE"
    except Exception as error:
        # Timeout, invalid identity and malformed replies remain uncertainty.
        LOGGER.debug(
            "provider_presence_unknown", extra={"error_type": type(error).__name__}
        )
        return "UNKNOWN"


def start(engine: Engine, role: str, stop: threading.Event) -> threading.Thread:
    """Maintain liveness while a legitimate translation is being computed."""
    instance = str(uuid.uuid4())
    interval = int(os.environ["AELEV_TRANSLATOR_PRESENCE_INTERVAL_SECONDS"])
    if interval <= 0:
        raise ValueError("positive presence interval required")
    component = role
    if role == "consumer":
        component = (
            "priority-consumer"
            if os.environ["AELEV_TRANSLATOR_REQUEST_PRIORITY"] == "0"
            else "normal-consumer"
        )

    def loop() -> None:
        while not stop.is_set():
            observations = [(component, "ONLINE")]
            if role == "worker":
                observations.append(("provider", observe_provider()))
            try:
                with engine.begin() as connection:
                    for name, state in observations:
                        connection.execute(
                            text(
                                """INSERT INTO translation_runtime_presence
                            (component,instance,observed_at,state)
                            VALUES(:component,:instance,:time,:state)
                            ON CONFLICT(component,instance) DO UPDATE
                            SET observed_at=EXCLUDED.observed_at,state=EXCLUDED.state"""
                            ),
                            {
                                "component": name,
                                "instance": instance,
                                "time": datetime.now(UTC),
                                "state": state,
                            },
                        )
                    # Retention applies only to this rebuildable presence projection.
                    connection.execute(
                        text(
                            "DELETE FROM translation_runtime_presence "
                            "WHERE observed_at < now() - interval '1 day'"
                        )
                    )
            except SQLAlchemyError:
                LOGGER.warning("runtime_presence_storage_unavailable")
            stop.wait(interval)

    thread = threading.Thread(target=loop, name="runtime-presence", daemon=True)
    thread.start()
    return thread


def read(engine: Engine, ttl_seconds: int) -> list[dict[str, str | None]]:
    """Aggregate live replicas, without turning API readiness into role health."""
    if ttl_seconds <= 0:
        raise ValueError("positive presence TTL required")
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text("""SELECT component,state,observed_at,
            observed_at >= now() - make_interval(secs => :ttl) AS fresh
            FROM translation_runtime_presence ORDER BY observed_at DESC"""),
                {"ttl": ttl_seconds},
            )
            .mappings()
            .all()
        )
    result: list[dict[str, str | None]] = []
    for component in COMPONENTS:
        current = [
            row for row in rows if row["component"] == component and row["fresh"]
        ]
        positive = next((row for row in current if row["state"] == "ONLINE"), None)
        selected = positive if positive is not None else current[0] if current else None
        result.append(
            {
                "id": component,
                "state": str(selected["state"]) if selected is not None else "UNKNOWN",
                "observed_at": (
                    selected["observed_at"].isoformat()
                    if selected is not None
                    else None
                ),
            }
        )
    return result
