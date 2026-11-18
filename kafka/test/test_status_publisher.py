"""Native protocol evidence; no application topics or fake runtime broker."""

from __future__ import annotations

import json
import struct
import ssl
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


def test_network_probe_uses_selected_endpoint_and_hostname_verified_tls(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        owner.PREFIX + "UI_BROKER_ENDPOINT", "CLIENT://broker.example.invalid:19092"
    )
    monkeypatch.setenv(owner.PREFIX + "UI_BROKER_PROTOCOL", "SSL")
    response = struct.pack(">ihi", 1, 0, 1) + struct.pack(">hhh", 3, 0, 12)
    raw = MagicMock()
    raw.__enter__.return_value = raw
    raw.recv.side_effect = AssertionError("plaintext socket was used")
    secured = MagicMock()
    secured.__enter__.return_value = secured
    secured.recv.side_effect = [struct.pack(">i", len(response)), response]
    context = Mock()
    context.wrap_socket.return_value = secured
    tls = Mock(return_value=context)
    connection = Mock(return_value=raw)
    monkeypatch.setattr(owner, "tls", tls, raising=False)
    monkeypatch.setattr(owner.socket, "create_connection", connection)
    assert owner.api_versions(cfg) == "ONLINE"
    connection.assert_called_once_with(
        ("broker.example.invalid", 19092), timeout=cfg.timeout
    )
    tls.assert_called_once_with(
        cfg.secret_root, "kafka-ca.crt", "kafka-client.crt", "kafka-client.key"
    )
    context.wrap_socket.assert_called_once_with(
        raw, server_hostname="broker.example.invalid"
    )


def test_tls_failure_never_falls_back_to_plaintext(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        owner.PREFIX + "UI_BROKER_ENDPOINT", "CLIENT://broker.example.invalid:19092"
    )
    monkeypatch.setenv(owner.PREFIX + "UI_BROKER_PROTOCOL", "SSL")
    monkeypatch.setattr(
        owner,
        "tls",
        Mock(side_effect=ssl.SSLCertVerificationError("test rejection")),
        raising=False,
    )
    connection = Mock()
    monkeypatch.setattr(owner.socket, "create_connection", connection)
    state, components = owner.probe(cfg)
    assert state == components[0]["state"] == "UNKNOWN"
    connection.assert_not_called()


@pytest.mark.parametrize(
    "endpoint",
    [
        "CLIENT://user:secret@broker.invalid:19092",
        "CLIENT://broker.invalid:0",
        "CLIENT://broker.invalid:19092/path",
    ],
)
def test_invalid_broker_route_is_rejected_before_connect(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setenv(owner.PREFIX + "UI_BROKER_ENDPOINT", endpoint)
    monkeypatch.setenv(owner.PREFIX + "UI_BROKER_PROTOCOL", "SSL")
    connection = Mock()
    monkeypatch.setattr(owner.socket, "create_connection", connection)
    with pytest.raises(ValueError):
        owner.api_versions(cfg)
    connection.assert_not_called()
