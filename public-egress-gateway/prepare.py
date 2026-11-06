"""Explicit local PKI preparation; production identities come from SPIRE/PKI."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from authorization import validate_identity


def openssl(*arguments: str) -> None:
    subprocess.run(
        ["openssl", *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def root_certificate(directory: Path) -> None:
    openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:3072",
        "-nodes",
        "-sha256",
        "-days",
        "30",
        "-subj",
        "/CN=AELEV-local-egress-CA",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout",
        str(directory / "ca.key"),
        "-out",
        str(directory / "ca.pem"),
    )
    (directory / "ca.key").chmod(0o600)


def issue(
    issuer: Path, destination: Path, name: str, alternative: str, usage: str
) -> None:
    destination.mkdir(parents=True, mode=0o750)
    key, certificate = destination / (name + ".key"), destination / (name + ".pem")
    if key.exists() or certificate.exists():
        raise ValueError("refusing to overwrite an existing identity")
    with tempfile.TemporaryDirectory(prefix="aelev-egress-csr-") as scratch:
        request, extension = (
            Path(scratch) / "request.pem",
            Path(scratch) / "extension.cnf",
        )
        extension.write_text(
            "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage="
            + usage
            + "\nsubjectAltName="
            + alternative
            + "\n"
        )
        openssl(
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=AELEV-egress-identity",
            "-keyout",
            str(key),
            "-out",
            str(request),
        )
        openssl(
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(issuer / "ca.pem"),
            "-CAkey",
            str(issuer / "ca.key"),
            "-set_serial",
            str(int.from_bytes(os.urandom(16), "big")),
            "-days",
            "7",
            "-sha256",
            "-extfile",
            str(extension),
            "-out",
            str(certificate),
        )
    key.chmod(0o640)
    certificate.chmod(0o644)


def initialize(root: Path, server_name: str, public_ip: str | None) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", server_name):
        raise ValueError("invalid server DNS name")
    if public_ip is not None:
        ipaddress.ip_address(public_ip)
    issuer, gateway = root / "local-ca", root / "gateway"
    if issuer.exists() or gateway.exists():
        raise ValueError(
            "local PKI already exists; use the documented rotation procedure"
        )
    issuer.mkdir(parents=True, mode=0o700)
    root_certificate(issuer)
    alternative = (
        "DNS:" + server_name + ",DNS:public-egress-gateway,DNS:localhost,IP:127.0.0.1"
    )
    if public_ip is not None:
        alternative += ",IP:" + public_ip
    issue(issuer, gateway, "server", alternative, "serverAuth")
    shutil.copyfile(issuer / "ca.pem", gateway / "server-ca.pem")
    shutil.copyfile(issuer / "ca.pem", gateway / "client-ca.pem")
    (root / "telemetry" / "client").mkdir(parents=True, mode=0o750)
    (root / "telemetry" / "sender").mkdir(parents=True, mode=0o750)


def main() -> None:
    if shutil.which("openssl") is None:
        raise RuntimeError("OpenSSL is required; see README installation instructions")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-local")
    init.add_argument("--root", type=Path, default=Path("secrets"))
    init.add_argument("--server-name", required=True)
    init.add_argument("--public-ip")
    client = commands.add_parser("issue-client")
    client.add_argument("--issuer", type=Path, default=Path("secrets/local-ca"))
    client.add_argument("--output", type=Path, required=True)
    client.add_argument("--identity", required=True)
    args = parser.parse_args()
    if args.command == "init-local":
        initialize(args.root, args.server_name, args.public_ip)
    else:
        validate_identity(args.identity)
        issue(args.issuer, args.output, "client", "URI:" + args.identity, "clientAuth")
        shutil.copyfile(args.issuer / "ca.pem", args.output / "ca.pem")


if __name__ == "__main__":
    main()
