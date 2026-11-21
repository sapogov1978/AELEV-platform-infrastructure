"""Real explicit-endpoint TLS probes and opt-in model preservation."""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
from aelev_platform_ui_client import Settings

import status_publisher as owner

ROOT = Path(__file__).resolve().parents[1]
PREFIX = owner.PREFIX


def openssl(*args: str) -> None:
    subprocess.run(["openssl", *args], capture_output=True, check=True, timeout=15)


@pytest.fixture
def material(tmp_path: Path) -> dict[str, Path]:
    ca, ca_key = tmp_path / "ca.crt", tmp_path / "ca.key"
    openssl(
        "req",
        "-x509",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:prime256v1",
        "-nodes",
        "-days",
        "1",
        "-subj",
        "/CN=temporary-status-test",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca),
    )
    result = {"ca": ca}
    for name, days in (("server", "1"), ("expired", "-1")):
        key, request, cert, ext = [
            tmp_path / (name + suffix) for suffix in (".key", ".csr", ".crt", ".ext")
        ]
        openssl(
            "req",
            "-new",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-subj",
            "/CN=probe.test",
            "-keyout",
            str(key),
            "-out",
            str(request),
        )
        ext.write_text(
            "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:probe.test\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n"
        )
        if name == "expired":
            (tmp_path / "new").mkdir()
            (tmp_path / "index").write_text("")
            (tmp_path / "serial").write_text("03\n")
            config = tmp_path / "ca.cnf"
            config.write_text(
                f"[ca]\ndefault_ca=authority\n[authority]\ndatabase={tmp_path}/index\n"
                f"serial={tmp_path}/serial\nnew_certs_dir={tmp_path}/new\n"
                f"certificate={ca}\nprivate_key={ca_key}\ndefault_md=sha256\n"
                "policy=subject\n[subject]\ncommonName=supplied\n"
            )
            ext.write_text("[leaf]\n" + ext.read_text())
            openssl(
                "ca",
                "-batch",
                "-notext",
                "-config",
                str(config),
                "-extfile",
                str(ext),
                "-extensions",
                "leaf",
                "-in",
                str(request),
                "-out",
                str(cert),
                "-startdate",
                "000101000000Z",
                "-enddate",
                "000102000000Z",
            )
        else:
            openssl(
                "x509",
                "-req",
                "-in",
                str(request),
                "-CA",
                str(ca),
                "-CAkey",
                str(ca_key),
                "-set_serial",
                "2",
                "-days",
                days,
                "-extfile",
                str(ext),
                "-out",
                str(cert),
            )
        result[name] = cert
        result[name + "_key"] = key
    return result


@contextmanager
def endpoint(
    material: dict[str, Path],
    response: bytes = b'{"status":"ready"}',
    *,
    code: int = 200,
    expired: bool = False,
    pause: float = 0,
) -> Iterator[tuple[int, list[bytes]]]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    stem = "expired" if expired else "server"
    context.load_cert_chain(material[stem], material[stem + "_key"])
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    received: list[bytes] = []

    def serve() -> None:
        try:
            with listener.accept()[0] as peer:
                peer.settimeout(1)
                with context.wrap_socket(peer, server_side=True) as channel:
                    received.append(channel.recv(4096))
                    time.sleep(pause)
                    channel.sendall(
                        (
                            f"HTTP/1.1 {code} Response\r\n"
                            f"Content-Length: {len(response)}\r\n"
                            "Location: https://never-follow.test/\r\n"
                            "Connection: close\r\n\r\n"
                        ).encode()
                        + response
                    )
        except (OSError, ssl.SSLError):
            pass  # Negative cases deliberately fail the handshake/response.

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
    return Settings(
        "https://shell.test",
        "https://idp.test",
        "aelev",
        "owner",
        "shell",
        "c403e937-ac5f-47c3-bba1-2f18c9407d04",
        30,
        1,
        tmp_path,
        tmp_path / "status.sqlite",
    )


def network(monkeypatch: pytest.MonkeyPatch, port: int, ca: Path) -> None:
    monkeypatch.setenv(PREFIX + "UI_PROBE_PROFILE", "network")
    monkeypatch.setenv(owner.NETWORK_PREFIX + "URL", f"https://127.0.0.1:{port}/readyz")
    monkeypatch.setenv(owner.NETWORK_PREFIX + "SERVER_NAME", "probe.test")
    monkeypatch.setenv(owner.NETWORK_PREFIX + "CA_FILE", str(ca))


def test_explicit_socket_and_separate_sni_are_actually_authenticated(
    material: dict[str, Path], cfg: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with endpoint(material) as (port, received):
        network(monkeypatch, port, material["ca"])
        assert owner.gateway_state(cfg) == "ONLINE"
    assert len(received) == 1
    assert received[0].startswith(b"GET /readyz HTTP/1.1\r\n")


@pytest.mark.parametrize(
    "kind",
    [
        "wrong-name",
        "wrong-ca",
        "expired",
        "timeout",
        "redirect",
        "wrong-body",
        "oversized",
    ],
)
def test_real_tls_and_response_failures_never_report_online(
    kind: str,
    material: dict[str, Path],
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body = (
        b"x" * 1024
        if kind == "oversized"
        else b'{"status":"wrong"}' if kind == "wrong-body" else b'{"status":"ready"}'
    )
    with endpoint(
        material,
        body,
        code=302 if kind == "redirect" else 200,
        expired=kind == "expired",
        pause=1.2 if kind == "timeout" else 0,
    ) as (port, received):
        network(monkeypatch, port, material["ca"])
        if kind == "wrong-name":
            monkeypatch.setenv(owner.NETWORK_PREFIX + "SERVER_NAME", "foreign.test")
        if kind == "wrong-ca":
            foreign = tmp_path / "foreign.crt"
            openssl(
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=foreign",
                "-keyout",
                str(tmp_path / "foreign.key"),
                "-out",
                str(foreign),
            )
            monkeypatch.setenv(owner.NETWORK_PREFIX + "CA_FILE", str(foreign))
        state, _ = owner.observe_provider(cfg)
        assert state != "ONLINE"
    assert len(received) <= 1


@pytest.mark.parametrize(
    "value",
    [
        "http://node.test:443/readyz",
        "https://user:secret@node.test/readyz",
        "https://node.test:0/readyz",
        "https://node.test:65536/readyz",
        "https://node.test:/readyz",
        "https://node.test/readyz?",
        "https://node.test/readyz#",
        "https://node.test/fetch",
        "https://node.test/readyz\r\n",
        "https://node%2etest/readyz",
        "https://node.test\\x/readyz",
    ],
)
def test_invalid_inputs_never_enter_probe_or_legacy(
    value: str, monkeypatch: pytest.MonkeyPatch, cfg: Settings
) -> None:
    network(monkeypatch, 18497, Path("/public/ca.crt"))
    monkeypatch.setenv(owner.NETWORK_PREFIX + "URL", value)
    probe = Mock(return_value="ONLINE")
    monkeypatch.setattr(owner, "probe", probe)
    assert owner.observe_provider(cfg)[0] != "ONLINE"
    probe.assert_not_called()


@pytest.mark.parametrize("missing", ["profile", *owner.NETWORK_SUFFIXES])
def test_partial_network_configuration_never_falls_back(
    missing: str, monkeypatch: pytest.MonkeyPatch, cfg: Settings
) -> None:
    network(monkeypatch, 18497, Path("/public/ca.crt"))
    monkeypatch.delenv(
        PREFIX + "UI_PROBE_PROFILE"
        if missing == "profile"
        else owner.NETWORK_PREFIX + missing
    )
    probe = Mock(return_value="ONLINE")
    monkeypatch.setattr(owner, "probe", probe)
    assert owner.observe_provider(cfg)[0] != "ONLINE"
    probe.assert_not_called()


def test_absent_profile_preserves_legacy_probe(
    monkeypatch: pytest.MonkeyPatch, cfg: Settings
) -> None:
    monkeypatch.setenv(PREFIX + "SERVER_NAME", "legacy.test")
    probe = Mock(return_value="ONLINE")
    monkeypatch.setattr(owner, "probe", probe)
    assert owner.gateway_state(cfg) == "ONLINE"
    probe.assert_called_once_with(
        "public-egress-gateway",
        8080,
        "legacy.test",
        "/run/secrets/gateway/server-ca.pem",
        cfg.timeout,
    )


def compiled(
    network_profile: bool, missing: str | None = None
) -> subprocess.CompletedProcess[str]:
    values = {
        "URL": "https://192.0.2.10:18497/readyz",
        "SERVER_NAME": "gateway.owner.test",
        "CA_FILE": "/run/secrets/gateway/server-ca.pem",
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
        "compose.yml",
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


def test_merged_profile_changes_only_one_publishers_probe_environment() -> None:
    before, after = compiled(False), compiled(True)
    assert before.returncode == after.returncode == 0, before.stderr + after.stderr
    base, network_model = json.loads(before.stdout), json.loads(after.stdout)
    env = network_model["services"]["public-egress-gateway-status-publisher"][
        "environment"
    ]
    assert env.pop(PREFIX + "UI_PROBE_PROFILE") == "network"
    for suffix in owner.NETWORK_SUFFIXES:
        assert env.pop(owner.NETWORK_PREFIX + suffix)
    assert base == network_model


@pytest.mark.parametrize("missing", owner.NETWORK_SUFFIXES)
def test_selected_profile_requires_all_explicit_model_inputs(missing: str) -> None:
    assert compiled(True, missing).returncode != 0
