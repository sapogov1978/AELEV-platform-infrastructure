"""Validate native TLS health semantics without SQL access."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from aelev_platform_ui_client import Settings

import status_publisher as owner


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv(owner.PREFIX + "FENCING_MODE", "docker")
    monkeypatch.setenv(owner.PREFIX + "UI_DESCRIPTION_REVISION", "4")
    for node in range(1, 4):
        for suffix, value in {
            "HOST": f"db-{node}.invalid",
            "REST_PORT": "8008",
            "ETCD_HOST": f"dcs-{node}.invalid",
            "ETCD_CLIENT_PORT": "2379",
        }.items():
            monkeypatch.setenv(owner.PREFIX + f"NODE_{node}_" + suffix, value)
    monkeypatch.setattr(owner, "tls", Mock(return_value=Mock()))
    return Settings(
        "https://shell.invalid",
        "https://identity.invalid",
        "realm",
        "owner",
        "shell",
        "28b83787-bd9a-430e-8114-f5bb7d3a60c7",
        30,
        5,
        tmp_path,
        tmp_path / "outbox.sqlite",
    )


@pytest.mark.parametrize(
    "component,code,payload,expected",
    [
        ("postgresql-1", 200, {"state": "running"}, "ONLINE"),
        ("postgresql-1", 503, {"state": "stopped"}, "OFFLINE"),
        ("postgresql-1", 200, {}, "UNKNOWN"),
        ("etcd-1", 200, {"health": "true"}, "ONLINE"),
        ("etcd-1", 503, {"health": "false"}, "OFFLINE"),
        ("etcd-1", 200, {}, "UNKNOWN"),
    ],
)
def test_native_health(
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
    code: int,
    payload: dict[str, str],
    expected: str,
) -> None:
    monkeypatch.setattr(
        owner, "request", Mock(return_value=(code, json.dumps(payload).encode()))
    )
    assert owner.node_state(cfg, component) == expected


def test_owned_composition(cfg: Settings) -> None:
    value = json.loads(owner.description(cfg))
    assert value["module_id"] == cfg.module_id
    assert value["revision"] == 4
    assert value["title"] == {
        "ru": "Хранилище структурированных данных",
        "en": "Structured data storage",
    }
    assert any("PostgreSQL" in part["title"]["ru"] for part in value["components"])
    assert {part["id"] for part in value["components"]} == {
        *owner.COMPONENTS,
        "fencer",
        "publisher",
        "writer",
    }
    assert value["service_kind"] == "INFRASTRUCTURE" and not value["sections"]


def test_watchdog_has_no_executor_component(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(owner.PREFIX + "FENCING_MODE", "watchdog")
    assert "fencer" not in {
        part["id"] for part in json.loads(owner.description(cfg))["components"]
    }


@pytest.mark.parametrize(
    "states,expected",
    [
        (["ONLINE", "OFFLINE", "OFFLINE"], "ONLINE"),
        (["OFFLINE", "OFFLINE", "OFFLINE"], "OFFLINE"),
        (["ONLINE", "ONLINE", "OFFLINE"], "OFFLINE"),
        (["UNKNOWN", "OFFLINE", "OFFLINE"], "UNKNOWN"),
        (["ONLINE", "UNKNOWN", "OFFLINE"], "UNKNOWN"),
    ],
)
def test_writer_requires_exactly_one_confirmed_primary(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch, states: list[str], expected: str
) -> None:
    monkeypatch.setattr(owner, "primary_state", lambda cfg, n: states[n - 1])
    assert owner.writer_state(cfg) == expected


@pytest.mark.parametrize(
    "code,role,state,expected",
    [
        (200, "primary", "running", "ONLINE"),
        (200, "replica", "running", "UNKNOWN"),
        (503, "replica", "running", "OFFLINE"),
        (503, "primary", "running", "OFFLINE"),
        (200, "primary", "starting", "UNKNOWN"),
    ],
)
def test_primary_probe_is_not_process_health(
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    role: str,
    state: str,
    expected: str,
) -> None:
    request = Mock(
        return_value=(code, json.dumps({"role": role, "state": state}).encode())
    )
    monkeypatch.setattr(owner, "request", request)
    assert owner.primary_state(cfg, 1) == expected
    assert request.call_args.args[1] == "https://db-1.invalid:8008/primary"


def test_writer_transport_failure_is_unknown(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(owner, "request", Mock(side_effect=TimeoutError("unavailable")))
    assert owner.writer_state(cfg) == "UNKNOWN"


def test_writer_requires_a_new_description_revision(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(owner.PREFIX + "UI_DESCRIPTION_REVISION", "3")
    with pytest.raises(ValueError, match="at least 4"):
        owner.description(cfg)
