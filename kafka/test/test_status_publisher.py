"""Native protocol evidence; no application topics or fake runtime broker."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from aelev_platform_ui_client import Settings

import status_publisher as owner


@pytest.fixture
def cfg(tmp_path: Path) -> Settings:
    return Settings(
        "https://shell.invalid",
        "https://identity.invalid",
        "realm",
        "owner",
        "shell",
        "9bd1c0b4-66f3-4f71-9dbe-7cbd8d43af41",
        30,
        5,
        tmp_path,
        tmp_path / "outbox.sqlite",
    )


def test_only_owned_broker_and_publisher_are_declared(cfg: Settings) -> None:
    value = json.loads(owner.description(cfg))
    assert value["module_id"] == cfg.module_id
    assert value["revision"] == 2
    assert value["title"] == {
        "ru": "Межсервисное взаимодействие",
        "en": "Inter-service communication",
    }
    assert any("Kafka" in part["title"]["ru"] for part in value["components"])
    assert value["service_kind"] == "INFRASTRUCTURE"
    assert value["sections"] == []
    assert [part["id"] for part in value["components"]] == ["broker", "publisher"]


@pytest.mark.parametrize(
    "correlation,error,count,records,expected",
    [
        (1, 0, 1, [(3, 0, 12)], "ONLINE"),
        (1, 1, 0, [], "OFFLINE"),
        (2, 0, 1, [(3, 0, 12)], None),
        (1, 0, 0, [], None),
        (1, 0, 2, [(3, 0, 12), (3, 0, 12)], None),
    ],
)
def test_real_api_versions_structure(
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
    correlation: int,
    error: int,
    count: int,
    records: list[tuple[int, int, int]],
    expected: str | None,
) -> None:
    response = struct.pack(">ihi", correlation, error, count) + b"".join(
        struct.pack(">hhh", *record) for record in records
    )
    connection = MagicMock()
    connection.recv.side_effect = [struct.pack(">i", len(response)), response]
    connection.__enter__.return_value = connection
    monkeypatch.setattr(
        owner.socket, "create_connection", Mock(return_value=connection)
    )
    if expected is None:
        with pytest.raises(ValueError):
            owner.api_versions(cfg)
    else:
        assert owner.api_versions(cfg) == expected
    assert struct.unpack(">h", connection.sendall.call_args.args[0][4:6])[0] == 18


def test_transport_outage_is_unknown(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(owner.socket, "create_connection", Mock(side_effect=OSError))
    state, components = owner.probe(cfg)
    assert state == components[0]["state"] == "UNKNOWN"
