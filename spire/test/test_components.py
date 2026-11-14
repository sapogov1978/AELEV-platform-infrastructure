"""Independent role scope and repeat safety, without a working Docker daemon."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from test_observation import plan as observation_plan

from aelev_spire.components import existing, install, render, validate
from aelev_spire.files import atomic
from aelev_spire.installation import Installer


def plan(role: str) -> dict[str, Any]:
    observation = observation_plan()
    observation["network"] = {
        "source_url": "https://identity-node.example:18084",
        "source_identity": "cn:AELEV SPIRE source",
        "observer_url": "https://observer-node.example:18083",
        "sender_otlp_url": "https://sender-node.example:14318",
        "sender_health_url": "https://sender-node.example:13134",
    }
    return {
        "owner": "infrastructure-spire",
        "schema_version": 1,
        "environment": "test",
        "component": role,
        "observation": observation,
        "ports": {
            "observer": {"bind_address": "127.0.0.1", "https": 18083},
            "status-publisher": {},
            "telemetry-sender": {
                "bind_address": "127.0.0.1",
                "otlp": 14318,
                "health": 13134,
            },
        }[role],
    }


def installer(tmp_path: Path) -> Installer:
    result = object.__new__(Installer)
    result.project = "fixture-component"
    result.root, result.image, result.image_id = (
        tmp_path,
        "sha256:" + "a" * 64,
        "sha256:" + "a" * 64,
    )
    result.host_os, result.update = "Linux", False
    model: dict[str, Any] = {
        "services": {},
        "volumes": {},
        "networks": {
            "spire-observation-access": {
                "name": result.project + "-access",
                "external": True,
            }
        },
    }
    for role, name in (
        ("observer", "observation"),
        ("status-publisher", "status"),
        ("telemetry-sender", "telemetry"),
    ):
        mounts: list[dict[str, Any]] = [
            {"type": "volume", "source": name, "target": "/own-state"},
            {
                "type": "volume",
                "source": name + "-secrets",
                "target": "/run/secrets/own",
                "read_only": True,
            },
        ]
        if role != "telemetry-sender":
            mounts += [
                {
                    "type": "volume",
                    "source": "spire-observation-public-source",
                    "target": "/run/observation",
                },
                {
                    "type": "volume",
                    "source": "spire-certificate-renewal-status",
                    "target": "/run/certificate-renewal",
                },
            ]
        model["services"]["spire-" + role] = {
            "image": result.image,
            "environment": {},
            "volumes": mounts,
            "networks": {"spire-observation-access": {}},
            "restart": "on-failure:3",
        }
        for mount in mounts:
            model["volumes"][mount["source"]] = {
                "name": result.project + "_" + mount["source"]
            }
    model["services"]["spire-status-provision"] = {
        "image": result.image,
        "environment": {},
        "volumes": [
            {"type": "volume", "source": "status-secrets", "target": "/own-secret"},
            {"type": "volume", "source": "status-admin", "target": "/own-admin"},
        ],
    }
    model["volumes"]["status-admin"] = {"name": result.project + "_status-admin"}

    def resolve(file: str, values: dict[str, str]) -> dict[str, Any]:
        assert file == "compose.observation.yml"
        assert values["AELEV_INFRASTRUCTURE_SPIRE_COMPOSE_PROJECT"] == result.project
        return copy.deepcopy(model)

    result.resolve = resolve
    return result


@pytest.mark.parametrize("role", ["observer", "status-publisher", "telemetry-sender"])
def test_one_role_recipe_never_requires_another_roles_daemon_or_state(
    tmp_path: Path, role: str
) -> None:
    selected = render(installer(tmp_path), role, plan(role), provision=False)
    assert set(selected["services"]) == {"spire-" + role}
    mounts = selected["services"]["spire-" + role]["volumes"]
    assert all(
        item["target"]
        not in {
            "/run/observation",
            "/run/spire/admin",
            "/var/lib/spire",
        }
        for item in mounts
    )
    assert not selected["networks"]["spire-observation-access"].get("external")
    assert "status-admin" not in selected["volumes"]
    assert selected["volumes"]["spire-certificate-renewal-status"]["name"] == (
        "fixture-component_certificate-renewal-status"
    )
    assert any(
        item["target"] == "/run/certificate-renewal"
        and item["source"] == "spire-certificate-renewal-status"
        for item in mounts
    )
    if role == "observer":
        assert (
            selected["services"]["spire-observer"]["ports"][0]["published"] == "18083"
        )


def test_only_publisher_provision_gets_its_own_admin_secret(tmp_path: Path) -> None:
    selected = render(
        installer(tmp_path),
        "status-publisher",
        plan("status-publisher"),
        provision=True,
    )
    assert set(selected["services"]) == {
        "spire-status-publisher",
        "spire-status-provision",
    }
    assert "status-admin" in selected["volumes"]
    assert all(
        item["source"] != "status-admin"
        for item in selected["services"]["spire-status-publisher"]["volumes"]
    )


def test_component_cannot_silently_select_old_shared_file_mode() -> None:
    selected = plan("observer")
    del selected["observation"]["network"]
    with pytest.raises(ValueError, match="NETWORK_REQUIRED"):
        validate(selected, "observer")


@pytest.mark.parametrize(
    "value",
    [
        "8.8.8.0/24",
        "127.0.0.0/24",
        "10.0.0.1/28",
        "10.0.0.0/8",
        "10.0.0.0/32",
        "203.0.113.0/24",
        "::/64",
        False,
    ],
)
def test_component_ipam_rejects_public_loopback_noncanonical_and_oversized_networks(
    value: object,
) -> None:
    selected = plan("observer")
    selected["access_subnet"] = value
    with pytest.raises(ValueError, match="ACCESS_SUBNET_INVALID"):
        validate(selected, "observer")


def test_exited_component_is_not_started_by_repeated_install(tmp_path: Path) -> None:
    value = installer(tmp_path)
    calls: list[tuple[str, ...]] = []
    record = {
        "Id": "1" * 64,
        "State": {"Status": "exited"},
        "Config": {
            "Labels": {
                "io.aelev.owner": "infrastructure-spire",
                "io.aelev.installation": value.project,
                "io.aelev.component": "observer",
                "com.docker.compose.project": value.project,
                "com.docker.compose.service": "spire-observer",
            }
        },
    }

    def docker(*args: str, payload: bytes | None = None) -> bytes:
        assert payload is None
        calls.append(args)
        if args[0] == "ps":
            return b"fixture-container\n"
        assert args == ("inspect", "fixture-container")
        return json.dumps([record]).encode()

    value.docker = docker
    with pytest.raises(ValueError, match="STOPPED_REQUIRES_OPERATOR_REVIEW"):
        install(value, "observer", plan("observer"), tmp_path)
    assert all(args[0] in {"ps", "inspect"} for args in calls)
    assert not (tmp_path / "component-deployment.json").exists()


def test_grouped_installation_cannot_be_adopted_as_component(tmp_path: Path) -> None:
    atomic(tmp_path / "deployment.json", {"role": "server"})
    with pytest.raises(ValueError, match="EXISTING_INSTALLATION_PRESERVED"):
        install(installer(tmp_path), "observer", plan("observer"), tmp_path)


def test_component_rejects_another_owners_runtime(tmp_path: Path) -> None:
    value = installer(tmp_path)

    def docker(*args: str, payload: bytes | None = None) -> bytes:
        assert payload is None
        return (
            b"fixture\n"
            if args[0] == "ps"
            else json.dumps(
                [{"Config": {"Labels": {"io.aelev.owner": "foreign"}}}]
            ).encode()
        )

    value.docker = docker
    with pytest.raises(ValueError, match="FOREIGN_RUNTIME_REFUSED"):
        existing(value, "observer")
