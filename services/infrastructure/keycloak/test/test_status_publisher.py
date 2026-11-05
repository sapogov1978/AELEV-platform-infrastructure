"""Test this owner's exact publication and failure handling, without live mutations."""

from __future__ import annotations

import json
import secrets
import ssl
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

import status_publisher as publisher
from status_publisher import Outbox, Publisher, Settings


@pytest.fixture
def cfg(tmp_path: Path) -> Settings:
    return Settings(
        "https://shell.invalid",
        "https://identity.invalid",
        "realm",
        "owner-publisher",
        "shell",
        "f879b580-5b4c-48c5-a077-2fc96c538a8c",
        "https://identity.invalid:9000/health/ready",
        30,
        10,
        tmp_path,
        tmp_path / "outbox.sqlite",
        "https://public-identity.invalid:18443",
    )


@pytest.fixture
def worker(cfg: Settings, monkeypatch: pytest.MonkeyPatch) -> Publisher:
    monkeypatch.setattr(
        publisher.ssl, "create_default_context", Mock(return_value=Mock())
    )
    return Publisher(cfg)


def test_description_only_identifies_its_owner(cfg: Settings) -> None:
    body = json.loads(cfg.description())
    assert body == {
        "schema_version": 1,
        "revision": 4,
        "ui_url": "https://public-identity.invalid:18443/admin/",
        "module_id": cfg.module_id,
        "service_kind": "INFRASTRUCTURE",
        "title": {"ru": "Идентификация и доступ", "en": "Identity and access"},
        "sections": [],
        "components": [
            {
                "id": "keycloak",
                "title": {"ru": "Keycloak", "en": "Keycloak"},
                "ui_url": "https://public-identity.invalid:18443/admin/",
            },
            {
                "id": "database",
                "title": {"ru": "Keycloak Database", "en": "Keycloak Database"},
            },
            {
                "id": "publisher",
                "title": {
                    "ru": "Keycloak Status Publisher",
                    "en": "Keycloak Status Publisher",
                },
            },
        ],
    }


def test_outbox_survives_restart_and_retries_exact_observation(cfg: Settings) -> None:
    box = Outbox(cfg.state_path, cfg.module_id)
    original = box.prepare("ONLINE", "2026-10-10T00:00:00Z")
    restored = Outbox(cfg.state_path, cfg.module_id)
    assert restored.pending() == original
    assert restored.prepare("OFFLINE", "2026-10-10T01:00:00Z") == original
    restored.acknowledge(original)
    second = restored.prepare("OFFLINE", "2026-10-10T01:00:00Z")
    assert json.loads(second)["sequence"] == 2
    with pytest.raises(ValueError):
        restored.acknowledge(original)


def test_foreign_uuid_cannot_reset_volume(cfg: Settings) -> None:
    Outbox(cfg.state_path, cfg.module_id)
    with pytest.raises(ValueError):
        Outbox(cfg.state_path, "67dfae64-72d4-48b8-beca-48cc8418223b")


def test_publisher_backup_restores_exact_state_into_fresh_volume(
    cfg: Settings,
) -> None:
    original = Outbox(cfg.state_path, cfg.module_id)
    pending = original.prepare("ONLINE", "2026-10-10T00:00:00Z")
    payload = original.backup_bytes()
    restored_path = cfg.state_path.with_name("restored.sqlite")
    Outbox.restore_bytes(restored_path, cfg.module_id, payload)
    restored = Outbox(restored_path, cfg.module_id)
    assert restored.pending() == pending
    assert json.loads(restored.pending())["sequence"] == 1


def test_publisher_restore_refuses_existing_or_foreign_state(cfg: Settings) -> None:
    source = Outbox(cfg.state_path, cfg.module_id)
    payload = source.backup_bytes()
    with pytest.raises(ValueError, match="already exists"):
        Outbox.restore_bytes(cfg.state_path, cfg.module_id, payload)
    foreign_path = cfg.state_path.with_name("foreign.sqlite")
    with pytest.raises(ValueError, match="another module"):
        Outbox.restore_bytes(
            foreign_path,
            "67dfae64-72d4-48b8-beca-48cc8418223b",
            payload,
        )
    assert not foreign_path.exists()


def test_withdrawal_confirms_pending_then_hides_owner(
    worker: Publisher, cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = Outbox(cfg.state_path, cfg.module_id)
    pending = box.prepare("ONLINE", "2026-10-10T00:00:00Z")
    transport = Mock()
    monkeypatch.setattr(worker, "publish", transport)
    worker.withdraw(box)
    statuses = [
        call.args[1] for call in transport.call_args_list if call.args[0] == "/status"
    ]
    assert statuses[0] == pending
    assert json.loads(statuses[1])["active"] is False
    assert json.loads(statuses[1])["sequence"] == 2
    assert box.pending() is None and not box.withdrawing()
    assert json.loads(box.prepare("ONLINE", "2026-10-10T01:00:00Z"))["sequence"] == 3


def test_withdrawal_failure_and_exact_retry_survive_restart(
    worker: Publisher, cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = Outbox(cfg.state_path, cfg.module_id)
    box.begin_withdrawal()
    pending = box.prepare("UNKNOWN", "2026-10-10T00:00:00Z", active=False)
    monkeypatch.setattr(worker, "publish", Mock(side_effect=OSError))
    with pytest.raises(OSError):
        worker.withdraw(box)
    restored = Outbox(cfg.state_path, cfg.module_id)
    assert restored.pending() == pending and restored.withdrawing()
    transport = Mock()
    monkeypatch.setattr(worker, "publish", transport)
    monkeypatch.setattr(worker, "probe", Mock(return_value=("ONLINE", [])))
    worker.cycle(restored)
    statuses = [
        call.args[1] for call in transport.call_args_list if call.args[0] == "/status"
    ]
    assert statuses[0] == pending
    assert json.loads(statuses[1])["active"] is True
    assert json.loads(statuses[1])["sequence"] == 2
    assert restored.pending() is None and not restored.withdrawing()


@pytest.mark.parametrize(
    "code,body,expected",
    [
        (200, {"status": "UP"}, "ONLINE"),
        (503, {"status": "DOWN"}, "OFFLINE"),
        (500, {}, "UNKNOWN"),
        (200, {"status": "OTHER"}, "UNKNOWN"),
    ],
)
def test_health_is_not_inferred_from_tcp(
    worker: Publisher,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    body: dict[str, str],
    expected: str,
) -> None:
    monkeypatch.setattr(
        publisher, "request", Mock(return_value=(code, json.dumps(body).encode()))
    )
    assert worker.health(worker.cfg.health_url)[0] == expected


@pytest.mark.parametrize("error", [OSError, ssl.SSLCertVerificationError, ValueError])
def test_network_or_trust_failure_is_unknown(
    worker: Publisher, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    monkeypatch.setattr(publisher, "request", Mock(side_effect=error))
    state, components = worker.probe()
    assert state == "UNKNOWN"
    assert [item["state"] for item in components] == ["UNKNOWN", "UNKNOWN"]


def test_cycle_repeats_exact_pending_after_lost_confirmation(
    worker: Publisher, cfg: Settings
) -> None:
    box = Outbox(cfg.state_path, cfg.module_id)
    components = [
        {"id": "keycloak", "state": "ONLINE", "observed_at": "2026-10-10T00:00:00Z"},
        {"id": "database", "state": "ONLINE", "observed_at": "2026-10-10T00:00:00Z"},
    ]
    worker.probe = Mock(return_value=("ONLINE", components))  # type: ignore[method-assign]
    send = Mock(side_effect=[None, OSError, None, None])
    worker.publish = send  # type: ignore[method-assign]
    with pytest.raises(OSError):
        worker.cycle(box)
    retained = box.pending()
    assert [item["id"] for item in json.loads(retained)["components"]] == [
        "keycloak",
        "database",
        "publisher",
    ]
    worker.cycle(box)
    assert send.call_args_list[1].args[1] == send.call_args_list[3].args[1] == retained
    assert box.pending() is None
    worker.probe.assert_called_once()


@pytest.mark.parametrize(
    "live,ready,database,expected",
    [
        ("UP", "UP", "UP", ("ONLINE", "ONLINE", "ONLINE")),
        ("UP", "DOWN", "DOWN", ("OFFLINE", "ONLINE", "OFFLINE")),
        ("DOWN", "DOWN", "UP", ("OFFLINE", "OFFLINE", "ONLINE")),
        ("UP", "UP", None, ("UNKNOWN", "ONLINE", "UNKNOWN")),
        ("UP", "UP", "OTHER", ("UNKNOWN", "ONLINE", "UNKNOWN")),
    ],
)
def test_components_have_independent_evidence(
    worker: Publisher,
    monkeypatch: pytest.MonkeyPatch,
    live: str,
    ready: str,
    database: str | None,
    expected: tuple[str, str, str],
) -> None:
    details = {
        "status": ready,
        "checks": (
            [
                {
                    "name": "Keycloak database connections async health check",
                    "status": database,
                }
            ]
            if database is not None
            else []
        ),
    }
    send = Mock(
        side_effect=[
            (200 if live == "UP" else 503, json.dumps({"status": live}).encode()),
            (200 if ready == "UP" else 503, json.dumps(details).encode()),
        ]
    )
    monkeypatch.setattr(publisher, "request", send)
    state, components = worker.probe()
    assert (state, components[0]["state"], components[1]["state"]) == expected
    assert send.call_args_list[0].args[1].endswith("/health/live")
    assert send.call_args_list[1].args[1] == worker.cfg.health_url


def test_duplicate_database_check_is_not_false_success(
    worker: Publisher, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = {"name": "Keycloak database connections async health check", "status": "UP"}
    monkeypatch.setattr(
        publisher,
        "request",
        Mock(
            return_value=(
                200,
                json.dumps({"status": "UP", "checks": [check, check]}).encode(),
            )
        ),
    )
    state, components = worker.probe()
    assert state == "UNKNOWN"
    assert components[1]["state"] == "UNKNOWN"


def test_failed_token_does_not_clear_pending(
    worker: Publisher, cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = Outbox(cfg.state_path, cfg.module_id)
    original = box.prepare("ONLINE", "2026-10-10T00:00:00Z")
    (cfg.secret_root / "client-secret").write_text(secrets.token_urlsafe(32))
    monkeypatch.setattr(publisher, "request", Mock(return_value=(401, b"{}")))
    with pytest.raises(RuntimeError):
        worker.cycle(box)
    assert box.pending() == original


def test_tls_is_verified_and_redirects_denied() -> None:
    context = ssl.create_default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert (
        publisher.NoRedirect().redirect_request(
            None, None, 302, "", {}, "https://other.invalid"
        )
        is None
    )


def test_provision_refuses_foreign_client(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        publisher.ssl, "create_default_context", Mock(return_value=Mock())
    )
    monkeypatch.setenv(publisher.PREFIX + "ADMIN_USER", "unit-user")
    password = cfg.secret_root / "admin-password"
    password.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    monkeypatch.setenv(publisher.PREFIX + "ADMIN_PASSWORD_FILE", str(password))
    replies: list[tuple[int, bytes]] = [
        (200, json.dumps({"access_token": secrets.token_urlsafe(32)}).encode()),
        (200, b'{"id":"role","name":"platform-ui-publisher"}'),
        (200, b'[{"id":"foreign","attributes":{"aelev.owner":"other"}}]'),
    ]
    transport = Mock(side_effect=replies)
    monkeypatch.setattr(publisher, "request", transport)
    with pytest.raises(ValueError):
        publisher.provision(cfg)
    assert transport.call_count == 3
    assert not (cfg.secret_root / "client-secret").exists()


def test_administrative_password_requires_a_regular_nonempty_file(
    cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        publisher.ssl, "create_default_context", Mock(return_value=Mock())
    )
    monkeypatch.setenv(publisher.PREFIX + "ADMIN_USER", "unit-user")
    missing = cfg.secret_root / "missing-password"
    monkeypatch.setenv(publisher.PREFIX + "ADMIN_PASSWORD_FILE", str(missing))
    with pytest.raises(ValueError, match="regular secret file"):
        publisher.provision(cfg)
    empty = cfg.secret_root / "empty-password"
    empty.write_text("\n", encoding="utf-8")
    monkeypatch.setenv(publisher.PREFIX + "ADMIN_PASSWORD_FILE", str(empty))
    with pytest.raises(ValueError, match="invalid secret file"):
        publisher.provision(cfg)


def test_no_redirect_or_plaintext_settings(cfg: Settings) -> None:
    assert publisher.https_url(cfg.ui_url) == cfg.ui_url
    for url in (
        "http://shell.invalid",
        "https://user:password@shell.invalid",
        "https://shell.invalid/#fragment",
    ):
        with pytest.raises(ValueError):
            publisher.https_url(url)
    assert replace(cfg, module_id=cfg.module_id).description() == cfg.description()
