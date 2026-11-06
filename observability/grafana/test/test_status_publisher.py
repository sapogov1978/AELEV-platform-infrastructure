"""No sibling-product probes; fail closed on uncertain responses."""

from pathlib import Path
from unittest.mock import Mock
import json

import pytest
from aelev_platform_ui_client import Settings
import status_publisher as owner


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv(owner.PREFIX + "UI_HEALTH_URL", "https://own.invalid/ready")
    monkeypatch.setenv(owner.PREFIX + "PUBLIC_HOST", "public-owner.invalid")
    monkeypatch.setenv(owner.PREFIX + "PORT", "13000")
    monkeypatch.setattr(owner, "tls", Mock(return_value=Mock()))
    return Settings(
        "https://shell.invalid",
        "https://identity.invalid",
        "realm",
        "owner",
        "shell",
        "11cc3a2b-f64f-4c71-96b7-3c7155f3c06b",
        30,
        5,
        tmp_path,
        tmp_path / "outbox.sqlite",
    )


def test_only_owned_components(cfg: Settings) -> None:
    value = json.loads(owner.description(cfg))
    assert value["module_id"] == cfg.module_id
    assert value["revision"] == 3
    assert value["ui_url"] == "https://public-owner.invalid:13000"
    assert value["components"][0]["ui_url"] == value["ui_url"]
    assert "ui_url" not in value["components"][1]
    assert value["title"] == {
        "ru": "Панели наблюдения и уведомления",
        "en": "Monitoring dashboards and notifications",
    }
    assert any("Grafana" in part["title"]["ru"] for part in value["components"])
    assert [part["id"] for part in value["components"]] == ["server", "publisher"]
    assert value["service_kind"] == "INFRASTRUCTURE" and not value["sections"]


@pytest.mark.parametrize(
    "code,expected",
    [(200, "ONLINE"), (503, "OFFLINE"), (401, "UNKNOWN"), (500, "UNKNOWN")],
)
def test_own_https_readiness(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch, code: int, expected: str
) -> None:
    call = Mock(return_value=(code, b'{"database":"ok"}'))
    monkeypatch.setattr(owner, "request", call)
    assert owner.server_state(cfg) == expected
    assert call.call_args.args[1] == "https://own.invalid/ready"


def test_transport_failure_is_not_offline(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(owner, "request", Mock(side_effect=OSError("unreachable")))
    state, components = owner.probe(cfg)
    assert state == "UNKNOWN" and components[0]["state"] == "UNKNOWN"
