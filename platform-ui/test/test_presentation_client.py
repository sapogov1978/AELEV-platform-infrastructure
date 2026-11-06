"""Verify the public transport artifact, not a catalogue of consuming products."""

from __future__ import annotations

import json
import secrets
import ssl
from pathlib import Path
from unittest.mock import Mock

import pytest

import aelev_platform_ui_client as client


@pytest.fixture
def cfg(tmp_path: Path) -> client.Settings:
    return client.Settings(
        "https://shell.invalid",
        "https://identity.invalid",
        "realm",
        "owner",
        "shell",
        "03f353f0-bfca-4cd4-9da8-8c391f601d71",
        30,
        5,
        tmp_path,
        tmp_path / "outbox.sqlite",
    )


def test_durable_exact_retry_and_monotonic_sequence(cfg: client.Settings) -> None:
    journal = client.Outbox(cfg.state_path, cfg.module_id)
    body = journal.prepare("ONLINE", "2026-10-10T00:00:00Z", [])
    restored = client.Outbox(cfg.state_path, cfg.module_id)
    assert restored.pending() == body
    assert restored.prepare("OFFLINE", "2026-10-10T01:00:00Z") == body
    restored.acknowledge(body)
    assert (
        json.loads(restored.prepare("OFFLINE", "2026-10-10T01:00:00Z"))["sequence"] == 2
    )
    with pytest.raises(ValueError):
        restored.acknowledge(body)


def test_foreign_identity_cannot_take_volume(cfg: client.Settings) -> None:
    client.Outbox(cfg.state_path, cfg.module_id)
    with pytest.raises(ValueError):
        client.Outbox(cfg.state_path, "b8808b0c-cea4-4e88-9324-131b3cce607d")


def test_withdrawal_confirms_pending_bytes_before_leaving(
    cfg: client.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "tls", Mock(return_value=Mock()))
    worker = client.Publisher(cfg, b"{}", lambda: ("ONLINE", []))
    sender = Mock()
    monkeypatch.setattr(worker, "publish", sender)
    journal = client.Outbox(cfg.state_path, cfg.module_id)
    pending = journal.prepare("ONLINE", "2026-10-10T00:00:00Z")
    worker.withdraw(journal)
    statuses = [
        call.args[1] for call in sender.call_args_list if call.args[0] == "/status"
    ]
    assert statuses[0] == pending
    assert json.loads(statuses[1])["active"] is False
    assert json.loads(statuses[1])["sequence"] == 2
    assert journal.pending() is None and not journal.withdrawing()
    restored = client.Outbox(cfg.state_path, cfg.module_id)
    worker.cycle(restored)
    returned = json.loads(sender.call_args_list[-1].args[1])
    assert returned["active"] is True and returned["sequence"] == 3


def test_unconfirmed_withdrawal_survives_restart(
    cfg: client.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "tls", Mock(return_value=Mock()))
    worker = client.Publisher(cfg, b"{}", lambda: ("ONLINE", []))
    monkeypatch.setattr(worker, "publish", Mock(side_effect=OSError))
    journal = client.Outbox(cfg.state_path, cfg.module_id)
    pending = journal.prepare("ONLINE", "2026-10-10T00:00:00Z")
    with pytest.raises(OSError):
        worker.withdraw(journal)
    restored = client.Outbox(cfg.state_path, cfg.module_id)
    assert restored.withdrawing() and restored.pending() == pending
    sender = Mock()
    monkeypatch.setattr(worker, "publish", sender)
    worker.cycle(restored)
    statuses = [
        json.loads(call.args[1])
        for call in sender.call_args_list
        if call.args[0] == "/status"
    ]
    assert [report["sequence"] for report in statuses] == [1, 2, 3]
    assert [report["active"] for report in statuses] == [True, False, True]


def test_exact_withdrawal_retry_is_not_replaced_by_new_presence(
    cfg: client.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "tls", Mock(return_value=Mock()))
    worker = client.Publisher(cfg, b"{}", lambda: ("ONLINE", []))
    journal = client.Outbox(cfg.state_path, cfg.module_id)
    journal.begin_withdrawal()
    pending = journal.prepare("UNKNOWN", "2026-10-10T00:00:00Z", active=False)
    sender = Mock()
    monkeypatch.setattr(worker, "publish", sender)
    worker.cycle(client.Outbox(cfg.state_path, cfg.module_id))
    statuses = [
        call.args[1] for call in sender.call_args_list if call.args[0] == "/status"
    ]
    assert statuses[0] == pending
    assert json.loads(statuses[1])["active"] is True


@pytest.mark.parametrize(
    "url",
    [
        "http://shell.invalid",
        "https://user:secret@shell.invalid",
        "https://shell.invalid/#x",
        "https://shell.invalid/?x",
        "https://shell. invalid",
        "https://shell.invalid/\\x",
    ],
)
def test_unambiguous_https_only(url: str) -> None:
    with pytest.raises(ValueError):
        client.https_url(url)


@pytest.mark.parametrize(
    "url", ["http://own.invalid/ready", "https://user:secret@own.invalid/ready"]
)
def test_probe_request_cannot_bypass_tls(url: str) -> None:
    with pytest.raises(ValueError):
        client.request(ssl.create_default_context(), url, 5)


def test_only_internal_resource_url_allows_query() -> None:
    url = "https://identity.invalid/clients?clientId=owner"
    assert client.https_url(url, allow_query=True) == url
    with pytest.raises(ValueError):
        client.https_url(url)


@pytest.mark.parametrize("error", [OSError, ssl.SSLCertVerificationError, ValueError])
def test_probe_failure_means_unknown(error: type[Exception]) -> None:
    observation = client.observe("component", Mock(side_effect=error))
    assert observation["state"] == "UNKNOWN"
    assert observation["observed_at"]


@pytest.mark.parametrize(
    "states,result",
    [
        (["ONLINE", "ONLINE"], "ONLINE"),
        (["ONLINE", "UNKNOWN"], "UNKNOWN"),
        (["ONLINE", "OFFLINE"], "OFFLINE"),
        ([], "UNKNOWN"),
    ],
)
def test_aggregate_requires_positive_evidence(states: list[str], result: str) -> None:
    assert client.aggregate([{"state": state} for state in states]) == result


@pytest.mark.parametrize("code", [400, 401, 403, 409, 429, 500, 503])
def test_failed_publication_keeps_exact_bytes(
    cfg: client.Settings, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    monkeypatch.setattr(client, "tls", Mock(return_value=Mock()))
    worker = client.Publisher(cfg, b"{}", lambda: ("ONLINE", []))
    monkeypatch.setattr(
        worker, "access_token", Mock(return_value=secrets.token_hex(16))
    )
    monkeypatch.setattr(client, "request", Mock(return_value=(code, b"{}")))
    outbox = client.Outbox(cfg.state_path, cfg.module_id)
    body = outbox.prepare("ONLINE", "2026-10-10T00:00:00Z")
    with pytest.raises(RuntimeError):
        worker.cycle(outbox)
    assert outbox.pending() == body


def test_success_acknowledges_and_preserves_original_time(
    cfg: client.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "tls", Mock(return_value=Mock()))
    worker = client.Publisher(
        cfg, b"{}", Mock(side_effect=AssertionError("pending must not be re-observed"))
    )
    monkeypatch.setattr(
        worker, "access_token", Mock(return_value=secrets.token_hex(16))
    )
    send = Mock(return_value=(200, b"{}"))
    monkeypatch.setattr(client, "request", send)
    outbox = client.Outbox(cfg.state_path, cfg.module_id)
    body = outbox.prepare("OFFLINE", "2026-10-10T00:00:00Z")
    worker.cycle(outbox)
    assert send.call_args_list[-1].args[4] == body
    assert outbox.pending() is None


def test_settings_require_one_explicit_source(
    cfg: client.Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = "AELEV_INFRASTRUCTURE_EXAMPLE_"
    values = {
        "URL": cfg.ui_url,
        "IDENTITY_URL": cfg.identity_url,
        "REALM": cfg.realm,
        "CLIENT_ID": cfg.client_id,
        "AUDIENCE": cfg.audience,
        "MODULE_ID": cfg.module_id,
        "INTERVAL_SECONDS": "30",
        "TIMEOUT_SECONDS": "5",
    }
    for key, value in values.items():
        monkeypatch.setenv(prefix + "UI_" + key, value)
    assert client.Settings.environment(prefix).module_id == cfg.module_id
    monkeypatch.delenv(prefix + "UI_URL")
    with pytest.raises(KeyError):
        client.Settings.environment(prefix)


def provision_files(cfg: client.Settings, tmp_path: Path) -> Path:
    admin_root = tmp_path / "admin"
    admin_root.mkdir()
    for name in ("admin-user", "admin-password"):
        (admin_root / name).write_text(secrets.token_hex(16))
    return admin_root


@pytest.mark.parametrize("mismatch", ["owner", "module", "ambiguous"])
def test_provision_refuses_foreign_or_ambiguous_identity(
    cfg: client.Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    admin_root = provision_files(cfg, tmp_path)
    declaration = {
        "id": "identity-id",
        "attributes": {
            "aelev.owner": "other" if mismatch == "owner" else "owner",
            "aelev.module_id": "other" if mismatch == "module" else cfg.module_id,
        },
    }
    replies = [
        (200, json.dumps({"access_token": secrets.token_hex(16)}).encode()),
        (200, b'{"id":"publisher-role","name":"platform-ui-publisher"}'),
        (
            200,
            json.dumps([declaration] * (2 if mismatch == "ambiguous" else 1)).encode(),
        ),
    ]
    send = Mock(side_effect=replies)
    monkeypatch.setattr(client, "tls", Mock(return_value=Mock()))
    monkeypatch.setattr(client, "request", send)
    with pytest.raises(ValueError):
        client.provision(cfg, "owner", admin_root)
    assert send.call_count == 3
    assert not (cfg.secret_root / "client-secret").exists()


def test_provision_updates_only_owned_identity_and_restricted_role(
    cfg: client.Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_root = provision_files(cfg, tmp_path)
    value = secrets.token_hex(32)
    replies = [
        (200, json.dumps({"access_token": secrets.token_hex(16)}).encode()),
        (200, b'{"id":"publisher-role","name":"platform-ui-publisher"}'),
        (
            200,
            json.dumps(
                [
                    {
                        "id": "identity-id",
                        "attributes": {
                            "aelev.owner": "owner",
                            "aelev.module_id": cfg.module_id,
                        },
                    }
                ]
            ).encode(),
        ),
        (204, b""),
        (200, b'{"id":"service-account"}'),
        (204, b""),
        (204, b""),
        (200, json.dumps({"value": value}).encode()),
    ]
    send = Mock(side_effect=replies)
    monkeypatch.setattr(client, "tls", Mock(return_value=Mock()))
    monkeypatch.setattr(client, "request", send)
    client.provision(cfg, "owner", admin_root)
    declaration = json.loads(send.call_args_list[3].args[4])
    assert not declaration["fullScopeAllowed"]
    assert not declaration["directAccessGrantsEnabled"]
    assert declaration["defaultClientScopes"] == ["roles"]
    assert (
        declaration["protocolMappers"][0]["config"]["included.client.audience"]
        == cfg.audience
    )
    assert (
        json.loads(send.call_args_list[5].args[4])[0]["name"] == "platform-ui-publisher"
    )
    assert (cfg.secret_root / "client-secret").read_text().strip() == value
    assert not (cfg.secret_root / "client-secret.pending").exists()
