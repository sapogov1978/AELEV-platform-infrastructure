from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from publisher_authority import prepare, stage


def fixture_authority(root: Path) -> bytes:
    root.mkdir()
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "publisher")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=40))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, None, None),
            True,
        )
        .sign(key, hashes.SHA256())
    )
    raw = certificate.public_bytes(serialization.Encoding.PEM)
    (root / "publisher.crt").write_bytes(raw)
    (root / "publisher.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (root / "publisher.key").chmod(0o600)
    return raw


def test_rotation_is_explicit_and_keeps_overlap(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    old = fixture_authority(source)
    trust = tmp_path / "publisher-ca.crt"
    trust.write_bytes(old)

    receipt = prepare(source, output)
    assert receipt["automatic_activation"] is False
    staged = stage(source, output, trust)
    assert staged["changed"] is True
    assert len(x509.load_pem_x509_certificates(trust.read_bytes())) == 2
    assert stage(source, output, trust)["changed"] is False


def test_transport_authority_uses_its_own_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    old = fixture_authority(source)
    (source / "publisher.crt").rename(source / "transport.crt")
    (source / "publisher.key").rename(source / "transport.key")
    trust = tmp_path / "transport-ca.crt"
    trust.write_bytes(old)

    receipt = prepare(source, output, authority="transport")
    assert receipt["authority"] == "transport"
    staged = stage(source, output, trust, authority="transport")
    assert staged["authority"] == "transport"
    assert len(x509.load_pem_x509_certificates(trust.read_bytes())) == 2
