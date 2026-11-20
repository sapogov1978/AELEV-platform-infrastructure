"""Offline regressions of this owner's canonical deployment."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import prepare
from aelev_observability_tools import read_env, validate

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / "deployment.json").read_text())
ROLE = SPEC["role"]


def model() -> dict[str, Any]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env.example",
            "--profile",
            "ui",
            "--profile",
            "provision",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env={k: v for k, v in os.environ.items() if not k.startswith("AELEV_")},
        capture_output=True,
        text=True,
        check=True,
    )
    decoded: dict[str, Any] = json.loads(result.stdout)
    return decoded


def test_complete_owner_model() -> None:
    data = model()
    assert ROLE in data["services"]
    assert not data["services"][ROLE].get("profiles")
    assert set(data["services"]) == {
        ROLE,
        ROLE + "-status-publisher",
        ROLE + "-status-provision",
    } | ({ROLE + "-configuration-init"} if ROLE != "blackbox-exporter" else set())
    assert set(data["services"][ROLE]["networks"]) == {ROLE + "-ui"}
    assert "docker.sock" not in json.dumps(data["services"])


def test_own_build_context_and_immutable_artifact() -> None:
    data = model()
    for service in data["services"].values():
        if "build" in service:
            assert Path(service["build"]["context"]) == ROOT
    packages = tomllib.loads((ROOT / "poetry.lock").read_text())["package"]
    package = next(p for p in packages if p["name"] == "aelev-observability-tools")
    entry = package["files"][0]
    assert (
        entry["hash"]
        == "sha256:"
        + hashlib.sha256((ROOT / "vendor" / entry["file"]).read_bytes()).hexdigest()
    )
    assert prepare.ROOT == ROOT


def test_templates_and_mounts_stay_in_owner() -> None:
    for item in SPEC["templates"]:
        assert (ROOT / item["source"]).resolve().is_relative_to(ROOT)
        assert (ROOT / item["source"]).is_file()
    for service in model()["services"].values():
        for mount in service.get("volumes", []):
            if mount["type"] == "bind":
                assert Path(mount["source"]).resolve().is_relative_to(ROOT)
                assert not mount.get("bind", {}).get("create_host_path", False)


def test_all_settings_are_owned_and_no_compose_defaults() -> None:
    env = read_env(ROOT / ".env.example", SPEC["prefix"])
    validate(env, env, SPEC)
    assert ":-" not in (ROOT / "compose.yml").read_text()
    assert "observability:" not in (ROOT / "compose.yml").read_text()


@pytest.mark.parametrize("missing", ["IMAGE", "SECRET_ROOT", "BIND", "UI_URL"])
def test_missing_setting_is_not_replaced(missing: str) -> None:
    env = read_env(ROOT / ".env.example", SPEC["prefix"])
    altered = dict(env)
    altered.pop(missing)
    with pytest.raises(ValueError):
        validate(altered, env, SPEC)


def test_init_is_explicit_and_has_no_network_or_secrets() -> None:
    services = model()["services"]
    if ROLE == "blackbox-exporter":
        assert ROLE + "-configuration-init" not in services
        return
    init = services[ROLE + "-configuration-init"]
    assert init["profiles"] == ["provision"]
    assert init["network_mode"] == "none"
    assert not init.get("environment")
    assert all(m["type"] == "volume" for m in init["volumes"])
    assert len(init["volumes"]) == 1


def test_remote_peer_targets_are_explicit_when_configured() -> None:
    env = read_env(ROOT / ".env.example", SPEC["prefix"])
    for key in SPEC.get("https", []):
        assert env[key].startswith("https://")
    if ROLE == "prometheus":
        assert env["PROMETHEUS_HEALTH_URL"].endswith(":19090/-/ready")
    if ROLE == "delivery":
        assert model()["services"][ROLE]["command"][0] == "run"


def network_model(settings: dict[str, str]) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AELEV_", "COMPOSE_"))
    }
    environment.update({SPEC["prefix"] + key: value for key, value in settings.items()})
    return subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env.example",
            "--profile",
            "ui",
            "--profile",
            "provision",
            "--file",
            "compose.yml",
            "--file",
            "compose.otlp-network.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "address,grpc,http",
    [("127.0.0.1", "34317", "34318"), ("0.0.0.0", "35317", "35318")],
)
def test_otlp_profile_only_appends_explicit_receiver_ports(
    address: str, grpc: str, http: str
) -> None:
    previous = model()
    result = network_model(
        {"OTLP_BIND_ADDRESS": address, "OTLP_GRPC_PORT": grpc, "OTLP_HTTP_PORT": http}
    )
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    ports = actual["services"][ROLE]["ports"]
    extra = [port for port in ports if port["target"] in {4317, 4318}]
    assert {(port["host_ip"], port["published"], port["target"]) for port in extra} == {
        (address, grpc, 4317),
        (address, http, 4318),
    }
    actual["services"][ROLE]["ports"] = [
        port for port in ports if port["target"] not in {4317, 4318}
    ]
    assert actual == previous


@pytest.mark.parametrize(
    "missing", ["OTLP_BIND_ADDRESS", "OTLP_GRPC_PORT", "OTLP_HTTP_PORT"]
)
def test_otlp_profile_refuses_missing_new_input(missing: str) -> None:
    settings = {
        "OTLP_BIND_ADDRESS": "127.0.0.1",
        "OTLP_GRPC_PORT": "34317",
        "OTLP_HTTP_PORT": "34318",
    }
    settings.pop(missing)
    result = network_model(settings)
    assert result.returncode != 0
    assert SPEC["prefix"] + missing in result.stderr
