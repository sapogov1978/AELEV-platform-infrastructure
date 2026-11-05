"""Synthetic certificates only; real SPIRE integration is a separate test."""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@pytest.fixture
def credential() -> tuple[bytes, bytes, bytes]:
    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    authority = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Isolated fixture")])
    root = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(authority.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=5))
        .add_extension(x509.BasicConstraints(ca=True, path_length=2), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(authority.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .sign(authority, hashes.SHA256())
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Isolated workload")])
        )
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(seconds=5))
        .not_valid_after(now + timedelta(minutes=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(authority.public_key()),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        "spiffe://test.example/fixture-owner/component/instance"
                    ),
                    x509.DNSName("localhost"),
                    x509.DNSName("spire-observer"),
                    x509.DNSName("spire-telemetry-sender"),
                    x509.DNSName("central.example"),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            critical=False,
        )
        .sign(authority, hashes.SHA256())
    )
    return (
        leaf.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        root.public_bytes(serialization.Encoding.PEM),
    )
