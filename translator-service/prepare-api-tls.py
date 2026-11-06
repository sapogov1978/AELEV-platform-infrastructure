"""Generate dedicated local API PKI without touching existing identities."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path


def execute(arguments: list[str]) -> None:
    """Never print key material, password values or raw OpenSSL errors."""
    result = subprocess.run(["openssl", *arguments], check=False, capture_output=True)
    if result.returncode:
        raise RuntimeError("TRANSLATOR_API_LOCAL_PKI_FAILED")


def prepare(root: Path) -> None:
    """Create a local CA, server and distinct read-only operator certificate."""
    if shutil.which("openssl") is None:
        raise RuntimeError("OPENSSL_REQUIRED")
    if root.exists():
        raise FileExistsError("LOCAL_PKI_ROOT_MUST_NOT_EXIST")
    os.umask(0o077)
    root.mkdir(parents=True)
    ca = root / "api-ca"
    server = root / "api-tls"
    operator = root / "api-operator"
    for directory in (ca, server, operator):
        directory.mkdir()
    execute(
        [
            "req",
            "-x509",
            "-newkey",
            "rsa:3072",
            "-noenc",
            "-sha256",
            "-days",
            "365",
            "-subj",
            "/CN=DRP translator local API CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",
            str(ca / "ca.key"),
            "-out",
            str(ca / "ca.crt"),
        ]
    )
    for directory, name, usage, extension in (
        (
            server,
            "server",
            "serverAuth",
            "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1",
        ),
        (operator, "operator", "clientAuth", "basicConstraints=critical,CA:FALSE"),
    ):
        execute(
            [
                "req",
                "-new",
                "-newkey",
                "rsa:3072",
                "-noenc",
                "-sha256",
                "-subj",
                "/CN=DRP translator local " + name,
                "-addext",
                "extendedKeyUsage=" + usage,
                "-addext",
                extension,
                "-keyout",
                str(directory / (name + ".key")),
                "-out",
                str(directory / (name + ".csr")),
            ]
        )
        execute(
            [
                "x509",
                "-req",
                "-sha256",
                "-days",
                "30",
                "-copy_extensions",
                "copyall",
                "-CA",
                str(ca / "ca.crt"),
                "-CAkey",
                str(ca / "ca.key"),
                "-CAcreateserial",
                "-in",
                str(directory / (name + ".csr")),
                "-out",
                str(directory / (name + ".crt")),
            ]
        )
    shutil.copyfile(ca / "ca.crt", server / "clients-ca.crt")
    shutil.copyfile(ca / "ca.crt", operator / "api-ca.crt")
    password = operator / "p12-password"
    password.write_text(secrets.token_urlsafe(32) + "\n")
    execute(
        [
            "pkcs12",
            "-export",
            "-in",
            str(operator / "operator.crt"),
            "-inkey",
            str(operator / "operator.key"),
            "-certfile",
            str(operator / "api-ca.crt"),
            "-out",
            str(operator / "operator.p12"),
            "-passout",
            "file:" + str(password),
            "-name",
            "DRP translator read-only operator",
        ]
    )
    execute(
        [
            "verify",
            "-CAfile",
            str(ca / "ca.crt"),
            str(server / "server.crt"),
            str(operator / "operator.crt"),
        ]
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.output)
    print(
        json.dumps({"event": "translator_local_api_pki_created", "keys_printed": False})
    )
