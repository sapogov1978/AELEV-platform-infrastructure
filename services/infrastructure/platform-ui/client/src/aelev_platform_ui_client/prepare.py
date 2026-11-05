"""Explicit operator-side PKI delivery; never run with issuer keys in a workload."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path


def openssl(*arguments: str) -> None:
    subprocess.run(["openssl", *arguments], check=True, capture_output=True)


def deliver(destination: Path, payload: bytes) -> None:
    if destination.exists():
        if destination.read_bytes() != payload:
            raise ValueError("existing credential is preserved: " + destination.name)
        return
    with destination.open("xb") as output:
        os.chmod(destination, 0o640)
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--name", default="publisher")
    parser.add_argument("--ca-cert", type=Path)
    parser.add_argument("--ca-key", type=Path)
    parser.add_argument("--trust", action="append", default=[])
    parser.add_argument("--admin-root", type=Path)
    parser.add_argument("--admin-user")
    parser.add_argument("--admin-password-file", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args.name):
        parser.error("invalid credential filename")
    if bool(args.ca_cert) != bool(args.ca_key):
        parser.error("both operator CA files are required")
    if args.admin_root and not (args.admin_user and args.admin_password_file):
        parser.error("both administrative inputs are required")
    openssl("version")
    os.umask(0o027)
    args.root.mkdir(parents=True, exist_ok=True)
    args.root.chmod(0o750)
    # Do not replace files while an existing identity may still own a module.
    if args.ca_cert:
        destination_key = args.root / (args.name + ".key")
        destination_cert = args.root / (args.name + ".crt")
        if destination_key.exists() or destination_cert.exists():
            raise ValueError(
                "existing identity preserved; use the approved rotation procedure"
            )
        with tempfile.TemporaryDirectory(prefix="aelev-presentation-pki-") as temporary:
            directory = Path(temporary)
            extension = directory / "extensions"
            extension.write_text(
                "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=clientAuth\n"
            )
            openssl(
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                "/CN=" + args.name,
                "-keyout",
                str(directory / "client.key"),
                "-out",
                str(directory / "client.csr"),
            )
            openssl(
                "x509",
                "-req",
                "-in",
                str(directory / "client.csr"),
                "-CA",
                str(args.ca_cert),
                "-CAkey",
                str(args.ca_key),
                "-set_serial",
                str(secrets.randbits(128)),
                "-days",
                "30",
                "-sha256",
                "-extfile",
                str(extension),
                "-out",
                str(directory / "client.crt"),
            )
            deliver(destination_key, (directory / "client.key").read_bytes())
            deliver(destination_cert, (directory / "client.crt").read_bytes())
    for entry in args.trust:
        name, separator, source = entry.partition("=")
        if not separator or Path(name).name != name or not name.endswith(".crt"):
            raise ValueError("trust must be a certificate basename=path")
        path = Path(source)
        openssl("x509", "-in", str(path), "-noout")
        deliver(args.root / name, path.read_bytes())
    if args.admin_root:
        args.admin_root.mkdir(parents=True, exist_ok=True)
        args.admin_root.chmod(0o750)
        deliver(args.admin_root / "admin-user", (args.admin_user + "\n").encode())
        deliver(
            args.admin_root / "admin-password", args.admin_password_file.read_bytes()
        )


if __name__ == "__main__":
    main()
