"""Self-presentation does not bypass JWT, mTLS or the public API."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from aelev_platform_ui_client import Settings

import status_publisher as owner


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(owner, "tls", Mock(return_value=Mock()))
    return Settings(
        "https://shell.invalid",
        "https://identity.invalid",
        "realm",
        "owner",
        "shell",
        "11c2d41e-b77c-4ffc-afce-7010bda8e214",
        30,
        5,
        tmp_path,
        tmp_path / "outbox.sqlite",
    )


def test_only_own_composition(cfg: Settings) -> None:
    value = json.loads(owner.description(cfg))
    assert value["module_id"] == cfg.module_id
    assert value["revision"] == 2
    assert value["title"] == {"ru": "Интерфейс платформы", "en": "Platform interface"}
    assert any("nginx" in part["title"]["ru"] for part in value["components"])
    assert [part["id"] for part in value["components"]] == ["nginx", "api", "publisher"]
    assert value["service_kind"] == "INFRASTRUCTURE" and not value["sections"]


@pytest.mark.parametrize(
    "code,payload,expected",
    [
        (200, {"status": "ready"}, "ONLINE"),
        (200, {}, "UNKNOWN"),
        (503, {}, "OFFLINE"),
        (403, {}, "UNKNOWN"),
    ],
)
def test_own_api_health(
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    payload: dict[str, str],
    expected: str,
) -> None:
    monkeypatch.setattr(
        owner, "request", Mock(return_value=(code, json.dumps(payload).encode()))
    )
    assert owner.component_state(cfg, "api") == expected


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"status": "ok"}, "ONLINE"),
        ({"status": "ready"}, "UNKNOWN"),
        ({}, "UNKNOWN"),
    ],
)
def test_shell_health_uses_canonical_response(
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, str],
    expected: str,
) -> None:
    request = Mock(return_value=(200, json.dumps(payload).encode()))
    monkeypatch.setattr(owner, "request", request)

    assert owner.component_state(cfg, "nginx") == expected
    assert request.call_args.args[1] == "https://shell.invalid/healthz"


def test_api_probe_uses_readiness_endpoint(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = Mock(return_value=(200, b'{"status":"ready"}'))
    monkeypatch.setattr(owner, "request", request)

    assert owner.component_state(cfg, "api") == "ONLINE"
    assert request.call_args.args[1] == "https://platform-ui-api:8444/api/readyz"
