"""Verify generated local chains with strict X.509 validation."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_tls", ROOT / "prepare-tls.py")
assert SPEC and SPEC.loader
prepare_tls = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare_tls)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="OpenSSL unavailable")
@pytest.mark.parametrize("peer_ips", ["", "10.248.0.1,2001:db8::12"])
def test_new_local_identities_have_strictly_verifiable_chains(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, peer_ips: str
) -> None:
    monkeypatch.setattr(prepare_tls, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["prepare-tls.py"])
    (tmp_path / ".env").write_bytes((ROOT / ".env.example").read_bytes())
    with (tmp_path / ".env").open("a") as stream:
        stream.write(
            prepare_tls.PREFIX + "NODE_1_ETCD_PEER_CLIENT_IPS=" + peer_ips + "\n"
        )
    previous_umask = os.umask(0o077)
    try:
        prepare_tls.main()
    finally:
        os.umask(previous_umask)
    secrets = tmp_path / "secrets"
    for certificate in secrets.rglob("*.crt"):
        if certificate.name.endswith("ca.crt") or certificate.parent.name == "issuer":
            continue
        etcd = certificate.parent.name.startswith(
            "etcd-"
        ) or certificate.name.startswith("etcd-")
        issuer = secrets / "issuer" / ("etcd.crt" if etcd else "postgresql.crt")
        subprocess.run(
            [
                "openssl",
                "verify",
                "-x509_strict",
                "-CAfile",
                str(issuer),
                str(certificate),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    existing = (secrets / "fencing/server.crt").read_bytes()
    for address in peer_ips.split(",") if peer_ips else []:
        subprocess.run(
            [
                "openssl",
                "verify",
                "-x509_strict",
                "-verify_ip",
                address,
                "-CAfile",
                str(secrets / "issuer/etcd.crt"),
                str(secrets / "etcd-1/server.crt"),
            ],
            check=True,
            capture_output=True,
        )
    with pytest.raises(SystemExit, match="Existing identities are preserved"):
        prepare_tls.main()
    assert (secrets / "fencing/server.crt").read_bytes() == existing


@pytest.mark.parametrize(
    "raw",
    [
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "ff02::1",
        "10.0.0.0/24",
        "10.0.0.1,,10.0.0.2",
        "10.0.0.1\nsubjectAltName=DNS:other",
        "fe80::1%eth0",
        ",".join(["10.0.0.1"] * 17),
    ],
)
def test_invalid_peer_source_addresses_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="Invalid etcd peer client IP list"):
        prepare_tls.peer_client_ips(
            {prepare_tls.PREFIX + "NODE_1_ETCD_PEER_CLIENT_IPS": raw}, 1
        )


def test_invalid_peer_address_does_not_create_partial_pki(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(prepare_tls, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["prepare-tls.py"])
    (tmp_path / ".env").write_text(
        prepare_tls.PREFIX + "NODE_1_ETCD_PEER_CLIENT_IPS=invalid\n"
    )
    with pytest.raises(SystemExit, match="Invalid NODE_N_ETCD_PEER_CLIENT_IPS"):
        prepare_tls.main()
    assert not (tmp_path / "secrets").exists()
