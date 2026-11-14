"""Installed bindings preserve ownership, exact releases and spent budgets."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from aelev_spire import composition

PROJECT = "fixture-spire"
DAEMON = "fixture-daemon"


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    marker = {
        "owner": composition.OWNER,
        "role": "installer",
        "project": PROJECT,
        "daemon_id": DAEMON,
        "sender_recovery_schema": 1,
        "observer_recovery_schema": 1,
    }
    (tmp_path / "owner.json").write_text(json.dumps(marker))
    models: dict[str, Any] = {
        name: {
            "name": PROJECT,
            "services": {},
            "volumes": {},
            "networks": {"access": {"name": PROJECT + "-access"}},
        }
        for name in ("compose.json", "observation-compose.json")
    }
    rows: dict[str, Any] = {}
    images: dict[str, Any] = {}
    volumes: dict[str, Any] = {}
    for number, (role, (file, command)) in enumerate(composition.ROLES.items(), 1):
        image = "sha256:" + str(number) * 64
        entrypoint = (
            ["python", "-m", "aelev_spire"]
            if role == "spire-server"
            else (
                ["python", "/opt/aelev/delivery.py"]
                if role == "spire-telemetry-sender"
                else ["python", "-m", "aelev_spire.observation"]
            )
        )
        restart = (
            "no"
            if role in ("spire-observer", "spire-telemetry-sender")
            else "on-failure:3"
        )
        service: dict[str, Any] = {
            "image": image,
            "entrypoint": entrypoint,
            "command": command,
            "environment": {},
            "restart": restart,
            "read_only": True,
            "labels": {
                "io.aelev.owner": composition.OWNER,
                "io.aelev.installation": PROJECT,
            },
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "healthcheck": {"test": ["CMD", "python", "-S", "fixture-health.py"]},
            "networks": {"access": None},
            "volumes": [
                {
                    "type": "volume",
                    "source": role,
                    "target": "/var/lib/state",
                    "read_only": False,
                }
            ],
        }
        models[file]["services"][role] = service
        models[file]["volumes"][role] = {"name": PROJECT + "_" + role}
        volume_name = PROJECT + "_" + role
        volumes[volume_name] = {
            "Labels": {
                "io.aelev.owner": composition.OWNER,
                "io.aelev.installation": PROJECT,
            }
        }
        rows[role] = {
            "Id": str(number) * 64,
            "Image": image,
            "RestartCount": 0,
            "Config": {
                "Labels": {
                    **service["labels"],
                    "com.docker.compose.project": PROJECT,
                    "com.docker.compose.service": role,
                },
                "Cmd": command,
                "Entrypoint": entrypoint,
                "Env": ["PATH=/fixed"],
                "Healthcheck": {"Test": service["healthcheck"]["test"]},
            },
            "HostConfig": {
                "ReadonlyRootfs": True,
                "Privileged": False,
                "CapDrop": service["cap_drop"],
                "SecurityOpt": service["security_opt"],
                "RestartPolicy": {
                    "Name": "no" if restart == "no" else "on-failure",
                    "MaximumRetryCount": 0 if restart == "no" else 3,
                },
            },
            "Mounts": [
                {
                    "Type": "volume",
                    "Destination": "/var/lib/state",
                    "Name": volume_name,
                    "RW": True,
                }
            ],
            "NetworkSettings": {"Networks": {PROJECT + "-access": {}}},
        }
        images[image] = {
            "Id": image,
            "Config": {"Env": ["PATH=/fixed"], "Entrypoint": entrypoint},
        }
    for name, model in models.items():
        (tmp_path / name).write_text(json.dumps(model))
    for role in ("observer", "sender"):
        (tmp_path / (role + "-recovery.json")).write_text(
            json.dumps(
                {
                    "owner": composition.OWNER,
                    "restart": "no",
                    "spent": 3,
                    "state": "COMPLETE",
                }
            )
        )
    calls: list[tuple[str, ...]] = []

    def docker(*arguments: str) -> Any:
        calls.append(arguments)
        if arguments[0] == "info":
            return DAEMON
        if arguments[0] == "ps":
            return [arguments[-1].split("=", 2)[-1]]
        if arguments[0] == "inspect":
            selected = rows.get(arguments[1])
            if selected is None:
                selected = next(
                    row for row in rows.values() if row["Id"] == arguments[1]
                )
            return [copy.deepcopy(selected)]
        if arguments[:2] == ("image", "inspect"):
            return [copy.deepcopy(images[arguments[2]])]
        assert arguments[:2] == ("volume", "inspect")
        return [copy.deepcopy(volumes[arguments[2]])]

    monkeypatch.setattr(composition, "docker", docker)
    return {
        "root": tmp_path,
        "rows": rows,
        "models": models,
        "calls": calls,
        "volumes": volumes,
    }


def test_export_is_read_only_and_keeps_all_four_releases(
    installed: dict[str, Any],
) -> None:
    value = composition.export(installed["root"], PROJECT, DAEMON)
    assert len(value["models"]) == 4
    assert len({role["image"] for role in value["roles"].values()}) == 4
    assert value["roles"]["spire-observer"]["restart"] == "no"
    assert value["roles"]["spire-telemetry-sender"]["restart"] == "no"
    assert all(
        call[0] in ("info", "ps", "inspect", "image", "volume")
        for call in installed["calls"]
    )
    assert all(
        volume["external"]
        for model in value["models"].values()
        for volume in model["volumes"].values()
    )


@pytest.mark.parametrize("component", composition.COMPONENTS)
def test_independent_export_does_not_require_other_roles(
    installed: dict[str, Any], component: str
) -> None:
    root = installed["root"]
    role = composition.COMPONENTS[component]
    original = composition.ROLES[role][0]
    model = copy.deepcopy(installed["models"][original])
    model["services"] = {role: model["services"][role]}
    if component != "server":
        model["services"][role]["labels"]["io.aelev.component"] = component
        installed["rows"][role]["Config"]["Labels"]["io.aelev.component"] = component
    for name in ("compose.json", "observation-compose.json"):
        (root / name).unlink()
    target = "compose.json" if component == "server" else "component-compose.json"
    (root / target).write_text(json.dumps(model))
    (root / "component-deployment.json").write_text(
        json.dumps({"owner": composition.OWNER, "role": component})
    )
    value = composition.export(root, PROJECT, DAEMON, component)
    assert set(value["models"]) == set(value["roles"]) == {role}
    assert value["component"] == component
    assert all(
        call[-1] == "label=com.docker.compose.service=" + role
        for call in installed["calls"]
        if call[0] == "ps"
    )
    output = root / "public"
    composition.publish(value, output)
    receipt = json.loads((output / "receipt.json").read_text())
    assert set(receipt) == {role, "generation"}
    public = json.loads((output / receipt["generation"] / "compose.json").read_text())
    assert set(public["services"]) == {role}


@pytest.mark.parametrize("component", ["unknown", "../server", "", "all-roles"])
def test_invalid_component_fails_before_docker(
    installed: dict[str, Any], component: str
) -> None:
    with pytest.raises(ValueError, match="SPIRE_COMPOSITION_COMPONENT_INVALID"):
        composition.export(installed["root"], PROJECT, DAEMON, component)
    assert not installed["calls"]


def test_component_deployment_cannot_impersonate_another_role(
    installed: dict[str, Any],
) -> None:
    root = installed["root"]
    model = installed["models"]["observation-compose.json"]
    (root / "component-compose.json").write_text(json.dumps(model))
    (root / "component-deployment.json").write_text(
        json.dumps({"owner": composition.OWNER, "role": "status-publisher"})
    )
    with pytest.raises(ValueError, match="SPIRE_COMPOSITION_COMPONENT_MISMATCH"):
        composition.export(root, PROJECT, DAEMON, "observer")


@pytest.mark.parametrize("fault", ["none", "port", "bind", "extra-port", "route"])
def test_export_preserves_actual_published_ports_and_aliases(
    installed: dict[str, Any], fault: str
) -> None:
    model = installed["models"]["compose.json"]
    service = model["services"]["spire-server"]
    service["ports"] = [
        {"target": 8084, "published": "18443", "host_ip": "192.168.10.4"}
    ]
    service["extra_hosts"] = {"source.example": "192.168.10.4"}
    (installed["root"] / "compose.json").write_text(json.dumps(model))
    host = installed["rows"]["spire-server"]["HostConfig"]
    host["PortBindings"] = {
        "8084/tcp": [{"HostIp": "192.168.10.4", "HostPort": "18443"}]
    }
    host["ExtraHosts"] = ["source.example:192.168.10.4"]
    if fault == "port":
        host["PortBindings"]["8084/tcp"][0]["HostPort"] = "18444"
    elif fault == "bind":
        host["PortBindings"]["8084/tcp"][0]["HostIp"] = "127.0.0.1"
    elif fault == "extra-port":
        host["PortBindings"]["9999/tcp"] = [{"HostIp": "", "HostPort": "9999"}]
    elif fault == "route":
        host["ExtraHosts"] = ["source.example:192.168.10.5"]
    if fault == "none":
        exported = composition.export(installed["root"], PROJECT, DAEMON)
        actual = exported["models"]["spire-server"]["services"]["spire-server"]
        assert actual["ports"] == service["ports"]
        assert actual["extra_hosts"] == service["extra_hosts"]
    else:
        with pytest.raises(ValueError, match="SPIRE_COMPOSITION_(PORT|ROUTE)_MISMATCH"):
            composition.export(installed["root"], PROJECT, DAEMON)


@pytest.mark.parametrize("role", composition.ROLES)
def test_only_role_specific_public_network_environment_is_exported(
    installed: dict[str, Any], role: str
) -> None:
    root = installed["root"]
    filename = composition.ROLES[role][0]
    model = installed["models"][filename]
    service = model["services"][role]
    own = {
        "spire-server": {
            "PUBLIC_SOURCE_MODE": "https",
            "PUBLIC_SOURCE_IDENTITY": "cn:AELEV SPIRE public-source",
            "PUBLIC_SOURCE_CLIENT_IDENTITIES": "cn:AELEV SPIRE observer,spiffe://test.example/query",
        },
        "spire-observer": {
            "SOURCE_MODE": "https",
            "SOURCE_URL": "https://source.example:8443",
            "SOURCE_IDENTITY": "cn:AELEV SPIRE public-source",
        },
        "spire-status-publisher": {
            "SOURCE_MODE": "https",
            "SOURCE_URL": "https://source.example:8443",
            "SOURCE_IDENTITY": "cn:AELEV SPIRE public-source",
            "OBSERVER_URL": "https://observer.example:8444",
            "SENDER_HEALTH_URL": "https://sender.example:8445",
        },
        "spire-telemetry-sender": {},
    }[role]
    service["environment"] = {composition.PREFIX + k: v for k, v in own.items()}
    installed["rows"][role]["Config"]["Env"] += [
        key + "=" + value for key, value in service["environment"].items()
    ]
    (root / filename).write_text(json.dumps(model))
    exported = composition.export(root, PROJECT, DAEMON)
    assert (
        exported["models"][role]["services"][role]["environment"]
        == service["environment"]
    )
    service["environment"][composition.PREFIX + "TOKEN"] = "private"
    (root / filename).write_text(json.dumps(model))
    with pytest.raises(ValueError, match="SPIRE_COMPOSITION_SECRET_ENV_REFUSED"):
        composition.export(root, PROJECT, DAEMON)


def test_image_entrypoint_inheritance_is_explicit_in_export(
    installed: dict[str, Any],
) -> None:
    model = installed["models"]["compose.json"]
    model["services"]["spire-server"]["entrypoint"] = None
    (installed["root"] / "compose.json").write_text(json.dumps(model))
    value = composition.export(installed["root"], PROJECT, DAEMON)
    assert value["models"]["spire-server"]["services"]["spire-server"][
        "entrypoint"
    ] == ["python", "-m", "aelev_spire"]


def test_installation_identity_is_independent_of_compose_coordinator(
    installed: dict[str, Any],
) -> None:
    for row in installed["rows"].values():
        row["Config"]["Labels"]["com.docker.compose.project"] = "fixture-aggregate"
    value = composition.export(installed["root"], PROJECT, DAEMON)
    assert all(
        role["compose_project"] == "fixture-aggregate"
        for role in value["roles"].values()
    )
    assert all(
        "label=io.aelev.installation=" + PROJECT in call
        for call in installed["calls"]
        if call[0] == "ps"
    )


@pytest.mark.parametrize(
    "attempts,expected",
    [(0, "on-failure:3"), (1, "on-failure:2"), (3, "no"), (551, "no")],
)
def test_native_history_never_becomes_fresh_budget(
    attempts: int, expected: str
) -> None:
    assert (
        composition.remaining_restart_policy(
            {"Name": "on-failure", "MaximumRetryCount": 3}, attempts
        )
        == expected
    )


@pytest.mark.parametrize(
    "fault",
    [
        "image",
        "environment",
        "mount",
        "network",
        "owner",
        "restart",
        "probe",
        "privileged",
    ],
)
def test_changed_runtime_is_never_exported(
    installed: dict[str, Any], fault: str
) -> None:
    row = installed["rows"]["spire-server"]
    if fault == "image":
        row["Image"] = "sha256:" + "a" * 64
    elif fault == "environment":
        row["Config"]["Env"].append("SECRET=do-not-export")
    elif fault == "mount":
        row["Mounts"][0]["Name"] = "foreign-state"
    elif fault == "network":
        row["NetworkSettings"]["Networks"]["foreign-network"] = {}
    elif fault == "owner":
        row["Config"]["Labels"]["io.aelev.owner"] = "foreign"
    elif fault == "restart":
        row["HostConfig"]["RestartPolicy"] = {
            "Name": "unless-stopped",
            "MaximumRetryCount": 0,
        }
    elif fault == "probe":
        row["Config"]["Healthcheck"]["Test"] = ["CMD", "expensive-import"]
    else:
        row["HostConfig"]["Privileged"] = True
    with pytest.raises(ValueError, match="SPIRE_COMPOSITION_"):
        composition.export(installed["root"], PROJECT, DAEMON)


@pytest.mark.parametrize("field", ["environment", "labels", "env_file", "command"])
def test_private_values_are_not_copied(installed: dict[str, Any], field: str) -> None:
    model = installed["models"]["compose.json"]
    service = model["services"]["spire-server"]
    if field == "environment":
        service[field][composition.PREFIX + "TOKEN"] = "do-not-export"
    elif field == "labels":
        service[field]["credential"] = "do-not-export"
    elif field == "command":
        service[field] = ["server", "--password", "do-not-export"]
    else:
        service[field] = "secret.env"
    (installed["root"] / "compose.json").write_text(json.dumps(model))
    with pytest.raises(ValueError, match="SPIRE_COMPOSITION_"):
        composition.export(installed["root"], PROJECT, DAEMON)


def test_publish_is_repeatable_and_only_references_existing_volumes(
    installed: dict[str, Any], tmp_path: Path
) -> None:
    value = composition.export(installed["root"], PROJECT, DAEMON)
    output = tmp_path / "public"
    composition.publish(value, output)
    before = {
        path.relative_to(output): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    composition.publish(value, output)
    assert before == {
        path.relative_to(output): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    receipt = json.loads((output / "receipt.json").read_text())
    model = json.loads((output / receipt["generation"] / "compose.json").read_text())
    assert set(model["services"]) == set(composition.ROLES)
    assert all(volume["external"] for volume in model["volumes"].values())


@pytest.mark.parametrize("state", ["PREPARED", "MISSING"])
def test_incomplete_recovery_cannot_be_exported(
    installed: dict[str, Any], state: str
) -> None:
    path = installed["root"] / "sender-recovery.json"
    if state == "MISSING":
        path.unlink()
    else:
        value = json.loads(path.read_text())
        value["state"] = state
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="RECOVERY_MISMATCH"):
        composition.export(installed["root"], PROJECT, DAEMON)


def test_nested_root_include_uses_owner_binding(
    installed: dict[str, Any], tmp_path: Path
) -> None:
    binary = shutil.which("docker")
    if binary is None:
        pytest.skip("Compose CLI is checked separately on the operator host")
    owner = tmp_path / "group" / "provider"
    owner.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1]
    for name in (
        "compose.group.yml",
        "compose.default.yml",
        "compose.yml",
        "compose.observation.yml",
    ):
        shutil.copyfile(source / name, owner / name)
    root = owner.parent / "compose.yml"
    root.write_text(
        "name: fixture-root\ninclude:\n"
        "  - path: ./provider/compose.group.yml\n"
        "    project_directory: ./provider\n"
    )
    output = owner / "runtime" / "installed-composition"
    composition.publish(composition.export(installed["root"], PROJECT, DAEMON), output)
    shutil.copyfile(output / "binding.env", owner / ".env")
    result = subprocess.run(
        [
            binary,
            "compose",
            "--env-file",
            os.devnull,
            "--profile",
            "*",
            "-f",
            str(root),
            "config",
            "--format",
            "json",
        ],
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("AELEV_", "COMPOSE_"))
        },
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    model = json.loads(result.stdout)
    assert set(model["services"]) == set(composition.ROLES)
    assert model["services"]["spire-observer"]["restart"] == "no"
    assert all(value["external"] for value in model["volumes"].values())
