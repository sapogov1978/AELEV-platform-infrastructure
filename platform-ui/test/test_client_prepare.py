"""Operator-issued client credentials must pass strict X.509 path validation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREPARE = Path(
    os.environ.get(
        "AELEV_TEST_PLATFORM_UI_CLIENT_PREPARE",
        str(ROOT / "client/src/aelev_platform_ui_client/prepare.py"),
    )
)


@pytest.mark.parametrize("system_signer", [False, True])
def test_operator_leaf_supports_strict_client_auth_and_preserves_existing(
    tmp_path: Path,
    system_signer: bool,
) -> None:
    if system_signer and (
        sys.platform != "darwin" or not Path("/usr/bin/openssl").is_file()
    ):
        pytest.skip("additional macOS system LibreSSL signer")
    ca, key = tmp_path / "ca.crt", tmp_path / "ca.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=ephemeral-operator-test-ca",
            "-days",
            "2",
            "-keyout",
            str(key),
            "-out",
            str(ca),
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-addext",
            "subjectKeyIdentifier=hash",
            "-addext",
            "authorityKeyIdentifier=keyid:always",
        ],
        check=True,
        capture_output=True,
    )
    destination = tmp_path / "client"
    arguments = [
        sys.executable,
        str(PREPARE),
        "--root",
        str(destination),
        "--name",
        "probe",
        "--ca-cert",
        str(ca),
        "--ca-key",
        str(key),
    ]
    signer_environment = dict(os.environ)
    if system_signer:
        binary_directory = tmp_path / "signer-bin"
        binary_directory.mkdir()
        (binary_directory / "openssl").symlink_to("/usr/bin/openssl")
        signer_environment["PATH"] = (
            str(binary_directory) + os.pathsep + signer_environment["PATH"]
        )
    subprocess.run(arguments, check=True, capture_output=True, env=signer_environment)
    result = subprocess.run(
        [
            "openssl",
            "verify",
            "-x509_strict",
            "-purpose",
            "sslclient",
            "-CAfile",
            str(ca),
            str(destination / "probe.crt"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    before = {path: path.read_bytes() for path in destination.iterdir()}
    repeated = subprocess.run(arguments, capture_output=True, env=signer_environment)
    assert repeated.returncode != 0
    assert before == {path: path.read_bytes() for path in destination.iterdir()}
    assert (destination / "probe.key").stat().st_mode & 0o777 == 0o640
