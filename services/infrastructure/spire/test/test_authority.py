"""Production must consume a valid external intermediate, never create a root."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from aelev_spire import settings
from aelev_spire.files import atomic
from aelev_spire.settings import verify_authority


def test_external_intermediate_is_checked_without_copying_root_key(
    tmp_path: Path,
) -> None:
    issuer = ec.generate_private_key(ec.SECP256R1())
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fixture root")])
    intermediate_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Fixture issuer")]
    )
    now = datetime.now(UTC)

    def certificate(
        subject: x509.Name, key: ec.EllipticCurvePrivateKey
    ) -> x509.Certificate:
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(root_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=10))
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
            .sign(root_key, hashes.SHA256())
        )

    root = certificate(root_name, root_key)
    cert = certificate(intermediate_name, issuer)
    atomic(tmp_path / "issuer.crt", cert.public_bytes(serialization.Encoding.PEM))
    atomic(tmp_path / "bundle.crt", root.public_bytes(serialization.Encoding.PEM))
    atomic(
        tmp_path / "issuer.key",
        issuer.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    verify_authority(tmp_path, production=True)
    assert set(path.name for path in tmp_path.iterdir()) == {
        "issuer.crt",
        "issuer.key",
        "bundle.crt",
    }
    atomic(tmp_path / "issuer.crt", root.public_bytes(serialization.Encoding.PEM))
    with pytest.raises(ValueError, match="PRODUCTION_ROOT_KEY_FORBIDDEN"):
        verify_authority(tmp_path, production=True)


def test_node_ttl_is_explicit_and_does_not_extend_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values: dict[str, bytes | dict[str, Any]] = {}

    def save(path: Path, value: bytes | dict[str, Any], mode: int = 0o600) -> None:
        values[str(path)] = value

    monkeypatch.setattr(settings, "atomic", save)
    plan = {
        "environment": "test",
        "trust_domain": "test.example",
        "svid_ttl_seconds": 20,
    }
    settings.server(plan, tmp_path / "default", tmp_path / "bootstrap")
    assert b'agent_ttl = "20s"' in values["/run/spire/server.conf"]
    settings.server(
        {**plan, "agent_ttl_seconds": 3600},
        tmp_path / "explicit",
        tmp_path / "bootstrap",
    )
    assert b'agent_ttl = "3600s"' in values["/run/spire/server.conf"]
    assert b'default_x509_svid_ttl = "20s"' in values["/run/spire/server.conf"]
    with pytest.raises(ValueError, match="AGENT_TTL_INVALID"):
        settings.server(
            {**plan, "agent_ttl_seconds": 7200},
            tmp_path / "rejected",
            tmp_path / "bootstrap",
        )
