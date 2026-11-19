"""Real TLS transport tests; JWT logic uses an explicit unit authenticator."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient

from api import Settings, create_app, main
from tls_peer import TLSH11Protocol
from transport_settings import spiffe_id

PROXY = "spiffe://qualification.test/platform-ui/nginx"


class UnitIdentity:
    async def verify(self, token: str) -> dict[str, Any]:
        return {
            "sub": "unit-owner",
            "realm_access": {"roles": ["platform-ui-viewer", "platform-ui-publisher"]},
        }


@pytest.fixture(scope="module")
def identities(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("ui-tls-peer")
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Unit transport CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(issuer)
        .issuer_name(issuer)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = root / "ca.crt"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    result: dict[str, Any] = {"root": root, "ca": ca_path}
    for name, uris in {
        "server": [],
        "proxy": [PROXY],
        "foreign": ["spiffe://qualification.test/other"],
        "probe": [],
        "ambiguous": [PROXY, "spiffe://qualification.test/other"],
        "untrusted": [PROXY],
    }.items():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        signing_key = ca_key
        signing_issuer = issuer
        if name == "untrusted":
            signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            signing_issuer = x509.Name(
                [x509.NameAttribute(NameOID.COMMON_NAME, "Foreign CA")]
            )
        names: list[x509.GeneralName] = [
            x509.UniformResourceIdentifier(uri) for uri in uris
        ]
        if name == "server":
            names = [
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(signing_issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    True, False, True, False, False, False, False, False, False
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage(
                    [
                        (
                            ExtendedKeyUsageOID.SERVER_AUTH
                            if name == "server"
                            else ExtendedKeyUsageOID.CLIENT_AUTH
                        )
                    ]
                ),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    signing_key.public_key()
                ),
                critical=False,
            )
        )
        if names:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(names), critical=False
            )
        cert = builder.sign(signing_key, hashes.SHA256())
        cert_path, key_path = root / (name + ".crt"), root / (name + ".key")
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        key_path.chmod(0o600)
        result[name] = (cert_path, key_path)
    return result


@pytest.fixture(scope="module")
def listener(identities: dict[str, Any]) -> Iterator[int]:
    root = identities["root"]
    settings = Settings(
        "https://shell.invalid",
        "https://identity.invalid/realms/r",
        "https://identity.invalid/certs",
        "shell",
        "unused-unit-ca",
        str(root / "metadata.sqlite"),
        120,
        300,
        10,
        65536,
        PROXY,
    )
    app = create_app(settings, UnitIdentity())
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        ssl_certfile=str(identities["server"][0]),
        ssl_keyfile=str(identities["server"][1]),
        ssl_ca_certs=str(identities["ca"]),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        http=TLSH11Protocol,
        ws="none",
        access_log=False,
        log_level="error",
    )
    server = uvicorn.Server(config)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("unit TLS server did not start")
            time.sleep(0.01)
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def connection(
    identities: dict[str, Any], port: int, name: str
) -> http.client.HTTPSConnection:
    context = ssl.create_default_context(cafile=str(identities["ca"]))
    context.load_cert_chain(*identities[name])
    return http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)


@pytest.mark.parametrize(
    "identity,expected",
    [("proxy", 200), ("foreign", 403), ("probe", 403), ("ambiguous", 403)],
)
def test_real_tls_identity_controls_forged_publisher_header(
    identities: dict[str, Any], listener: int, identity: str, expected: int
) -> None:
    module = str(uuid4())
    body = json.dumps(
        {
            "schema_version": 1,
            "module_id": module,
            "revision": 1,
            "title": {"ru": "Проверка транспорта", "en": "Transport test"},
            "sections": [],
        }
    )
    client = connection(identities, listener, identity)
    try:
        client.request(
            "PUT",
            "/api/modules/" + module,
            body,
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer owner",
                "X-Verified-Publisher": "SUCCESS",
            },
        )
        response = client.getresponse()
        response.read()
        assert response.status == expected
    finally:
        client.close()


def test_probe_can_read_readiness_but_cannot_read_metadata(
    identities: dict[str, Any], listener: int
) -> None:
    client = connection(identities, listener, "probe")
    try:
        client.request("GET", "/api/readyz")
        response = client.getresponse()
        assert response.status == 200 and json.loads(response.read()) == {
            "status": "ready"
        }
        client.request(
            "GET",
            "/api/modules",
            headers={
                "Authorization": "Bearer owner",
                "X-Verified-Publisher": "SUCCESS",
            },
        )
        response = client.getresponse()
        response.read()
        assert response.status == 403
    finally:
        client.close()


@pytest.mark.parametrize(
    "identity,expected", [("proxy", 200), ("probe", 403), ("foreign", 403)]
)
def test_proxy_readiness_requires_the_proxy_identity(
    identities: dict[str, Any], listener: int, identity: str, expected: int
) -> None:
    client = connection(identities, listener, identity)
    try:
        client.request("GET", "/api/proxy-readyz")
        response = client.getresponse()
        response.read()
        assert response.status == expected
    finally:
        client.close()


@pytest.mark.parametrize("identity", [None, "untrusted"])
def test_listener_requires_a_trusted_client_certificate(
    identities: dict[str, Any], listener: int, identity: str | None
) -> None:
    context = ssl.create_default_context(cafile=str(identities["ca"]))
    if identity is not None:
        context.load_cert_chain(*identities[identity])
    client = http.client.HTTPSConnection(
        "127.0.0.1", listener, context=context, timeout=5
    )
    try:
        with pytest.raises((ssl.SSLError, ConnectionError)):
            client.request("GET", "/api/readyz")
            client.getresponse()
    finally:
        client.close()


def test_connection_peer_identity_never_leaks_to_parallel_keepalive(
    identities: dict[str, Any], listener: int
) -> None:
    def requests(name: str) -> list[int]:
        client = connection(identities, listener, name)
        codes = []
        try:
            for _ in range(8):
                client.request(
                    "GET",
                    "/api/modules",
                    headers={
                        "Authorization": "Bearer owner",
                        "X-Verified-Publisher": "SUCCESS",
                    },
                )
                response = client.getresponse()
                response.read()
                codes.append(response.status)
            return codes
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        good = executor.submit(requests, "proxy")
        bad = executor.submit(requests, "foreign")
        assert good.result() == [200] * 8
        assert bad.result() == [403] * 8


def test_network_configuration_refuses_missing_proxy_before_server_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Settings(
        "https://shell.invalid",
        "https://identity.invalid/realms/r",
        "https://identity.invalid/certs",
        "shell",
        "unused",
        str(tmp_path / "untouched.sqlite"),
        120,
        300,
        10,
        65536,
    )
    data = asdict(cfg)
    data.pop("trusted_proxy_spiffe_id")
    path = tmp_path / "api.json"
    path.write_text(json.dumps(data))
    assert Settings.read(path) == cfg
    monkeypatch.setenv(
        "AELEV_INFRASTRUCTURE_PLATFORM_UI_TRANSPORT_PROFILE", "network-mtls"
    )
    monkeypatch.setattr("sys.argv", ["api.py", str(path)])
    with pytest.raises(ValueError, match="requires trusted proxy"):
        main()
    assert not Path(cfg.database).exists()
    guarded = replace(cfg, trusted_proxy_spiffe_id=PROXY)
    with TestClient(create_app(guarded, UnitIdentity())) as client:
        assert (
            client.get(
                "/api/modules",
                headers={
                    "Authorization": "Bearer owner",
                    "X-Verified-Publisher": "SUCCESS",
                },
            ).status_code
            == 403
        )


@pytest.mark.parametrize(
    "value",
    [
        "https://proxy.test/",
        "spiffe://UPPER/path",
        "spiffe://proxy.test/a/../b",
        "spiffe://proxy.test/a?",
        "spiffe://proxy.test/a#",
        "spiffe://user@proxy.test/a",
        "spiffe://proxy.test:443/a",
        "spiffe://proxy.test/a%2fb",
    ],
)
def test_proxy_identity_cannot_be_an_ambiguous_url(value: str) -> None:
    with pytest.raises(ValueError):
        spiffe_id(value)
