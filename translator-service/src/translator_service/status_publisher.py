"""Observe owned queue, transport, API and runtime presence, not consumers."""

from __future__ import annotations

import json
import os

from aelev_platform_ui_client import Settings, aggregate, observe, run, timestamp

from translator_service.api import verify_kafka
from translator_service.api_server import swagger_url
from translator_service.presence import read
from translator_service.runtime import repository

PREFIX = "AELEV_INFRASTRUCTURE_TRANSLATOR_"
COMPONENTS = {
    "database": "PostgreSQL переводчика",
    "kafka-connection": "Соединение с Kafka",
    "priority-consumer": "Приоритетный consumer",
    "normal-consumer": "Обычный consumer",
    "model": "Вычислительная модель",
    "outbox": "Outbox",
    "api": "Операционный API",
    "publisher": "Издатель состояния переводчика",
}


def description(cfg: Settings) -> bytes:
    url = swagger_url()
    revision = int(os.environ[PREFIX + "UI_DESCRIPTION_REVISION"])
    if not 1 <= revision <= 9223372036854775807:
        raise ValueError("TRANSLATOR_UI_REVISION_INVALID")
    return json.dumps(
        {
            "schema_version": 1,
            "module_id": cfg.module_id,
            "revision": revision,
            "swagger_url": url,
            "title": {
                "ru": "Перевод текстов",
                "en": "Text translation",
            },
            "service_kind": "INFRASTRUCTURE",
            "sections": [],
            "components": [
                {
                    "id": name,
                    "title": {"ru": title, "en": name},
                    **({"swagger_url": url} if name == "api" else {}),
                }
                for name, title in COMPONENTS.items()
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def probe(cfg: Settings) -> tuple[str, list[dict[str, str]]]:
    compute_desired_state = os.environ[
        PREFIX + "COMPUTE_DESIRED_STATE"
    ].strip().upper()
    if compute_desired_state not in {"RUNNING", "STOPPED"}:
        raise ValueError("TRANSLATOR_COMPUTE_DESIRED_STATE_INVALID")

    def kafka() -> str:
        try:
            verify_kafka()
            return "ONLINE"
        except Exception:
            return "UNKNOWN"

    observations = [observe("kafka-connection", kafka)]
    try:
        repo = repository()
        try:
            roles = read(
                repo.engine, int(os.environ["AELEV_TRANSLATOR_PRESENCE_TTL_SECONDS"])
            )
        finally:
            repo.engine.dispose()
        observations.append(
            {"id": "database", "state": "ONLINE", "observed_at": timestamp()}
        )
        runtime = [
            {
                "id": str(role["id"]),
                "state": str(role["state"]),
                "observed_at": role["observed_at"] or timestamp(),
            }
            for role in roles
        ]
        compute = [
            observation
            for observation in runtime
            if observation["id"] in {"model", "worker"}
        ]
        if {observation["id"] for observation in compute} != {"model", "worker"}:
            raise ValueError("TRANSLATOR_COMPUTE_OBSERVATION_INCOMPLETE")
        observations.extend(
            observation
            for observation in runtime
            if observation["id"] not in {"model", "worker"}
        )
        observations.append(
            {
                "id": "model",
                "state": aggregate(compute),
                "observed_at": timestamp(),
            }
        )
        if compute_desired_state == "STOPPED":
            for observation in observations:
                if observation["id"] == "model":
                    observation["state"] = "OFFLINE"
                    observation["observed_at"] = timestamp()
    except Exception:
        observations.extend(
            {"id": role, "state": "UNKNOWN", "observed_at": timestamp()}
            for role in (
                "database",
                "priority-consumer",
                "normal-consumer",
                "model",
                "outbox",
            )
        )

    def api() -> str:
        # This is an owner-private local endpoint, never the inter-service interface.
        import httpx

        with httpx.Client(timeout=cfg.timeout, trust_env=False) as client:
            try:
                response = client.get(
                    "http://translator-api:"
                    + os.environ["AELEV_INFRASTRUCTURE_TRANSLATOR_API_CONTAINER_PORT"]
                    + "/healthz"
                )
            except httpx.HTTPError:
                return "UNKNOWN"
            return (
                "ONLINE"
                if response.status_code == 200 and response.json() == {"status": "ok"}
                else "UNKNOWN"
            )

    observations.append(observe("api", api))
    return aggregate(observations), observations


if __name__ == "__main__":
    settings = Settings.environment(PREFIX)
    run(settings, "translator-status", description(settings), lambda: probe(settings))
