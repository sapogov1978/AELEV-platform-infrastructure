import os
import ssl
from hashlib import sha256
from pathlib import Path

import pytest

from prepare import PREFIX, development_pki, env

ROOT = Path(__file__).resolve().parents[1]


def handshake(server: ssl.SSLContext, client: ssl.SSLContext) -> None:
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    endpoints = (
        server.wrap_bio(server_in, server_out, server_side=True),
        client.wrap_bio(client_in, client_out, server_hostname="127.0.0.1"),
    )
    complete: set[int] = set()
    for _ in range(20):
        for index, endpoint in enumerate(endpoints):
            if index not in complete:
                try:
                    endpoint.do_handshake()
                    complete.add(index)
                except ssl.SSLWantReadError:
                    pass
        client_in.write(server_out.read())
        server_in.write(client_out.read())
        if len(complete) == 2:
            assert all(endpoint.getpeercert() for endpoint in endpoints)
            return
    pytest.fail("mutual TLS handshake did not complete")


def test_generated_identities_support_strict_mutual_tls_and_preserve_existing(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".env"
    data = env(ROOT / ".env.example")
    secret = tmp_path / "secrets"
    data[PREFIX + "SECRET_ROOT"] = str(secret)
    data[PREFIX + "SECRET_GID"] = str(os.getgid())
    config.write_text("\n".join(f"{key}={value}" for key, value in data.items()))
    identity_ca = tmp_path / "identity-ca.crt"
    identity_ca.write_text("external identity trust remains opaque\n")
    password = tmp_path / "password"
    password.write_text("local test operator password\n")
    development_pki(config, identity_ca, password)

    for server_role in ("nginx", "api"):
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.verify_mode = ssl.CERT_REQUIRED
        server.verify_flags |= ssl.VERIFY_X509_STRICT
        server.load_cert_chain(
            secret / server_role / "server.crt", secret / server_role / "server.key"
        )
        server.load_verify_locations(secret / server_role / "ca.crt")
        for client_role, name in (("nginx", "client"), ("api", "health")):
            client = ssl.create_default_context(cafile=secret / "api/ca.crt")
            client.verify_flags |= ssl.VERIFY_X509_STRICT
            client.load_cert_chain(
                secret / client_role / f"{name}.crt",
                secret / client_role / f"{name}.key",
            )
            handshake(server, client)

    before = {
        path: sha256(path.read_bytes()).hexdigest()
        for path in secret.rglob("*")
        if path.is_file()
    }
    with pytest.raises(ValueError, match="refusing to overwrite an existing identity"):
        development_pki(config, identity_ca, password)
    assert before == {path: sha256(path.read_bytes()).hexdigest() for path in before}
