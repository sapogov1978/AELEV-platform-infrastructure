"""Render the actual owner network recipe; no service or credential is created."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

import recovery
from test_configuration import _clean_environment

ROOT = Path(__file__).resolve().parents[1]


def network_model(
    tmp_path: Path, instance: str = "network", subnet: int = 81
) -> dict[str, Any]:
    env = tmp_path / (instance + ".env")
    prefix = "AELEV_INFRASTRUCTURE_KEYCLOAK_"
    values = {
        "COMPOSE_PROJECT": "keycloak-test-" + instance,
        "NETWORK": "keycloak-test-" + instance + "-client",
        "INGRESS_NETWORK": "keycloak-test-" + instance + "-ingress",
        "NETWORK_SUBNET": f"10.254.{subnet}.0/28",
        "INGRESS_NETWORK_SUBNET": f"10.254.{subnet}.16/28",
        "DATABASE_TLS_VOLUME": "keycloak-test-" + instance + "-tls",
        "DATABASE_VOLUME": "keycloak-test-" + instance + "-sql",
        "UI_STATE_VOLUME": "keycloak-test-" + instance + "-status",
        "ADMIN_PASSWORD_FILE": str(tmp_path / (instance + "-admin")),
        "DATABASE_PASSWORD_FILE": str(tmp_path / (instance + "-password")),
        "DATABASE_SERVER_TLS_ROOT": str(tmp_path / (instance + "-server")),
        "DATABASE_CLIENT_TLS_ROOT": str(tmp_path / (instance + "-client")),
        "UI_SECRET_ROOT": str(tmp_path / (instance + "-status")),
        "SECRET_GID": str(os.getegid()),
        "UI_SECRET_GID": str(os.getegid()),
    }
    env.write_text(
        "".join(prefix + key + "=" + value + "\n" for key, value in values.items())
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ROOT / ".env.example"),
            "--env-file",
            str(ROOT / "network.env.example"),
            "--env-file",
            str(env),
            "-f",
            str(ROOT / "compose.yml"),
            "-f",
            str(ROOT / "compose.network.yml"),
            "--profile",
            "*",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=True,
    )
    return cast(dict[str, Any], json.loads(result.stdout))


def test_network_recipe_keeps_pins_and_requires_verified_tls(tmp_path: Path) -> None:
    model = network_model(tmp_path)
    sql = model["services"]["keycloak-database"]
    api = model["services"]["keycloak"]
    assert (
        sql["image"]
        == "postgres@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94"
    )
    assert api["image"] == "aelev/keycloak:26.3.2-production"
    url = api["environment"]["KC_DB_URL"]
    assert url.startswith(
        "jdbc:postgresql://database.identity.example.com:15432/keycloak?"
    )
    assert "sslmode=verify-full" in url
    assert "sslkey=/run/secrets/keycloak-database-client/client.p12" in url
    assert url.endswith("&sslpassword=")
    assert any(port["target"] == 5432 for port in sql["ports"])
    assert {port["target"] for port in api["ports"]} == {8443, 9000}
    assert model["networks"]["keycloak"].get("internal", False) is False
    assert "ssl=on" in sql["command"]


def test_network_identity_operation_is_node_local_and_keys_are_separate(
    tmp_path: Path,
) -> None:
    model = network_model(tmp_path)
    services = model["services"]
    operation = services["keycloak-database-identity-preparer"]
    assert operation["network_mode"] == "none"
    assert operation["user"] == "0:0"
    assert operation["image"] == services["keycloak-database"]["image"]
    assert operation["restart"] == "no"
    assert json.loads(operation["labels"]["io.aelev.deployment.recipe-inputs"]) == [
        "compose.yml"
    ]
    assert services["keycloak"]["restart"] == "no"
    sources, _ = recovery.source_map(model)
    assert recovery.transport_profile(model) == "network-mtls"
    assert (
        sources["database-server/server.key"].parent
        != sources["database-client/client.p12"].parent
    )
    assert sources["tls.ca.crt"] == ROOT / "secrets/https/ca.crt"
    api_targets = {mount["target"] for mount in services["keycloak"]["volumes"]}
    assert "/run/secrets/keycloak-database-server" not in api_targets
    assert "/var/lib/keycloak-database-tls" not in api_targets


def test_separate_role_instances_have_disjoint_networks_and_state(
    tmp_path: Path,
) -> None:
    models = [
        network_model(tmp_path, "sql", 81),
        network_model(tmp_path, "api", 82),
        network_model(tmp_path, "publisher", 83),
    ]
    network_sets = [
        {entry["name"] for entry in model["networks"].values()} for model in models
    ]
    volume_sets = [
        {entry["name"] for entry in model["volumes"].values()} for model in models
    ]
    for index in range(len(models)):
        for other in range(index):
            assert network_sets[index].isdisjoint(network_sets[other])
            assert volume_sets[index].isdisjoint(volume_sets[other])


@pytest.mark.parametrize("conflict", ["profile", "missing-client"])
def test_network_restore_rejects_profile_or_identity_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: str
) -> None:
    model = network_model(tmp_path)
    sources, _ = recovery.source_map(model)
    payloads = {"files/" + name: b"synthetic" for name in sources}
    manifest = {
        "transport_profile": "network-mtls",
        "module_id": model["services"]["keycloak-status-publisher"]["environment"][
            "AELEV_INFRASTRUCTURE_KEYCLOAK_UI_MODULE_ID"
        ],
    }
    if conflict == "profile":
        manifest["transport_profile"] = "legacy-local"
    else:
        del payloads["files/database-client/client.p12"]
    monkeypatch.setattr(recovery, "configuration", lambda: model)
    monkeypatch.setattr(recovery, "validated_bundle", lambda _: (manifest, payloads))
    calls: list[tuple[str, ...]] = []

    def compose(*arguments: str, payload: bytes | None = None) -> bytes:
        calls.append(arguments)
        return b""

    monkeypatch.setattr(recovery, "compose", compose)
    with pytest.raises(ValueError, match="profile mismatch|missing required"):
        recovery.restore(tmp_path / "unused")
    assert calls == [("ps", "-q")]
    assert not sources["admin-password"].exists()
    assert not sources["database-client/client.p12"].parent.exists()
