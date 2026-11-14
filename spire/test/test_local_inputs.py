"""Verify real PKI isolation, owner layout and preservation of initial material."""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from test_observation import plan

from aelev_spire.certificate_delivery import PROFILES
from aelev_spire.observation import PREFIX, select_source
from aelev_spire.public_state import RemoteSource


@pytest.fixture
def local() -> Any:
    spec = importlib.util.spec_from_file_location(
        "spire_local_inputs", Path(__file__).resolve().parents[1] / "local-inputs.py"
    )
    assert spec and spec.loader
    result: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def store(path: Path, value: bytes) -> None:
    path.write_bytes(value)
    path.chmod(0o600)


def issued(root: Path, name: str) -> dict[str, str]:
    """A separate issuer operator supplies a new owner leaf, never its CA key."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name + " issuer")])
    now = datetime.now(UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False
        )
        .sign(key, hashes.SHA256())
    )
    private = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "spire " + name)])
        )
        .issuer_name(subject)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=90))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, False, False, False, False),
            True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(private.public_key()), False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False
        )
        .sign(key, hashes.SHA256())
    )
    for basename, value in ((name + "-issuer", key), (name, private)):
        store(
            root / (basename + ".key"),
            value.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
    store(root / (name + "-issuer.crt"), ca.public_bytes(serialization.Encoding.PEM))
    store(root / (name + ".crt"), cert.public_bytes(serialization.Encoding.PEM))
    return {
        "certificate": str(root / (name + ".crt")),
        "key": str(root / (name + ".key")),
        "issuer_certificate": str(root / (name + "-issuer.crt")),
    }


@pytest.fixture
def configuration(tmp_path: Path) -> dict[str, Any]:
    root = tmp_path.resolve() / "issuer-operators"
    root.mkdir(mode=0o700)
    credentials = {
        name: issued(root, name) for name in ("central-write", "status-publisher")
    }
    for name, value in (
        ("admin-user", b"admin\n"),
        ("admin-password", b"unit-only-password\n"),
        ("publisher.token", b"unit-opaque-publisher\n"),
        ("provisioner.token", b"unit-opaque-provisioner\n"),
    ):
        store(root / name, value)
    return {
        "schema_version": 1,
        "environment": "test",
        "server_plan": {
            "owner": "infrastructure-spire",
            "schema_version": 1,
            "environment": "test",
            "trust_domain": "test.example",
            "svid_ttl_seconds": 300,
        },
        "observation_plan": plan(),
        "issued": credentials,
        "trust": {
            key: credentials["central-write"]["issuer_certificate"]
            for key in ("central", "shell", "identity", "grafana")
        },
        "status_admin": {
            "user": str(root / "admin-user"),
            "password": str(root / "admin-password"),
        },
        "grafana_tokens": {
            key: str(root / (key + ".token")) for key in ("publisher", "provisioner")
        },
    }


def test_real_private_bundle_matches_eight_delivery_identities(
    local: Any, configuration: dict[str, Any], tmp_path: Path
) -> None:
    output = tmp_path.resolve() / "prepared"
    receipt = local.prepare(configuration, output)
    initial = Path(receipt["initial_input"])
    assert receipt["status"] == "LOCAL_INITIAL_BUNDLE_PREPARED"
    assert receipt["external_issuer_keys_delivered"] is False
    assert receipt["renewal_enrolled"] is False
    assert receipt["runtime_installed"] is False
    assert {p.name for p in (output / "authorities").glob("*.key")} == {
        "query.key",
        "write.key",
        "server.key",
    }
    for path in output.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    requests = json.loads(Path(receipt["renewal_requests"]).read_text())
    assert requests["profiles"].keys() == local.PROFILES.keys()
    key_hashes: set[bytes] = set()
    for name, rows in requests["profiles"].items():
        assert {row["id"] for row in rows} == set(PROFILES[name])
        for row in rows:
            expected = PROFILES[name][row["id"]]
            assert (row["volume_role"], row["file"], Path(row["key"]).name) == expected
            assert "issuer_key" not in row
            key = serialization.load_pem_private_key(
                Path(row["key"]).read_bytes(), None
            )
            key_hashes.add(
                key.public_key().public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )
            leaf = x509.load_pem_x509_certificate(Path(row["certificate"]).read_bytes())
            leaf.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
            leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
            assert (
                timedelta(days=89)
                < leaf.not_valid_after_utc - datetime.now(UTC)
                < timedelta(days=91)
            )
    assert len(key_hashes) == 8
    subprocess.run(
        [
            "openssl",
            "verify",
            "-x509_strict",
            "-purpose",
            "sslserver",
            "-verify_hostname",
            "spire-observer",
            "-CAfile",
            str(initial / "sender/server-ca.crt"),
            str(initial / "observer/metrics.crt"),
        ],
        check=True,
        capture_output=True,
    )
    # Query credentials and server trust never authorize telemetry publication.
    for ca, leaf in (
        (initial / "observer/query-ca.crt", initial / "observer/observer.crt"),
        (initial / "sender/server-ca.crt", initial / "observer/metrics.crt"),
    ):
        result = subprocess.run(
            [
                "openssl",
                "verify",
                "-x509_strict",
                "-purpose",
                "sslclient",
                "-CAfile",
                str(ca),
                str(leaf),
            ],
            check=False,
            capture_output=True,
        )
        assert result.returncode != 0


def test_repeat_preserves_material_and_rejects_reconfigured_installation(
    local: Any, configuration: dict[str, Any], tmp_path: Path
) -> None:
    output = tmp_path.resolve() / "prepared"
    local.prepare(configuration, output)
    before = {
        str(p): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
        for p in output.rglob("*")
        if p.is_file()
    }
    local.prepare(configuration, output)
    assert before == {
        str(p): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
        for p in output.rglob("*")
        if p.is_file()
    }
    configuration["server_plan"]["trust_domain"] = "changed.example"
    with pytest.raises(local.InputError, match="INPUT_CHANGED"):
        local.prepare(configuration, output)


def network_configuration(configuration: dict[str, Any]) -> dict[str, Any]:
    configuration["server_plan"]["public_source"] = {
        "bind_address": "127.0.0.1",
        "public_port": 18084,
        "identity": "cn:AELEV SPIRE public-source",
        "client_identities": ["cn:AELEV SPIRE observer", "cn:AELEV SPIRE status-query"],
    }
    configuration["observation_plan"]["network"] = {
        "source_url": "https://identity-node.example:18084",
        "source_identity": "cn:AELEV SPIRE public-source",
        "observer_url": "https://observer-node.example:19464",
        "sender_otlp_url": "https://sender-node.example:14318",
        "sender_health_url": "https://sender-node.example:13134",
    }
    return configuration


def test_network_bundle_issues_source_and_actual_endpoint_sans(
    local: Any,
    configuration: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network_configuration(configuration)
    receipt = local.prepare(configuration, tmp_path.resolve() / "network")
    requests = json.loads(Path(receipt["renewal_requests"]).read_text())
    assert len(requests["profiles"]) == 5
    assert sum(len(items) for items in requests["profiles"].values()) == 9
    source = requests["profiles"]["public-source.json"][0]
    assert (source["volume_role"], source["file"]) == (
        "public-source-secrets",
        "source.crt",
    )
    initial = Path(receipt["initial_input"])
    for name, value in {
        "SOURCE_MODE": "https",
        "SOURCE_URL": configuration["observation_plan"]["network"]["source_url"],
        "SOURCE_IDENTITY": configuration["observation_plan"]["network"][
            "source_identity"
        ],
    }.items():
        monkeypatch.setenv(PREFIX + name, value)
    selected = select_source(
        "publish", tmp_path / "no-shared-source", initial / "status"
    )
    assert isinstance(selected, RemoteSource)
    selected.identity.current()
    for relative, hostname in (
        ("public-source/source.crt", "identity-node.example"),
        ("observer/metrics.crt", "observer-node.example"),
        ("sender/sender.crt", "sender-node.example"),
    ):
        subprocess.run(
            [
                "openssl",
                "verify",
                "-x509_strict",
                "-purpose",
                "sslserver",
                "-verify_hostname",
                hostname,
                "-CAfile",
                str(initial / "sender/server-ca.crt"),
                str(initial / relative),
            ],
            check=True,
            capture_output=True,
        )
    assert (initial / "public-source/source-client-ca.crt").read_bytes() == (
        (initial / "sender/local-write-ca.crt").read_bytes()
        + (initial / "observer/query-ca.crt").read_bytes()
    )


def test_external_issuer_keys_cannot_enter_the_input_contract(
    local: Any, configuration: dict[str, Any], tmp_path: Path
) -> None:
    configuration["issued"]["status-publisher"]["issuer_key"] = "not-an-owner-input"
    output = tmp_path.resolve() / "prepared"
    with pytest.raises(local.InputError, match="ISSUED_IDENTITIES_REQUIRED"):
        local.prepare(configuration, output)
    assert not output.exists()


def test_wrong_issuer_is_rejected_before_new_key_creation(
    local: Any, configuration: dict[str, Any], tmp_path: Path
) -> None:
    configuration["issued"]["central-write"]["issuer_certificate"] = configuration[
        "issued"
    ]["status-publisher"]["issuer_certificate"]
    output = tmp_path.resolve() / "prepared"
    with pytest.raises(local.InputError, match="PKI_VALIDATION_FAILED"):
        local.prepare(configuration, output)
    assert not output.exists()


def test_external_ca_itself_cannot_be_delivered_as_a_client_identity(
    local: Any, configuration: dict[str, Any], tmp_path: Path
) -> None:
    value = configuration["issued"]["central-write"]
    value["certificate"] = value["issuer_certificate"]
    value["key"] = str(Path(value["issuer_certificate"]).with_suffix(".key"))
    output = tmp_path.resolve() / "prepared"
    with pytest.raises(local.InputError, match="LEAF_REQUIRED"):
        local.prepare(configuration, output)
    assert not output.exists()


def test_a_leaf_cannot_smuggle_its_issuer_private_key_into_runtime(
    local: Any, configuration: dict[str, Any], tmp_path: Path
) -> None:
    value = configuration["issued"]["central-write"]
    ca = x509.load_pem_x509_certificate(Path(value["issuer_certificate"]).read_bytes())
    issuer_key_path = Path(value["issuer_certificate"]).with_suffix(".key")
    issuer_key = serialization.load_pem_private_key(issuer_key_path.read_bytes(), None)
    assert isinstance(issuer_key, ec.EllipticCurvePrivateKey)
    now = datetime.now(UTC)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wrong owner key")])
        )
        .issuer_name(ca.subject)
        .public_key(ca.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=90))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca.public_key()), False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            False,
        )
        .sign(issuer_key, hashes.SHA256())
    )
    certificate = tmp_path.resolve() / "issuer-key-as-leaf.crt"
    store(certificate, leaf.public_bytes(serialization.Encoding.PEM))
    value.update(certificate=str(certificate), key=str(issuer_key_path))
    output = tmp_path.resolve() / "prepared"
    with pytest.raises(local.InputError, match="LEAF_REUSES_ISSUER_KEY"):
        local.prepare(configuration, output)
    assert not output.exists()


def test_issued_key_mismatch_is_rejected_before_preparation(
    local: Any, configuration: dict[str, Any], tmp_path: Path
) -> None:
    configuration["issued"]["central-write"]["key"] = configuration["issued"][
        "status-publisher"
    ]["key"]
    output = tmp_path.resolve() / "prepared"
    with pytest.raises(local.InputError, match="LEAF_KEY_MISMATCH"):
        local.prepare(configuration, output)
    assert not output.exists()


def test_issuer_scopes_cannot_share_one_delivered_identity(
    local: Any, configuration: dict[str, Any], tmp_path: Path
) -> None:
    configuration["issued"]["status-publisher"] = configuration["issued"][
        "central-write"
    ].copy()
    output = tmp_path.resolve() / "prepared"
    with pytest.raises(local.InputError, match="AUTHORITY_OR_KEY_REUSED"):
        local.prepare(configuration, output)
    assert not output.exists()


def test_incomplete_operation_never_issues_new_keys_on_repeat(
    local: Any, configuration: dict[str, Any], tmp_path: Path
) -> None:
    output = tmp_path.resolve() / "partial"
    output.mkdir(mode=0o700)
    store(output / "receipt.json", b'{"status":"PREPARING"}')
    with pytest.raises(local.InputError, match="INSPECTION_REQUIRED"):
        local.prepare(configuration, output)
    assert list(output.iterdir()) == [output / "receipt.json"]


def test_production_cannot_create_local_authority(
    local: Any, configuration: dict[str, Any], tmp_path: Path
) -> None:
    configuration["environment"] = "production"
    output = tmp_path.resolve() / "prepared"
    with pytest.raises(local.InputError, match="CONFIG_INVALID"):
        local.prepare(configuration, output)
    assert not output.exists()


def test_tampered_prepared_key_is_never_repaired_implicitly(
    local: Any, configuration: dict[str, Any], tmp_path: Path
) -> None:
    output = tmp_path.resolve() / "prepared"
    local.prepare(configuration, output)
    target = output / "initial-input/sender/local-write.key"
    store(target, b"changed-key")
    with pytest.raises(local.InputError, match="MATERIAL_CHANGED"):
        local.prepare(configuration, output)
    assert target.read_bytes() == b"changed-key"
