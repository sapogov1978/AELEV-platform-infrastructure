"""Owner telemetry has finite recovery and no dependency-heavy liveness probe."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "AELEV_INFRASTRUCTURE_PUBLIC_EGRESS_GATEWAY_"


def compiled(**overrides: str) -> dict[str, Any]:
    if shutil.which("docker") is None:
        pytest.skip("Offline Compose compilation is checked on the native host")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AELEV_", "COMPOSE_"))
    }
    environment.update({PREFIX + key: value for key, value in overrides.items()})
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env.example",
            "--profile",
            "ui",
            "--profile",
            "telemetry",
            "config",
            "--no-env-resolution",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_sender_has_only_local_liveness_and_automatic_owned_volume_init() -> None:
    services = compiled()["services"]
    sender = services["public-egress-gateway-telemetry-sender"]
    assert sender["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-S",
        "/opt/aelev/delivery_health.py",
    ]
    assert sender["stop_grace_period"] == "1m0s"
    assert (
        sender["depends_on"]["public-egress-gateway-telemetry-data-init"]["condition"]
        == "service_completed_successfully"
    )
    assert (
        services["public-egress-gateway-telemetry-data-init"]["network_mode"] == "none"
    )
    assert services["public-egress-gateway-telemetry-data-init"]["restart"] == "no"
    assert sender["read_only"] is True
    assert not any("docker.sock" in str(mount) for mount in sender["volumes"])


def test_default_restarts_are_bounded_and_existing_budget_overrides_survive() -> None:
    services = compiled()["services"]
    for name in (
        "public-egress-gateway",
        "public-egress-gateway-status-publisher",
        "public-egress-gateway-telemetry-sender",
    ):
        assert services[name]["restart"] == "on-failure:3"
    services = compiled(
        RESTART_POLICY="on-failure:2",
        UI_RESTART_POLICY="no",
        TELEMETRY_RESTART_POLICY="on-failure:1",
    )["services"]
    assert services["public-egress-gateway"]["restart"] == "on-failure:2"
    assert services["public-egress-gateway-status-publisher"]["restart"] == "no"
    assert (
        services["public-egress-gateway-telemetry-sender"]["restart"] == "on-failure:1"
    )


def test_selected_sender_is_pinned_and_health_uses_both_trust_domains() -> None:
    values = dict(
        line.split("=", 1)
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert re.fullmatch(
        r".+@sha256:[a-f0-9]{64}", values[PREFIX + "TELEMETRY_SENDER_IMAGE"]
    )
    config = json.loads((ROOT / "observability/sender.local.json").read_bytes())
    assert config["health_ca"] == "/run/secrets/telemetry/sender/health-ca.crt"
    assert config["health_cert"].endswith("/local-query.crt")
    assert config["client_cert"].endswith("/central-write.crt")
    assert config["client_ca"].endswith("/local-write-ca.crt")
