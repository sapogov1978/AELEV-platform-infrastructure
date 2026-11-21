"""Render the real independent worker without a Docker daemon or credentials."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "compose.compute-client.hosts.yml"
PREFIX = "AELEV_INFRASTRUCTURE_TRANSLATOR_"


def inputs(address: str = "192.0.2.22") -> dict[str, str]:
    return {
        PREFIX + key: value
        for key, value in {
            "WORKER_IMAGE": "registry.example/worker@sha256:" + "a" * 64,
            "SECRET_GID": "10001",
            "PROVIDER_URL": "https://provider.owner.test:18487",
            "ENVIRONMENT": "production",
            "HIGH_PRIORITY_BURST": "4",
            "PRESENCE_INTERVAL_SECONDS": "10",
            "PRESENCE_TTL_SECONDS": "30",
            "DATABASE_HOST": "192.0.2.11",
            "DATABASE_PORT": "5432",
            "DATABASE_NAME": "translator",
            "DATABASE_USER": "translator",
            "DATABASE_SSLMODE": "verify-full",
            "DATABASE_CA_FILE": "ca.crt",
            "DATABASE_CLIENT_CERT_FILE": "client.crt",
            "DATABASE_CLIENT_KEY_FILE": "client.key",
            "DATABASE_SECRET_ROOT": "/fixture/database",
            "OTLP_HTTP_ENDPOINT": "https://telemetry.owner.test:4318",
            "TELEMETRY_SECRET_ROOT": "/fixture/telemetry",
            "WORKER_IDENTITY_POLICY": "/fixture/identity.json",
            "ROLE_MEMORY_LIMIT": "256m",
            "ROLE_CPUS": "0.5",
            "WORKER_SVID_VOLUME": "fixture-worker-svid",
            "PROVIDER_DNS_NAME": "provider.owner.test",
            "PROVIDER_NODE_ADDRESS": address,
        }.items()
    }


def render(
    tmp_path: Path, values: dict[str, str], *, opt_in: bool
) -> subprocess.CompletedProcess[str]:
    compose = shutil.which("docker-compose")
    docker = shutil.which("docker")
    assert (
        compose is not None or docker is not None
    ), "Docker Compose config is required for model verification"
    env_file = tmp_path / "explicit.env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir(exist_ok=True)
    command = [compose] if compose is not None else [str(docker), "compose"]
    command.extend(
        [
            "--project-directory",
            str(tmp_path),
            "--env-file",
            str(env_file),
            "--file",
            str(ROOT / "compose.compute-worker.mtls.yml"),
        ]
    )
    if opt_in:
        command.extend(["--file", str(PROFILE)])
    command.extend(["config", "--format", "json"])
    return subprocess.run(
        command,
        cwd=tmp_path,
        env={
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C",
            "DOCKER_HOST": "unix://" + str(tmp_path / "absent-daemon.sock"),
            "DOCKER_CONFIG": str(docker_config),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def model(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode == 0, result.stderr
    value: dict[str, Any] = json.loads(result.stdout)
    return value


@pytest.mark.parametrize("address", ["192.0.2.22", "2001:db8::22"])
def test_opt_in_changes_only_worker_host_mapping(tmp_path: Path, address: str) -> None:
    baseline = model(render(tmp_path, inputs(address), opt_in=False))
    mapped = model(render(tmp_path, inputs(address), opt_in=True))
    assert set(mapped["services"]) == {"translator-worker"}
    worker = mapped["services"]["translator-worker"]
    assert worker.pop("extra_hosts") == ["provider.owner.test=" + address]
    assert mapped == baseline
    assert worker["environment"]["AELEV_TRANSLATOR_PROVIDER_TRANSPORT"] == "mtls"
    assert worker["environment"]["AELEV_ENVIRONMENT"] == "production"


def test_omission_needs_no_mapping_values_and_preserves_full_model(
    tmp_path: Path,
) -> None:
    values = inputs()
    original = model(render(tmp_path, values, opt_in=False))
    del values[PREFIX + "PROVIDER_DNS_NAME"]
    del values[PREFIX + "PROVIDER_NODE_ADDRESS"]
    assert model(render(tmp_path, values, opt_in=False)) == original


@pytest.mark.parametrize("field", ["PROVIDER_DNS_NAME", "PROVIDER_NODE_ADDRESS"])
@pytest.mark.parametrize("value", [None, ""])
def test_selected_profile_requires_each_explicit_mapping_value(
    tmp_path: Path, field: str, value: str | None
) -> None:
    values = inputs()
    if value is None:
        del values[PREFIX + field]
    else:
        values[PREFIX + field] = value
    result = render(tmp_path, values, opt_in=True)
    assert result.returncode != 0
    assert PREFIX + field in result.stderr


def test_independent_worker_instances_can_target_different_provider_nodes(
    tmp_path: Path,
) -> None:
    first = model(render(tmp_path, inputs("192.0.2.22"), opt_in=True))
    second = model(render(tmp_path, inputs("198.51.100.44"), opt_in=True))
    assert (
        first["services"]["translator-worker"]["extra_hosts"]
        != second["services"]["translator-worker"]["extra_hosts"]
    )
    for value in (first, second):
        worker = value["services"]["translator-worker"]
        assert worker.get("network_mode") is None
        assert "ports" not in worker
        assert "translation-provider" not in value["services"]
