#!/usr/bin/env python3
"""Create operator-managed local TLS identities without overwriting secrets."""

from __future__ import annotations

import ipaddress
import argparse
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
PREFIX = "AELEV_INFRASTRUCTURE_POSTGRESQL_"


def run(*arguments: str) -> None:
    """Execute OpenSSL without logging private material."""
    subprocess.run(["openssl", *arguments], check=True, capture_output=True)


def main() -> None:
    """Generate two separate trust domains and all cluster identities."""
    if not shutil.which("openssl"):
        raise SystemExit("OpenSSL is required; see README.md")
    values = dict(
        line.split("=", 1)
        for line in (ROOT / ".env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--fencing-only", action="store_true")
    args = parser.parse_args()
    if args.fencing_only:
        prepare_fencing(values)
        return
    destination = ROOT / "secrets"
    if any(
        (destination / name).exists()
        for name in (
            "issuer",
            "admin",
            "node-1",
            "node-2",
            "node-3",
            "etcd-1",
            "etcd-2",
            "etcd-3",
        )
    ):
        raise SystemExit(
            "Existing identities are preserved; refusing to overwrite secrets"
        )
    os.umask(0o077)
    issuer = destination / "issuer"
    issuer.mkdir(parents=True)
    for domain in ("postgresql", "etcd"):
        run(
            "req",
            "-x509",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-days",
            "365",
            "-subj",
            f"/CN=AELEV {domain} operator CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-addext",
            "subjectKeyIdentifier=hash",
            "-keyout",
            str(issuer / f"{domain}.key"),
            "-out",
            str(issuer / f"{domain}.crt"),
        )

    def certificate(
        directory: Path,
        name: str,
        common_name: str,
        domain: str,
        host: str | None = None,
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        run(
            "req",
            "-new",
            "-newkey",
            "rsa:3072",
            "-nodes",
            "-subj",
            f"/CN={common_name}",
            "-keyout",
            str(directory / f"{name}.key"),
            "-out",
            str(directory / f"{name}.csr"),
        )
        extensions = [
            "basicConstraints=critical,CA:FALSE",
            "subjectKeyIdentifier=hash",
            "authorityKeyIdentifier=keyid:always",
            "keyUsage=critical,digitalSignature,keyEncipherment",
            (
                "extendedKeyUsage=clientAuth"
                if host is None
                else "extendedKeyUsage=serverAuth,clientAuth"
            ),
        ]
        if host:
            try:
                ipaddress.ip_address(host)
                alternative = f"IP:{host}"
            except ValueError:
                alternative = f"DNS:{host}"
            extensions.append(
                f"subjectAltName={alternative},DNS:localhost,IP:127.0.0.1"
            )
        config = directory / f"{name}.extensions"
        config.write_text("\n".join(extensions) + "\n")
        run(
            "x509",
            "-req",
            "-in",
            str(directory / f"{name}.csr"),
            "-CA",
            str(issuer / f"{domain}.crt"),
            "-CAkey",
            str(issuer / f"{domain}.key"),
            "-set_serial",
            str(secrets.randbits(128)),
            "-days",
            "90",
            "-sha256",
            "-extfile",
            str(config),
            "-out",
            str(directory / f"{name}.crt"),
        )
        config.unlink()
        (directory / f"{name}.csr").unlink()

    admin = destination / "admin"
    certificate(admin, "admin", values[PREFIX + "ADMIN_USER"], "postgresql")
    certificate(admin, "replication", "aelev_replication", "postgresql")
    for name in ("admin-password", "replication-password", "provision-password"):
        (admin / name).write_text(secrets.token_hex(32) + "\n")
    shutil.copyfile(issuer / "postgresql.crt", admin / "ca.crt")
    for node in range(1, 4):
        node_dir = destination / f"node-{node}"
        certificate(
            node_dir,
            "server",
            f"postgresql-{node}",
            "postgresql",
            values[PREFIX + f"NODE_{node}_HOST"],
        )
        certificate(node_dir, "etcd-client", f"patroni-{node}", "etcd")
        for name in (
            "admin.crt",
            "admin.key",
            "replication.crt",
            "replication.key",
            "admin-password",
            "replication-password",
            "ca.crt",
        ):
            shutil.copyfile(admin / name, node_dir / name)
        shutil.copyfile(issuer / "etcd.crt", node_dir / "etcd-ca.crt")
        etcd_dir = destination / f"etcd-{node}"
        certificate(
            etcd_dir,
            "server",
            f"etcd-{node}",
            "etcd",
            values[PREFIX + f"NODE_{node}_ETCD_HOST"],
        )
        certificate(etcd_dir, "client", f"etcd-probe-{node}", "etcd")
        shutil.copyfile(issuer / "etcd.crt", etcd_dir / "ca.crt")

    if values[PREFIX + "FENCING_MODE"] != "watchdog":
        prepare_fencing(values)


def prepare_fencing(values: dict[str, str]) -> None:
    """Add only the executor identity, preserving every existing cluster key."""
    destination = ROOT / "secrets/fencing"
    if destination.exists():
        raise SystemExit(
            "Existing fencing identity is preserved; refusing to overwrite"
        )
    issuer = ROOT / "secrets/issuer"
    node = ROOT / "secrets/node-1"
    required = [issuer / "postgresql.crt", issuer / "postgresql.key"] + [
        node / name
        for name in (
            "admin.crt",
            "admin.key",
            "ca.crt",
            "etcd-ca.crt",
            "etcd-client.crt",
            "etcd-client.key",
        )
    ]
    if not all(path.is_file() for path in required):
        raise SystemExit("Existing local issuer and node-1 identities are required")
    os.umask(0o077)
    destination.mkdir()
    hosts = {"localhost", "127.0.0.1"}
    for number in range(1, 4):
        address = values[PREFIX + f"NODE_{number}_FENCING_URL"]
        host = urlsplit(address).hostname
        if urlsplit(address).scheme != "https" or not host:
            raise SystemExit("Fencing endpoints require HTTPS and a hostname")
        hosts.add(host)
    alternatives = []
    for host in sorted(hosts):
        try:
            ipaddress.ip_address(host)
            alternatives.append("IP:" + host)
        except ValueError:
            alternatives.append("DNS:" + host)
    extensions = destination / "server.extensions"
    extensions.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid:always\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=" + ",".join(alternatives) + "\n"
    )
    run(
        "req",
        "-new",
        "-newkey",
        "rsa:3072",
        "-nodes",
        "-subj",
        "/CN=postgresql-fencer",
        "-keyout",
        str(destination / "server.key"),
        "-out",
        str(destination / "server.csr"),
    )
    run(
        "x509",
        "-req",
        "-in",
        str(destination / "server.csr"),
        "-CA",
        str(issuer / "postgresql.crt"),
        "-CAkey",
        str(issuer / "postgresql.key"),
        "-set_serial",
        str(secrets.randbits(128)),
        "-days",
        "90",
        "-sha256",
        "-extfile",
        str(extensions),
        "-out",
        str(destination / "server.crt"),
    )
    for name in (
        "admin.crt",
        "admin.key",
        "ca.crt",
        "etcd-ca.crt",
        "etcd-client.crt",
        "etcd-client.key",
    ):
        shutil.copyfile(node / name, destination / name)
    extensions.unlink()
    (destination / "server.csr").unlink()


if __name__ == "__main__":
    main()
