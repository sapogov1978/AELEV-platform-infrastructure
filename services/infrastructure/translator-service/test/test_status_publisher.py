"""Owned runtime presence is presented without domain jobs or consumer names."""

import json
from pathlib import Path
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from aelev_platform_ui_client import Settings, timestamp

from translator_service import presence
from translator_service import status_publisher as owner


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("AELEV_TRANSLATOR_PRESENCE_TTL_SECONDS", "30")
    monkeypatch.setenv("AELEV_INFRASTRUCTURE_TRANSLATOR_UI_DESCRIPTION_REVISION", "3")
    monkeypatch.setenv("AELEV_INFRASTRUCTURE_TRANSLATOR_API_CONTAINER_PORT", "8080")
    monkeypatch.setenv(
        "AELEV_INFRASTRUCTURE_TRANSLATOR_COMPUTE_DESIRED_STATE", "RUNNING"
    )
    monkeypatch.setenv(
        "AELEV_INFRASTRUCTURE_TRANSLATOR_API_PUBLIC_URL",
        "https://translator.invalid:8443",
    )
    return Settings(
        "https://shell.invalid",
        "https://identity.invalid",
        "realm",
        "owner",
        "shell",
        "95779649-cefd-4c54-a65a-08f9472db1ce",
        30,
        5,
        tmp_path,
        tmp_path / "outbox.sqlite",
    )


def test_description_is_own_technical_composition(cfg: Settings) -> None:
    value = json.loads(owner.description(cfg))
    assert value["module_id"] == cfg.module_id
    assert value["revision"] == 3
    assert value["swagger_url"] == "https://translator.invalid:8443/docs"
    assert "ui_url" not in value
    for part in value["components"]:
        if part["id"] == "api":
            assert part["swagger_url"] == value["swagger_url"]
        else:
            assert "swagger_url" not in part
    assert value["title"] == {"ru": "Перевод текстов", "en": "Text translation"}
    model = next(part for part in value["components"] if part["id"] == "model")
    assert model["title"]["ru"] == "Вычислительная модель"
    assert "Index-Translate" not in model["title"]["ru"]
    assert "worker" not in {part["id"] for part in value["components"]}
    assert value["service_kind"] == "INFRASTRUCTURE"
    assert value["sections"] == []
    assert [part["id"] for part in value["components"]] == list(owner.COMPONENTS)


def test_address_and_revision_can_change_without_source_edits(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(owner.PREFIX + "API_PUBLIC_URL", "https://remote.owner.invalid")
    monkeypatch.setenv(owner.PREFIX + "UI_DESCRIPTION_REVISION", "4")
    value = json.loads(owner.description(cfg))
    assert value["revision"] == 4
    assert value["swagger_url"] == "https://remote.owner.invalid/docs"
    assert value["module_id"] == cfg.module_id


@pytest.mark.parametrize("revision", ["0", "-1", "9223372036854775808", "bad"])
def test_invalid_description_revision_fails_closed(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch, revision: str
) -> None:
    monkeypatch.setenv(owner.PREFIX + "UI_DESCRIPTION_REVISION", revision)
    with pytest.raises(ValueError):
        owner.description(cfg)


@pytest.mark.parametrize("unavailable", ["none", "database", "kafka", "api", "model"])
def test_failures_never_become_false_online(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    repo = MagicMock()
    monkeypatch.setattr(owner, "repository", Mock(return_value=repo))
    monkeypatch.setattr(
        owner,
        "verify_kafka",
        Mock(side_effect=OSError() if unavailable == "kafka" else None),
    )
    rows = [
        {
            "id": name,
            "state": "UNKNOWN" if name == unavailable else "ONLINE",
            "observed_at": timestamp(),
        }
        for name in presence.COMPONENTS
    ]
    monkeypatch.setattr(
        owner,
        "read",
        Mock(
            side_effect=OSError() if unavailable == "database" else None,
            return_value=rows,
        ),
    )
    connection = MagicMock()
    response = connection.__enter__.return_value.get
    response.side_effect = (
        httpx.ConnectError("unreachable") if unavailable == "api" else None
    )
    response.return_value.status_code = 200
    response.return_value.json.return_value = {"status": "ok"}
    monkeypatch.setattr(httpx, "Client", Mock(return_value=connection))
    state, observations = owner.probe(cfg)
    assert state == ("ONLINE" if unavailable == "none" else "UNKNOWN")
    assert len(observations) == len(owner.COMPONENTS) - 1
    assert {part["id"] for part in observations} == set(owner.COMPONENTS) - {
        "publisher"
    }
    repo.engine.dispose.assert_called_once()


def test_intentionally_stopped_compute_is_reported_offline(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = MagicMock()
    monkeypatch.setattr(owner, "repository", Mock(return_value=repo))
    monkeypatch.setattr(owner, "verify_kafka", Mock(return_value=None))
    monkeypatch.setattr(
        owner,
        "read",
        Mock(
            return_value=[
                {"id": name, "state": "UNKNOWN", "observed_at": None}
                for name in presence.COMPONENTS
            ]
        ),
    )
    connection = MagicMock()
    response = connection.__enter__.return_value.get
    response.return_value.status_code = 200
    response.return_value.json.return_value = {"status": "ok"}
    monkeypatch.setattr(httpx, "Client", Mock(return_value=connection))
    monkeypatch.setenv(
        "AELEV_INFRASTRUCTURE_TRANSLATOR_COMPUTE_DESIRED_STATE", "STOPPED"
    )

    state, observations = owner.probe(cfg)

    compute = {
        observation["id"]: observation["state"]
        for observation in observations
        if observation["id"] in {"model", "worker"}
    }
    assert compute == {"model": "OFFLINE"}
    assert state == "OFFLINE"


@pytest.mark.parametrize(
    ("model", "worker", "expected"),
    [
        ("ONLINE", "ONLINE", "ONLINE"),
        ("ONLINE", "UNKNOWN", "UNKNOWN"),
        ("ONLINE", "OFFLINE", "OFFLINE"),
    ],
)
def test_public_compute_component_aggregates_internal_model_and_worker(
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    worker: str,
    expected: str,
) -> None:
    repo = MagicMock()
    monkeypatch.setattr(owner, "repository", Mock(return_value=repo))
    monkeypatch.setattr(owner, "verify_kafka", Mock(return_value=None))
    monkeypatch.setattr(
        owner,
        "read",
        Mock(
            return_value=[
                {
                    "id": name,
                    "state": (
                        model
                        if name == "model"
                        else worker
                        if name == "worker"
                        else "ONLINE"
                    ),
                    "observed_at": timestamp(),
                }
                for name in presence.COMPONENTS
            ]
        ),
    )
    connection = MagicMock()
    response = connection.__enter__.return_value.get
    response.return_value.status_code = 200
    response.return_value.json.return_value = {"status": "ok"}
    monkeypatch.setattr(httpx, "Client", Mock(return_value=connection))

    state, observations = owner.probe(cfg)

    public = {row["id"]: row["state"] for row in observations}
    assert "worker" not in public
    assert public["model"] == expected
    assert state == expected
