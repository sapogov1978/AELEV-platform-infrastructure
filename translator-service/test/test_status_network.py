"""Actual mTLS owner diagnostics; no queue/model/operator side effects."""

from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from aelev_platform_ui_client import Settings
from provider_tls_fixtures import Authority

from translator_service import status_publisher as owner

ROOT = Path(__file__).resolve().parents[1]
PREFIX = owner.PREFIX


@pytest.fixture
def material(tmp_path: Path) -> dict[str, Path]:
    issuer = Authority(tmp_path / "issuer")
    foreign = Authority(tmp_path / "foreign")
    server, server_key = issuer.leaf([], dns_names=("api.test",))
    expired, expired_key = issuer.leaf([], expired=True, dns_names=("api.test",))
    query, query_key = issuer.leaf([], common_name="publisher-api-query")
    foreign_query, foreign_key = foreign.leaf([], common_name="foreign-query")
    expired_query, expired_query_key = issuer.leaf(
        [], expired=True, common_name="expired-query"
    )
    return {
        "ca": issuer.cert,
        "foreign_ca": foreign.cert,
        "server": server,
        "server_key": server_key,
        "expired": expired,
        "expired_key": expired_key,
        "query": query,
        "query_key": query_key,
        "foreign_query": foreign_query,
        "foreign_key": foreign_key,
        "expired_query": expired_query,
        "expired_query_key": expired_query_key,
    }


@contextmanager
def endpoint(
    material: dict[str, Path],
    body: bytes = b'{"status":"ok"}',
    *,
    code: int = 200,
    expired: bool = False,
    pause: float = 0,
) -> Generator[tuple[int, list[bytes]]]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    name = "expired" if expired else "server"
    context.load_cert_chain(material[name], material[name + "_key"])
    context.load_verify_locations(cafile=str(material["ca"]))
    context.verify_mode = ssl.CERT_REQUIRED
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(1.5)
    received: list[bytes] = []

    def serve() -> None:
        try:
            with listener.accept()[0] as peer:
                peer.settimeout(1.5)
                with context.wrap_socket(peer, server_side=True) as channel:
                    received.append(channel.recv(4096))
                    time.sleep(pause)
                    channel.sendall(
                        (
                            f"HTTP/1.1 {code} Response\r\n"
                            f"Content-Length: {len(body)}\r\n"
                            "Location: https://never-follow.test/\r\n"
                            "Connection: close\r\n\r\n"
                        ).encode()
                        + body
                    )
        except OSError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], received
    finally:
        listener.close()
        thread.join(3)
        assert not thread.is_alive()


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith(owner.NETWORK_PREFIX) or key == PREFIX + "UI_PROBE_PROFILE":
            monkeypatch.delenv(key)


@pytest.fixture
def cfg(tmp_path: Path) -> Settings:
    secret_root = tmp_path / "status"
    secret_root.mkdir()
    return Settings(
        "https://shell.test",
        "https://idp.test",
        "aelev",
        "owner",
        "shell",
        "95779649-cefd-4c54-a65a-08f9472db1ce",
        30,
        1,
        secret_root,
        secret_root / "status.sqlite",
    )


def network(
    cfg: Settings,
    material: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    port: int = 28496,
) -> None:
    monkeypatch.setenv(PREFIX + "UI_PROBE_PROFILE", "network")
    monkeypatch.setenv(
        owner.NETWORK_PREFIX + "URL", f"https://127.0.0.1:{port}/healthz"
    )
    monkeypatch.setenv(owner.NETWORK_PREFIX + "SERVER_NAME", "api.test")
    for field, item, filename in (
        ("CA_FILE", "ca", "api-ca.crt"),
        ("CERT_FILE", "query", "api-query.crt"),
        ("KEY_FILE", "query_key", "api-query.key"),
    ):
        destination = cfg.secret_root / filename
        shutil.copyfile(material[item], destination)
        destination.chmod(0o600)
        monkeypatch.setenv(owner.NETWORK_PREFIX + field, str(destination))


def observed(cfg: Settings) -> str:
    return owner.observe("api", lambda: owner.network_api(cfg) or "UNKNOWN")["state"]


def test_real_own_query_identity_explicit_endpoint_and_separate_sni(
    material: dict[str, Path], cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with endpoint(material) as (port, received):
        network(cfg, material, monkeypatch, port)
        assert owner.network_api(cfg) == "ONLINE"
    assert len(received) == 1
    assert received[0].startswith(b"GET /healthz HTTP/1.1\r\n")


@pytest.mark.parametrize(
    "kind",
    [
        "wrong-name",
        "wrong-ca",
        "foreign-client",
        "missing-client",
        "expired-server",
        "expired-client",
        "timeout",
        "redirect",
        "wrong-body",
        "malformed",
        "duplicate-field",
        "oversized",
    ],
)
def test_tls_and_response_negatives_never_false_online(
    kind: str, material: dict[str, Path], cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {
        "wrong-body": b'{"status":"ok","extra":true}',
        "malformed": b"[",
        "duplicate-field": b'{"status":"wrong","status":"ok"}',
        "oversized": b" " * 300 + b'{"status":"ok"}',
    }.get(kind, b'{"status":"ok"}')
    with endpoint(
        material,
        body,
        code=302 if kind == "redirect" else 200,
        expired=kind == "expired-server",
        pause=1.2 if kind == "timeout" else 0,
    ) as (port, received):
        network(cfg, material, monkeypatch, port)
        if kind == "wrong-name":
            monkeypatch.setenv(owner.NETWORK_PREFIX + "SERVER_NAME", "foreign.test")
        if kind == "wrong-ca":
            shutil.copyfile(material["foreign_ca"], cfg.secret_root / "api-ca.crt")
        if kind in ("foreign-client", "expired-client"):
            name = "foreign" if kind == "foreign-client" else "expired_query"
            cert_name = "foreign_query" if kind == "foreign-client" else "expired_query"
            key_name = name + "_key"
            shutil.copyfile(material[cert_name], cfg.secret_root / "api-query.crt")
            shutil.copyfile(material[key_name], cfg.secret_root / "api-query.key")
        if kind == "missing-client":
            (cfg.secret_root / "api-query.crt").unlink()
        assert observed(cfg) == "UNKNOWN"
    assert len(received) <= 1


@pytest.mark.parametrize("missing", ["profile", *owner.NETWORK_SUFFIXES])
def test_partial_network_never_invokes_legacy_http(
    missing: str,
    material: dict[str, Path],
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network(cfg, material, monkeypatch)
    monkeypatch.delenv(
        PREFIX + "UI_PROBE_PROFILE"
        if missing == "profile"
        else owner.NETWORK_PREFIX + missing
    )
    monkeypatch.setattr(owner, "verify_kafka", Mock(return_value=None))
    monkeypatch.setattr(owner, "repository", Mock(side_effect=RuntimeError("not used")))
    legacy = Mock(side_effect=AssertionError("legacy must not be called"))
    monkeypatch.setattr(httpx, "Client", legacy)
    _, parts = owner.probe(cfg)
    assert next(x["state"] for x in parts if x["id"] == "api") == "UNKNOWN"
    legacy.assert_not_called()


@pytest.mark.parametrize(
    "value",
    [
        "http://api.test/healthz",
        "https://user:secret@api.test/healthz",
        "https://api.test:0/healthz",
        "https://api.test:65536/healthz",
        "https://api.test:/healthz",
        "https://api.test/healthz?",
        "https://api.test/healthz#",
        "https://api.test/v1/translate",
        "https://api.test/healthz\r\n",
        "https://api%2etest/healthz",
        "https://bad_host/healthz",
    ],
)
def test_invalid_uri_does_not_open_socket(
    value: str,
    material: dict[str, Path],
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network(cfg, material, monkeypatch)
    monkeypatch.setenv(owner.NETWORK_PREFIX + "URL", value)
    connect = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(owner.socket, "create_connection", connect)
    assert observed(cfg) == "UNKNOWN"
    connect.assert_not_called()


@pytest.mark.parametrize(
    "kind", ["outside", "symlink", "hardlink", "exposed", "empty", "oversized"]
)
def test_own_query_material_boundary(
    kind: str,
    material: dict[str, Path],
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    network(cfg, material, monkeypatch)
    key = cfg.secret_root / "api-query.key"
    if kind == "outside":
        monkeypatch.setenv(
            owner.NETWORK_PREFIX + "KEY_FILE", str(material["query_key"])
        )
    elif kind == "symlink":
        key.unlink()
        key.symlink_to(material["query_key"])
    elif kind == "hardlink":
        os.link(key, tmp_path / "second-link")
    elif kind == "exposed":
        key.chmod(0o644)
    elif kind == "empty":
        key.write_bytes(b"")
    else:
        key.write_bytes(b"x" * (1024**2 + 1))
    connect = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(owner.socket, "create_connection", connect)
    assert observed(cfg) == "UNKNOWN"
    connect.assert_not_called()


def test_absent_profile_selects_legacy_only(cfg: Settings) -> None:
    assert owner.network_api(cfg) is None


def compiled(
    network_profile: bool, missing: str | None = None
) -> subprocess.CompletedProcess[str]:
    values = {
        "URL": "https://192.0.2.10:28496/healthz",
        "SERVER_NAME": "api.owner.test",
        "CA_FILE": "/run/secrets/status/api-ca.crt",
        "CERT_FILE": "/run/secrets/status/api-query.crt",
        "KEY_FILE": "/run/secrets/status/api-query.key",
    }
    if missing is not None:
        values.pop(missing)
    command = [
        "docker",
        "compose",
        "--env-file",
        ".env.example",
        "--profile",
        "*",
        "--file",
        "compose.remote.yml",
    ]
    if network_profile:
        command += ["--file", "compose.publisher-network.yml"]
    return subprocess.run(
        [*command, "config", "--no-env-resolution", "--format", "json"],
        cwd=ROOT,
        env={
            **{
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("AELEV_", "COMPOSE_"))
            },
            **{owner.NETWORK_PREFIX + k: v for k, v in values.items()},
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_profile_changes_only_one_publishers_probe_environment() -> None:
    before, after = compiled(False), compiled(True)
    assert before.returncode == after.returncode == 0, before.stderr + after.stderr
    base, current = json.loads(before.stdout), json.loads(after.stdout)
    env = current["services"]["translator-service-status-publisher"]["environment"]
    assert env.pop(PREFIX + "UI_PROBE_PROFILE") == "network"
    for suffix in owner.NETWORK_SUFFIXES:
        assert env.pop(owner.NETWORK_PREFIX + suffix)
    assert current == base


@pytest.mark.parametrize("missing", owner.NETWORK_SUFFIXES)
def test_selected_profile_requires_all_inputs(missing: str) -> None:
    assert compiled(True, missing).returncode != 0
