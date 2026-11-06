import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import HTTPException

from api import OIDC, Settings


@pytest.fixture
def identity(tmp_path: Path) -> tuple[OIDC, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "unit-generated-ca")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(seconds=5))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    ca = tmp_path / "ca.crt"
    ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cfg = Settings(
        "https://shell.invalid",
        "https://identity.invalid/realms/r",
        "https://identity.invalid/certs",
        "shell",
        str(ca),
        str(tmp_path / "unused.sqlite"),
        120,
        300,
        10,
        65536,
    )
    auth = OIDC(cfg)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "unit-key"
    auth.keys = {"unit-key": jwt.PyJWK(jwk, algorithm="RS256")}
    auth.expires = time.monotonic() + 300
    return auth, key


def claims() -> dict[str, Any]:
    return {
        "sub": "unit-generated-owner",
        "iss": "https://identity.invalid/realms/r",
        "aud": "shell",
        "iat": int(time.time()),
        "exp": int(time.time()) + 120,
        "typ": "Bearer",
    }


def token(key: rsa.RSAPrivateKey, value: dict[str, Any]) -> str:
    return jwt.encode(value, key, algorithm="RS256", headers={"kid": "unit-key"})


def test_signed_access_token(identity: tuple[OIDC, rsa.RSAPrivateKey]) -> None:
    auth, key = identity
    assert (
        asyncio.run(auth.verify(token(key, claims())))["sub"] == "unit-generated-owner"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("iss", "https://other.invalid"),
        ("aud", "other"),
        ("exp", 1),
        ("iat", 9999999999),
        ("typ", "ID"),
        ("sub", ""),
    ],
)
def test_wrong_claims(
    identity: tuple[OIDC, rsa.RSAPrivateKey], field: str, value: Any
) -> None:
    auth, key = identity
    data = claims()
    data[field] = value
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.verify(token(key, data)))
    assert error.value.status_code == 401


def test_wrong_signature(identity: tuple[OIDC, rsa.RSAPrivateKey]) -> None:
    auth, _ = identity
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.verify(token(other, claims())))
    assert error.value.status_code == 401


def test_expired_trust_fails_closed(identity: tuple[OIDC, rsa.RSAPrivateKey]) -> None:
    auth, key = identity
    auth.expires = time.monotonic() - 1
    auth.refresh_not_before = time.monotonic() + 5
    with pytest.raises(HTTPException) as error:
        asyncio.run(auth.verify(token(key, claims())))
    assert error.value.status_code == 503
