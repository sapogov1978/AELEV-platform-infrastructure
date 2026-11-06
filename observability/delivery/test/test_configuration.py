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
    assert data["services"][ROLE]["stop_grace_period"] == "20s"
    assert set(data["services"]) == {
        ROLE,
        ROLE + "-status-publisher",
        ROLE + "-status-provision",
    } | ({ROLE + "-configuration-init"} if ROLE != "blackbox-exporter" else set())
    assert set(data["services"][ROLE]["networks"]) == {ROLE + "-ui"}
    assert "docker.sock" not in json.dumps(data["services"])


def test_receiver_uses_minimal_probe_and_explicit_restart_budget() -> None:
    receiver = model()["services"]["delivery"]
    assert receiver["restart"] == "on-failure:3"
    assert receiver["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-S",
        "/opt/aelev/delivery_health.py",
    ]
    recipe = (ROOT / "Dockerfile.status").read_text()
    assert "aelev/observability-sender:1.0.6@sha256:ccccd35e" in recipe
    assert "COPY --from=sqlite /usr/local/lib/libsqlite3.so.0" in recipe
    assert "sqlite3.sqlite_version_info == (3, 53, 4)" in recipe
    assert "COPY delivery_snapshot.py /opt/aelev/delivery_snapshot.py" in recipe


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
