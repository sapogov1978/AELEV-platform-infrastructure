"""Canonical owner recipes use bounded, dependency-independent liveness."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
NATIVE = {
    "blackbox-exporter": 9115,
    "loki": 3100,
    "tempo": 3200,
    "otel-collector": 13133,
}


def model(owner: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env.example",
            "--profile",
            "ui",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT / "observability" / owner,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AELEV_")
        },
        check=True,
        capture_output=True,
        text=True,
    )
    value: dict[str, Any] = json.loads(result.stdout)
    return value["services"]


@pytest.mark.parametrize("owner,port", list(NATIVE.items()))
def test_native_probe_is_local_and_owner_contained(owner: str, port: int) -> None:
    root = ROOT / "observability" / owner
    row = model(owner)[owner]
    assert row["restart"] == "no"
    assert row["healthcheck"]["test"] == [
        "CMD",
        "/opt/aelev/native-health",
        str(port),
        "localhost",
        "/run/secrets/observability",
    ]
    assert row["healthcheck"]["timeout"] == "5s"
    assert Path(row["build"]["context"]) == root
    dockerfile = (root / "Dockerfile.native").read_text()
    archive = root / "vendor/aelev-native-health-1.0.0.tar.gz"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() in dockerfile
    assert "FROM ${BASE_IMAGE} AS runtime" in dockerfile
    assert "-static" in dockerfile
    with tarfile.open(archive) as package:
        assert set(package.getnames()) == {"health.c", "README.md"}
        assert all(item.isfile() and not item.issym() for item in package.getmembers())
    assert "docker.sock" not in json.dumps(row)


@pytest.mark.parametrize("owner", ["blackbox-exporter", "prometheus"])
def test_operator_reads_only_its_own_event_loop_signals(owner: str) -> None:
    services = model(owner)
    row = services[owner + "-operator"]
    assert row["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-S",
        "/opt/aelev/operator-health.py",
    ]
    assert row["restart"] == "no"
    assert row["image"] != services.get(owner + "-status-publisher", {}).get("image")
    dockerfile = (ROOT / "observability" / owner / "Dockerfile.status").read_text()
    assert (
        "/aelev_observability_tools/operator_health.py /opt/aelev/operator-health.py"
        in dockerfile
    )


def test_canonical_infinite_policies_are_removed_without_a_second_controller() -> None:
    paths = [
        "keycloak/compose.yml",
        "minio/compose.yml",
        "platform-ui/compose.yml",
        "postgresql/compose.runtime.yml",
        "observability/prometheus/compose.yml",
        "observability/grafana/compose.yml",
        "observability/delivery/compose.yml",
        "observability/otel-collector/compose.yml",
        "observability/loki/compose.yml",
        "observability/blackbox-exporter/compose.yml",
    ]
    for path in paths:
        assert "restart: unless-stopped" not in (ROOT / path).read_text()
    assert "services:" not in (ROOT / "compose.yml").read_text()
