"""Own node endpoints and quorum, without bucket credentials."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from aelev_platform_ui_client import Settings

import status_publisher as owner


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for site in ("A", "B"):
        for node in range(1, 5):
            monkeypatch.setenv(
                owner.PREFIX + f"SITE_{site}_NODE_{node}_ENDPOINT",
                f"https://node-{site.lower()}-{node}.invalid:9000/data",
            )
    monkeypatch.setattr(owner, "tls", Mock(return_value=Mock()))
    return Settings(
        "https://shell.invalid",
        "https://identity.invalid",
        "realm",
        "owner",
        "shell",
        "d950fd82-d6e7-4a67-8bf9-dd50b8108719",
        30,
        5,
        tmp_path,
        tmp_path / "outbox.sqlite",
    )


def test_eight_owned_nodes_and_publisher(cfg: Settings) -> None:
    value = json.loads(owner.description(cfg))
    assert value["module_id"] == cfg.module_id
    assert value["revision"] == 2
    assert value["title"] == {"ru": "Хранилище артефактов", "en": "Artifact storage"}
    assert any("MinIO" in part["title"]["ru"] for part in value["components"])
    assert [part["id"] for part in value["components"]] == [*owner.NODES, "publisher"]
    assert value["service_kind"] == "INFRASTRUCTURE" and not value["sections"]


@pytest.mark.parametrize(
    "code,expected",
    [(200, "ONLINE"), (503, "OFFLINE"), (401, "UNKNOWN"), (500, "UNKNOWN")],
)
def test_native_write_quorum(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch, code: int, expected: str
) -> None:
    call = Mock(return_value=(code, b""))
    monkeypatch.setattr(owner, "request", call)
    assert owner.node_state(cfg, "site-a-1") == expected
    assert (
        call.call_args.args[1] == "https://node-a-1.invalid:9000/minio/health/cluster"
    )


def test_partial_outage_not_green(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        owner,
        "node_state",
        lambda cfg, node: "UNKNOWN" if node == "site-a-1" else "ONLINE",
    )
    state, components = owner.probe(cfg)
    assert state == "UNKNOWN" and len(components) == 8
