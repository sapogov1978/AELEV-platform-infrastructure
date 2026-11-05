import shutil
import subprocess
from pathlib import Path

import pytest

from certificate_authority import prepare, stage


def test_explicit_rollover_reuses_key_and_stages_overlap(tmp_path: Path) -> None:
    executable = shutil.which("openssl")
    if executable is None:
        pytest.skip("OpenSSL required")
    source = tmp_path / "source"
    source.mkdir()
    certificate = source / "ca.crt"
    private = source / "ca.key"
    subprocess.run(
        [
            executable,
            "req",
            "-new",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "40",
            "-subj",
            "/CN=test-ca",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",
            str(private),
            "-out",
            str(certificate),
        ],
        capture_output=True,
        check=True,
    )
    private.chmod(0o600)
    output = tmp_path / "candidate"
    receipt = prepare(certificate, private, output, "test-owner", 365, executable)
    assert receipt["automatic_activation"] is False
    assert receipt["private_key_reused"] is True
    trust = tmp_path / "trust.crt"
    trust.write_bytes(certificate.read_bytes())
    assert stage(certificate, output, trust, "test-owner", executable)["changed"] is True
    assert trust.read_bytes().count(b"BEGIN CERTIFICATE") == 2
    assert stage(certificate, output, trust, "test-owner", executable)["changed"] is False

    bundle = tmp_path / "bundle.crt"
    bundle.write_bytes(certificate.read_bytes() + certificate.read_bytes())
    with pytest.raises(ValueError, match="TRUST_INVALID"):
        stage(certificate, output, bundle, "test-owner", executable)
