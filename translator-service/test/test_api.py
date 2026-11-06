"""Operational readiness includes the mandatory Kafka dependency."""

from typing import cast

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from translator_service import api


def test_liveness_ignores_unavailable_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> None:
        raise RuntimeError("dependency unavailable")

    monkeypatch.setattr(api, "verify_kafka", unavailable)
    with TestClient(api.app) as client:
        assert cast(httpx.Client, client).get("/healthz").json() == {"status": "ok"}


def test_ready_requires_database_and_kafka(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[str] = []

    def database_ready() -> dict[str, object]:
        checked.append("database")
        return {}

    monkeypatch.setattr(api, "read_status", database_ready)
    monkeypatch.setattr(api, "verify_kafka", lambda: checked.append("kafka"))

    assert api.ready() == {"status": "ready"}
    assert checked == ["database", "kafka"]


def test_ready_fails_when_kafka_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def database_ready() -> dict[str, object]:
        return {}

    monkeypatch.setattr(api, "read_status", database_ready)

    def unavailable() -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(api, "verify_kafka", unavailable)
    with pytest.raises(HTTPException) as error:
        api.ready()
    assert error.value.status_code == 503
    assert error.value.detail == "not ready"
