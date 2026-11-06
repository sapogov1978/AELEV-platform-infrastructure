from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from aelev_spire.authority_rotation import prepare, stage_trust
from aelev_spire.files import atomic


def authority(name: str) -> tuple[bytes, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
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
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            True,
        )
        .sign(key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def test_explicit_rotation_stages_overlap_without_copying_keys(tmp_path: Path) -> None:
    source = tmp_path / "source"
    candidate = tmp_path / "candidate"
    observation = tmp_path / "observation"
    source.mkdir()
    old: dict[str, bytes] = {}
    for name in ("query", "server", "write"):
        certificate, key = authority(name)
        old[name] = certificate
        atomic(source / f"{name}.crt", certificate)
        atomic(source / f"{name}.key", key)
    for role in ("observer", "sender", "status"):
        (observation / role).mkdir(parents=True)
    atomic(observation / "observer/query-ca.crt", old["query"])
    atomic(observation / "observer/sender-ca.crt", old["server"])
    atomic(observation / "sender/server-ca.crt", old["server"])
    atomic(observation / "sender/local-write-ca.crt", old["write"])
    atomic(
        observation / "sender/local-query-ca.crt",
        old["query"] + old["server"],
    )
    atomic(observation / "status/query-ca.crt", old["server"])

    prepared = prepare(source, candidate)
    assert prepared["automatic_activation"] is False
    staged = stage_trust(source, candidate, observation)
    assert staged["code"] == "SPIRE_AUTHORITY_ROTATION_TRUST_STAGED"
    assert len(staged["changed"]) == 6
    assert (
        len(
            x509.load_pem_x509_certificates(
                (observation / "sender/local-query-ca.crt").read_bytes()
            )
        )
        == 4
    )
    assert not list(observation.rglob("*.key"))

    repeated = stage_trust(source, candidate, observation)
    assert repeated["changed"] == []
