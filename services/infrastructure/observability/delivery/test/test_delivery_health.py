"""Local process signals cannot be replaced by healthy dependencies or old PIDs."""

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import delivery_health


def signal(value: dict[str, object]) -> str:
    return "\n".join(
        str(value[k])
        for k in (
            "owner",
            "schema_version",
            "mode",
            "pid",
            "process_start",
            "monotonic_ns",
        )
    )


def fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    proc = tmp_path / "proc"
    (proc / "1").mkdir(parents=True)
    (proc / "1/stat").write_text("1 (python) " + "S " + "0 " * 18 + "123 0")
    value: dict[str, object] = {
        "owner": "telemetry-delivery",
        "schema_version": 1,
        "mode": "sender",
        "pid": 1,
        "process_start": "123",
        "monotonic_ns": time.monotonic_ns(),
    }
    path = tmp_path / "heartbeat.signal"
    path.write_text(signal(value))
    path.chmod(0o600)
    return path, proc, value


def test_central_archive_uses_the_same_local_incarnation_signal(tmp_path: Path) -> None:
    path, proc, value = fixture(tmp_path)
    value["mode"] = "archive"
    path.write_text(signal(value))
    assert delivery_health.healthy(path, proc)


@pytest.mark.parametrize(
    "defect",
    [
        "stale",
        "future",
        "pid",
        "reused",
        "mode",
        "owner",
        "permissions",
        "symlink",
        "oversize",
        "malformed",
    ],
)
def test_invalid_signals_fail_closed(tmp_path: Path, defect: str) -> None:
    path, proc, value = fixture(tmp_path)
    assert delivery_health.healthy(path, proc)
    if defect == "stale":
        value["monotonic_ns"] = time.monotonic_ns() - 16_000_000_000
    elif defect == "future":
        value["monotonic_ns"] = time.monotonic_ns() + 60_000_000_000
    elif defect == "pid":
        value["pid"] = 2
    elif defect == "reused":
        value["process_start"] = "122"
    elif defect == "mode":
        value["mode"] = "unknown"
    elif defect == "owner":
        value["owner"] = "another-owner"
    elif defect == "permissions":
        path.chmod(0o644)
    elif defect == "symlink":
        path.rename(tmp_path / "actual")
        path.symlink_to(tmp_path / "actual")
    elif defect in {"oversize", "malformed"}:
        path.write_text("x" * (4097 if defect == "oversize" else 1))
        assert not delivery_health.healthy(path, proc)
        return
    path.write_text(signal(value))
    assert not delivery_health.healthy(path, proc)


def test_publication_is_private_atomic_and_bound_to_current_process(
    tmp_path: Path,
) -> None:
    path, proc, _ = fixture(tmp_path)
    with patch.object(delivery_health.os, "getpid", return_value=1):
        delivery_health.publish("sender", path, proc)
    assert delivery_health.healthy(path, proc)
    assert not list(tmp_path.glob(".delivery-health-*"))
    assert path.stat().st_mode & 0o777 == 0o600
    path.unlink()
    assert not delivery_health.healthy(path, proc)


def test_probe_uses_only_standard_library_without_application(tmp_path: Path) -> None:
    source = Path(delivery_health.__file__).read_text()
    assert "import ssl" not in source and "import sqlite" not in source
    assert "import json" not in source and "import pathlib" not in source
    result = subprocess.run(
        [sys.executable, "-S", "-P", delivery_health.__file__],
        capture_output=True,
        timeout=5,
        check=False,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )
    assert result.returncode == 1
    assert result.stdout == result.stderr == b""


def test_special_file_cannot_block_probe(tmp_path: Path) -> None:
    path = tmp_path / "not-a-heartbeat"
    os.mkfifo(path, 0o600)
    started = time.monotonic()
    assert not delivery_health.healthy(path)
    assert time.monotonic() - started < 1
