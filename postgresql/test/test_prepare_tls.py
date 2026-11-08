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
def test_new_local_identities_have_strictly_verifiable_chains(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(prepare_tls, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["prepare-tls.py"])
    (tmp_path / ".env").write_bytes((ROOT / ".env.example").read_bytes())
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
    with pytest.raises(SystemExit, match="Existing identities are preserved"):
        prepare_tls.main()
    assert (secrets / "fencing/server.crt").read_bytes() == existing
