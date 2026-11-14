"""Ephemeral TLS material only; no provider credential or API call."""

from __future__ import annotations

import hashlib
import json
import ssl
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

OWNER = "translator-service"
SERVER = f"spiffe://test.example/{OWNER}/compute/instance"
CLIENT = f"spiffe://test.example/{OWNER}/control/instance"


def openssl(*arguments: str) -> str:
    return subprocess.run(
        ["openssl", *arguments], capture_output=True, text=True, check=True, timeout=15
    ).stdout.strip()


class Authority:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        (root / "new").mkdir()
        (root / "index").write_text("")
        (root / "serial").write_text("01\n")
        self.cert, self.key = root / "ca.crt", root / "ca.key"
        openssl(
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=ephemeral-test-authority",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",
            str(self.key),
            "-out",
            str(self.cert),
        )
        self.sequence = 0

    def leaf(
        self,
        names: list[str],
        *,
        expired: bool = False,
        is_ca: bool = False,
        dns_names: tuple[str, ...] = ("localhost",),
    ) -> tuple[Path, Path]:
        self.sequence += 1
        stem = self.root / f"leaf-{self.sequence}"
        key, csr, cert = (
            stem.with_suffix(".key"),
            stem.with_suffix(".csr"),
            stem.with_suffix(".crt"),
        )
        openssl(
            "req",
            "-new",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-subj",
            "/CN=ephemeral-leaf",
            "-keyout",
            str(key),
            "-out",
            str(csr),
        )
        config = stem.with_suffix(".cnf")
        config.write_text(
            f"[ca]\ndefault_ca=authority\n[authority]\ndatabase={self.root}/index\n"
            f"serial={self.root}/serial\nnew_certs_dir={self.root}/new\n"
            f"certificate={self.cert}\nprivate_key={self.key}\n"
            "default_md=sha256\npolicy=subject\nx509_extensions=leaf\nunique_subject=no\n"
            "[subject]\ncommonName=supplied\n[leaf]\nbasicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth,clientAuth\n"
            "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n"
            "subjectAltName="
            + ",".join(
                [
                    *("DNS:" + name for name in dns_names),
                    *("URI:" + name for name in names),
                ]
            )
            + "\n"
        )
        if is_ca:
            config.write_text(
                config.read_text()
                .replace("CA:FALSE", "CA:TRUE")
                .replace(
                    "keyUsage=critical,digitalSignature",
                    "keyUsage=critical,digitalSignature,keyCertSign,cRLSign",
                )
            )
        now = datetime.now(UTC)
        before = now - timedelta(minutes=10)
        after = now + timedelta(minutes=-1 if expired else 10)
        openssl(
            "ca",
            "-batch",
            "-notext",
            "-config",
            str(config),
            "-in",
            str(csr),
            "-out",
            str(cert),
            "-startdate",
            before.strftime("%y%m%d%H%M%SZ"),
            "-enddate",
            after.strftime("%y%m%d%H%M%SZ"),
        )
        key.chmod(0o600)
        return cert, key


def delivered(
    root: Path,
    authority: Authority,
    cert: Path,
    key: Path,
    own: str = SERVER,
    role: str = "server",
) -> None:
    root.mkdir(exist_ok=True)
    policy = {
        "schema_version": 1,
        "environment": "test",
        "delivery": "spire-file-v1",
        "server_spiffe_id": SERVER,
        "client_spiffe_id": CLIENT,
    }
    (root / "identity.json").write_text(json.dumps(policy))
    raw = key.read_bytes() + cert.read_bytes()
    bundle = authority.cert.read_bytes()
    generation = hashlib.sha256(raw + bundle).hexdigest()
    directory = root / "generations" / generation
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tls.pem").write_bytes(raw)
    (directory / "tls.pem").chmod(0o600)
    (directory / "bundle.pem").write_bytes(bundle)
    expires = openssl("x509", "-in", str(cert), "-noout", "-enddate").split("=", 1)[1]
    serial = openssl("x509", "-in", str(cert), "-noout", "-serial").split("=", 1)[1]
    marker = {
        "owner": "spire-svid-delivery",
        "schema_version": 1,
        "state": "READY",
        "environment": "test",
        "spiffe_id": own,
        "updated_at": datetime.now(UTC).isoformat(),
        "expires_at": datetime.fromtimestamp(
            ssl.cert_time_to_seconds(expires), UTC
        ).isoformat(),
        "serial": str(int(serial, 16)),
        "generation": generation,
        "tls_sha256": hashlib.sha256(raw).hexdigest(),
        "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
    }
    temporary = root / "new.json"
    temporary.write_text(json.dumps(marker))
    temporary.replace(root / "current.json")
