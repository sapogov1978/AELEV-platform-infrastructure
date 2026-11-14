"""Real TLS projection exchange, distinct role keys and explicit trust policy."""

import base64
import hashlib
import http.client
import json
import socket
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from aelev_spire.configuration import seed
from aelev_spire.installation import Installer
from aelev_spire.observation import (
    PREFIX,
    STATUS_PUBLISHER_IDENTITIES,
    STATUS_QUERY_IDENTITIES,
    TELEMETRY_IDENTITIES,
    component_renewal,
    metrics,
    renewal_snapshot,
    select_source,
    snapshot,
)
from aelev_spire.public_state import (
    PeerTLS,
    PublicServer,
    RemoteSource,
    heartbeat,
    renewal,
    source_renewal,
)
from aelev_spire.source_setup import configure, specification


def credentials(root: Path) -> None:
    root.mkdir(exist_ok=True)
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Projection fixture CA")]
    )
    ca = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=90))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False
        )
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    (root / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    for name in ("source", "observer", "foreign", "metrics", "query", "publisher"):
        key = ec.generate_private_key(ec.SECP256R1())
        leaf = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=60))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False
            )
            .add_extension(
                x509.KeyUsage(
                    True, False, False, False, False, False, False, False, False
                ),
                True,
            )
            .add_extension(
                x509.ExtendedKeyUsage(
                    [
                        ExtendedKeyUsageOID.SERVER_AUTH
                        if name in {"source", "metrics"}
                        else ExtendedKeyUsageOID.CLIENT_AUTH
                    ]
                ),
                False,
            )
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("localhost")]), False
            )
            .sign(ca_key, hashes.SHA256())
        )
        (root / (name + ".crt")).write_bytes(
            leaf.public_bytes(serialization.Encoding.PEM)
        )
        path = root / (name + ".key")
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        path.chmod(0o600)


def identity(root: Path, name: str = "observer", *, server: bool = False) -> PeerTLS:
    return PeerTLS(
        root,
        name + ".crt",
        name + ".key",
        "ca.crt",
        "cn:" + name,
        ("cn:observer" if server else "cn:source",),
        server=server,
    )


def projection() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "owner": "infrastructure-spire",
        "heartbeat": {
            "owner": "infrastructure-spire",
            "role": "server",
            "ready": True,
            "reason": "SPIRE_READY",
            "updated_at": datetime.now(UTC).isoformat(),
        },
        "renewal": None,
    }


@contextmanager
def serving(
    root: Path, value: dict[str, Any]
) -> Generator[tuple[PublicServer, RemoteSource]]:
    with PublicServer(
        ("127.0.0.1", 0), identity(root, "source", server=True), lambda: value
    ) as server:
        worker = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        worker.start()
        try:
            yield (
                server,
                RemoteSource(
                    "https://localhost:" + str(server.server_port), identity(root)
                ),
            )
        finally:
            server.shutdown()
            worker.join(timeout=3)


def test_remote_projection_needs_no_shared_file_and_keeps_source_time(
    tmp_path: Path,
) -> None:
    credentials(tmp_path)
    value = projection()
    with serving(tmp_path, value) as (_, source):
        assert snapshot(source) == value["heartbeat"]
        assert b"aelev_spire_source_fresh 1" in metrics(source)
        assert b"aelev_spire_certificate_renewal_ready 0" in metrics(source)
        value["heartbeat"]["ready"] = False
        assert snapshot(source)["ready"] is False
        value["heartbeat"]["updated_at"] = (
            datetime.now(UTC) - timedelta(seconds=16)
        ).isoformat()
        with pytest.raises(ValueError, match="SPIRE_OBSERVATION_STALE_OR_INVALID"):
            snapshot(source)
        assert b"aelev_spire_source_fresh 0" in metrics(source)


def test_valid_ca_does_not_authorize_another_client_identity(tmp_path: Path) -> None:
    credentials(tmp_path)
    with serving(tmp_path, projection()) as (server, _):
        foreign = RemoteSource(
            "https://localhost:" + str(server.server_port),
            identity(tmp_path, "foreign"),
        )
        with pytest.raises((OSError, ValueError, http.client.HTTPException)):
            foreign.read()


def test_source_identity_checked_before_get(tmp_path: Path) -> None:
    credentials(tmp_path)
    with serving(tmp_path, projection()) as (server, _):
        client = identity(tmp_path)
        client.peer_identities = ("cn:another-source",)
        with pytest.raises(ValueError, match="PEER_INVALID"):
            RemoteSource("https://localhost:" + str(server.server_port), client).read()


def test_explicit_https_never_falls_back_to_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps(projection()["heartbeat"]))
    monkeypatch.setenv(PREFIX + "SOURCE_MODE", "https")
    monkeypatch.delenv(PREFIX + "SOURCE_URL", raising=False)
    with pytest.raises(ValueError, match="CONFIGURATION_INVALID"):
        select_source("serve", path, tmp_path)
    monkeypatch.setenv(PREFIX + "SOURCE_MODE", "file")
    monkeypatch.setenv(PREFIX + "SOURCE_URL", "https://source.example")
    with pytest.raises(ValueError, match="CONFIGURATION_INVALID"):
        select_source("serve", path, tmp_path)


def test_rotation_rejects_session_on_removed_trust(tmp_path: Path) -> None:
    credentials(tmp_path)
    with serving(tmp_path, projection()) as (_, source):
        connection = http.client.HTTPSConnection(
            source.host, source.port, context=source.identity.current(), timeout=2
        )
        connection.connect()
        credentials(tmp_path)  # New CA and own keys, same role names.
        with pytest.raises((OSError, http.client.HTTPException)):
            connection.request("GET", "/v1/public-state")
            connection.getresponse().read()
        connection.close()
        assert snapshot(source)["ready"] is True


def test_slow_headers_cannot_hold_a_server_slot(tmp_path: Path) -> None:
    credentials(tmp_path)
    with serving(tmp_path, projection()) as (server, source):
        with (
            socket.create_connection((source.host, source.port), timeout=3) as raw,
            source.identity.current().wrap_socket(
                raw, server_hostname="localhost"
            ) as channel,
        ):
            channel.sendall(b"GET /v1/public-state HTTP/1.1\r\nHost: localhost\r\n")
            started = time.monotonic()
            assert channel.recv(1) == b""
            assert time.monotonic() - started < 2.8
        assert snapshot(source)["ready"] is True
        assert server.slots.acquire(timeout=1)
        server.slots.release()


def test_public_heartbeat_drops_unrelated_fields() -> None:
    value = {**projection()["heartbeat"], "pid": 123, "private_key": "NEVER EXPOSE"}
    assert set(heartbeat(value)) == {"owner", "role", "ready", "reason", "updated_at"}


@pytest.mark.parametrize("number", [float("nan"), float("inf"), time.time() + 3600])
def test_renewal_rejects_nonfinite_and_future_receipts(number: float) -> None:
    value: dict[str, Any] = {
        "owner": "telemetry-certificate-renewal",
        "consumer_owner": "infrastructure-spire",
        "schema_version": 1,
        "renewal_healthy": True,
        "ready": True,
        "checked_at": number,
        "last_success_at": time.time(),
        "certificates": [],
    }
    with pytest.raises(ValueError, match="RENEWAL_NOT_READY"):
        renewal(value, set())


def source_receipt(root: Path) -> dict[str, Any]:
    raw = (root / "source.crt").read_bytes()
    cert = x509.load_pem_x509_certificate(raw)
    ca = x509.load_pem_x509_certificate((root / "ca.crt").read_bytes())
    return {
        "owner": "telemetry-certificate-renewal",
        "consumer_owner": "infrastructure-spire",
        "schema_version": 1,
        "renewal_healthy": True,
        "ready": True,
        "checked_at": time.time(),
        "last_success_at": time.time(),
        "certificates": [
            {
                "id": "public-source",
                "healthy": True,
                "certificate_sha256": hashlib.sha256(raw).hexdigest(),
                "certificate_expires_at": cert.not_valid_after_utc.timestamp(),
                "ca_expires_at": ca.not_valid_after_utc.timestamp(),
            }
        ],
    }


@pytest.mark.parametrize("mutation", ["none", "hash", "expiry", "stale", "scope"])
def test_source_renewal_is_bound_to_actual_leaf_and_fresh_receipt(
    tmp_path: Path,
    mutation: str,
) -> None:
    credentials(tmp_path)
    receipt = source_receipt(tmp_path)
    item = receipt["certificates"][0]
    if mutation == "hash":
        item["certificate_sha256"] = "a" * 64
    elif mutation == "expiry":
        item["certificate_expires_at"] += 3600
    elif mutation == "stale":
        receipt["checked_at"] -= 181
    elif mutation == "scope":
        item["id"] = "sender"
    path = tmp_path / "public-source.json"
    path.write_text(json.dumps(receipt))
    source = identity(tmp_path, "source", server=True)
    if mutation == "none":
        assert source_renewal(path, source)["certificates"][0]["id"] == "public-source"
    else:
        with pytest.raises(ValueError, match="RENEWAL_NOT_READY"):
            source_renewal(path, source)


def test_remote_source_cannot_attest_other_roles_renewal(tmp_path: Path) -> None:
    credentials(tmp_path)
    receipt = source_receipt(tmp_path)
    template = receipt["certificates"][0]
    receipt["certificates"] = [
        {**template, "id": name}
        for name in sorted(
            TELEMETRY_IDENTITIES | STATUS_QUERY_IDENTITIES | STATUS_PUBLISHER_IDENTITIES
        )
    ]
    value = {**projection(), "renewal": receipt}
    with serving(tmp_path, value) as (_, source):
        with pytest.raises(ValueError, match="RENEWAL_NOT_READY"):
            renewal_snapshot(source)
        assert b"aelev_spire_certificate_renewal_ready 0" in metrics(source)
        receipt["certificates"] = [template]
        assert len(renewal_snapshot(source)["certificates"]) == 1
        # Source renewal alone never attests the observer's local credentials.
        assert b"aelev_spire_certificate_renewal_ready 0" in metrics(source)


@pytest.mark.parametrize("role", ["observer", "publisher"])
def test_component_renewal_uses_only_local_role_receipts(
    tmp_path: Path, role: str
) -> None:
    credentials(tmp_path)
    base = source_receipt(tmp_path)
    scope = {
        "observer": {"observer.json": {"metrics": "metrics", "observer": "observer"}},
        "publisher": {
            "status-query.json": {"status-query": "query"},
            "status-publisher.json": {"status-publisher": "publisher"},
        },
    }[role]
    for filename, leaves in scope.items():
        entries: list[dict[str, Any]] = []
        for name, stem in leaves.items():
            raw = (tmp_path / (stem + ".crt")).read_bytes()
            cert = x509.load_pem_x509_certificate(raw)
            entries.append(
                {
                    **base["certificates"][0],
                    "id": name,
                    "certificate_sha256": hashlib.sha256(raw).hexdigest(),
                    "certificate_expires_at": cert.not_valid_after_utc.timestamp(),
                }
            )
        (tmp_path / filename).write_text(json.dumps({**base, "certificates": entries}))
    value = {**projection(), "renewal": base}
    with serving(tmp_path, value) as (_, source):
        result = component_renewal(source, role, tmp_path, tmp_path / "status.json")
        assert len(result["certificates"]) == 3
        assert not (tmp_path / "sender.json").exists()
        assert not (tmp_path / "status.json").exists()
        first = tmp_path / next(iter(scope))
        first.unlink()
        with pytest.raises(OSError):
            component_renewal(source, role, tmp_path, tmp_path / "status.json")


def test_world_readable_private_key_rejected(tmp_path: Path) -> None:
    credentials(tmp_path)
    (tmp_path / "observer.key").chmod(0o644)
    with pytest.raises(ValueError, match="KEY_PERMISSIONS_INVALID"):
        identity(tmp_path).current()


def test_server_install_prepares_separate_secret_volume_without_changing_existing_key(
    tmp_path: Path,
) -> None:
    inputs = tmp_path / "inputs"
    credentials(inputs)
    (inputs / "source-client-ca.crt").write_bytes((inputs / "ca.crt").read_bytes())
    plan = {
        "public_source": {
            "bind_address": "127.0.0.1",
            "public_port": 18084,
            "identity": "cn:source",
            "client_identities": ["cn:observer"],
        }
    }
    installer = object.__new__(Installer)
    installer.image = "sha256:" + "a" * 64
    installer.project = "fixture-spire"

    def volume(name: str, *, ephemeral: bool = False) -> str:
        assert not ephemeral
        return "fixture-spire_" + name

    installer.volume = volume
    destination = tmp_path / "source-volume"

    def deliver(volume: str, role: str, files: dict[str, bytes]) -> None:
        assert volume == "fixture-spire_public-source-secrets"
        seed(
            destination,
            {
                "marker": {"owner": "infrastructure-spire", "role": role},
                "files": {
                    name: base64.b64encode(raw).decode() for name, raw in files.items()
                },
            },
        )

    installer.seed = deliver
    model: dict[str, Any] = {
        "services": {
            "spire-server": {
                "environment": {
                    "AELEV_INFRASTRUCTURE_SPIRE_OBSERVATION_ROOT": "/run/observation"
                },
                "volumes": [
                    {"target": "/run/observation"},
                    {"target": "/var/lib/spire"},
                ],
            }
        },
        "volumes": {"spire-observation-source": {}},
    }
    configure(installer, model, plan, inputs)
    original = (destination / "source.key").read_bytes()
    server = model["services"]["spire-server"]
    assert "AELEV_INFRASTRUCTURE_SPIRE_OBSERVATION_ROOT" not in server["environment"]
    assert "/run/observation" not in {item["target"] for item in server["volumes"]}
    assert all(
        item["read_only"]
        for item in server["volumes"]
        if item["target"].startswith("/run/")
    )
    assert server["ports"] == [
        {
            "target": 8084,
            "published": "18084",
            "host_ip": "127.0.0.1",
            "protocol": "tcp",
        }
    ]
    credentials(inputs)
    (inputs / "source-client-ca.crt").write_bytes((inputs / "ca.crt").read_bytes())
    with pytest.raises(ValueError, match="EXISTING_CONFIGURATION_PRESERVED"):
        configure(installer, model, plan, inputs)
    assert (destination / "source.key").read_bytes() == original


@pytest.mark.parametrize(
    "value", [None, "spiffe://", "cn:", "spiffe://host/../other", "cn:source\n"]
)
def test_server_plan_rejects_invalid_principal_before_install(value: object) -> None:
    plan = {
        "public_source": {
            "bind_address": "127.0.0.1",
            "public_port": 18084,
            "identity": value,
            "client_identities": ["cn:observer"],
        }
    }
    with pytest.raises(ValueError, match="IDENTITIES_REQUIRED"):
        specification(plan)
