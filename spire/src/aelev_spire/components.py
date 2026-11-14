"""Install one observation role on its own daemon and owner-local storage."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .files import atomic, read
from .network import configure as configure_network
from .network import subnet
from .observation_setup import (
    OWNER,
    PREFIX,
    SECRET_FILES,
    bind_network,
    documents,
    gateway_aliases,
    installation_routes,
)
from .public_state import material

if TYPE_CHECKING:
    from .installation import Installer

ROLES = {"observer", "status-publisher", "telemetry-sender"}
VOLUMES = {
    "OBSERVATION_CONFIG": "observation-configuration",
    "OBSERVER_DATA": "observation",
    "STATUS_DATA": "status",
    "TELEMETRY_DATA": "telemetry",
    "OBSERVER_SECRETS": "observer-secrets",
    "STATUS_SECRETS": "status-secrets",
    "SENDER_SECRETS": "sender-secrets",
    "STATUS_ADMIN": "status-admin",
}


def validate(plan: dict[str, Any], role: str) -> dict[str, Any]:
    if (
        role not in ROLES
        or set(plan) - {"access_subnet"}
        != {
            "owner",
            "schema_version",
            "environment",
            "component",
            "observation",
            "ports",
        }
        or plan.get("owner") != OWNER
        or type(plan.get("schema_version")) is not int
        or plan["schema_version"] != 1
        or plan.get("component") != role
        or plan.get("environment") not in {"local", "test", "production"}
    ):
        raise ValueError("SPIRE_COMPONENT_PLAN_INVALID")
    selected = plan["observation"]
    if not isinstance(selected, dict):
        raise ValueError("SPIRE_COMPONENT_PLAN_INVALID")
    observation = cast(dict[str, Any], selected)
    subnet(plan.get("access_subnet"))
    if "network" not in observation:
        raise ValueError("SPIRE_COMPONENT_NETWORK_REQUIRED")
    documents(observation)
    if plan["environment"] == "production" and (
        gateway_aliases(observation)
        or not re.fullmatch(r".+@sha256:[0-9a-f]{64}", observation["sender_image"])
    ):
        raise ValueError("SPIRE_COMPONENT_PRODUCTION_ADMISSION_REQUIRED")
    raw_ports = plan["ports"]
    if not isinstance(raw_ports, dict):
        raise ValueError("SPIRE_COMPONENT_PORTS_INVALID")
    ports = cast(dict[str, Any], raw_ports)
    fields: set[str] = {
        "observer": {"bind_address", "https"},
        "status-publisher": set[str](),
        "telemetry-sender": {"bind_address", "otlp", "health"},
    }[role]
    if set(ports) != fields:
        raise ValueError("SPIRE_COMPONENT_PORTS_INVALID")
    if fields:
        if not isinstance(ports["bind_address"], str):
            raise ValueError("SPIRE_COMPONENT_PORTS_INVALID")
        ipaddress.ip_address(ports["bind_address"])
        numbers = [ports[name] for name in fields - {"bind_address"}]
        if any(
            type(number) is not int or not 1 <= number <= 65535 for number in numbers
        ) or len(set(numbers)) != len(numbers):
            raise ValueError("SPIRE_COMPONENT_PORTS_INVALID")
    return observation


def render(
    installer: Installer, role: str, plan: dict[str, Any], *, provision: bool
) -> dict[str, Any]:
    observation = validate(plan, role)
    values = {
        PREFIX + "COMPOSE_PROJECT": installer.project,
        PREFIX + "IMAGE": installer.image,
        PREFIX + "SENDER_IMAGE": observation["sender_image"],
        PREFIX + "ACCESS_NETWORK": installer.project + "-access",
    }
    values.update(
        {
            PREFIX + key + "_VOLUME": installer.project + "_" + name
            for key, name in VOLUMES.items()
        }
    )
    values.update(
        {
            PREFIX + "UI_" + name.upper(): value
            for name, value in observation["presentation"].items()
        }
    )
    model = installer.resolve("compose.observation.yml", values)
    bind_network(model, observation)
    model["volumes"]["spire-certificate-renewal-status"] = {
        "name": installer.project + "_certificate-renewal-status"
    }
    target = "spire-" + role
    selected = {target}
    if role == "status-publisher" and provision:
        selected.add("spire-status-provision")
    model["services"] = {
        name: service for name, service in model["services"].items() if name in selected
    }
    used = {
        mount["source"]
        for service in model["services"].values()
        for mount in service["volumes"]
        if mount["type"] == "volume"
    }
    model["volumes"] = {
        name: definition
        for name, definition in model["volumes"].items()
        if name in used
    }
    network = model["networks"]["spire-observation-access"]
    network.pop("external", None)
    configure_network(model, "spire-observation-access", plan.get("access_subnet"))
    network["labels"] = {
        "io.aelev.owner": OWNER,
        "io.aelev.installation": installer.project,
        "io.aelev.role": "access",
    }
    ports = plan["ports"]
    if ports:
        mappings = (
            [(8083, "https")]
            if role == "observer"
            else [(4318, "otlp"), (13134, "health")]
        )
        model["services"][target]["ports"] = [
            {
                "target": internal,
                "published": str(ports[name]),
                "host_ip": ports["bind_address"],
                "protocol": "tcp",
            }
            for internal, name in mappings
        ]
    routes = installation_routes(installer, gateway_aliases(observation))
    revision = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    for service in model["services"].values():
        service["labels"] = {
            "io.aelev.owner": OWNER,
            "io.aelev.installation": installer.project,
            "io.aelev.component": role,
            "io.aelev.configuration-revision": revision,
        }
        if routes:
            service["extra_hosts"] = routes
    return model


def deliver(
    installer: Installer, volume: str, role: str, files: dict[str, bytes]
) -> None:
    installer.docker(
        "run",
        "--rm",
        "-i",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "128m",
        "--pids-limit",
        "64",
        "--entrypoint",
        "python",
        "-v",
        volume + ":/var/lib/spire",
        installer.image,
        "-m",
        "aelev_spire.observation_setup",
        "seed",
        payload=json.dumps(
            {
                "role": role,
                "update": installer.update,
                "files": {
                    name: base64.b64encode(raw).decode() for name, raw in files.items()
                },
            }
        ).encode(),
    )


def existing(installer: Installer, role: str) -> list[dict[str, Any]]:
    ids = (
        installer.docker(
            "ps",
            "--all",
            "--filter",
            "label=io.aelev.installation=" + installer.project,
            "--filter",
            "label=com.docker.compose.service=spire-" + role,
            "--format",
            "{{.ID}}",
        )
        .decode()
        .splitlines()
    )
    if len(ids) > 1:
        raise ValueError("SPIRE_COMPONENT_RUNTIME_AMBIGUOUS")
    records: list[dict[str, Any]] = (
        json.loads(installer.docker("inspect", *ids)) if ids else []
    )
    if len(records) != len(ids):
        raise ValueError("SPIRE_COMPONENT_RUNTIME_AMBIGUOUS")
    for record in records:
        labels = record["Config"].get("Labels", {})
        if any(
            labels.get(key) != value
            for key, value in {
                "io.aelev.owner": OWNER,
                "io.aelev.installation": installer.project,
                "io.aelev.component": role,
                "com.docker.compose.project": installer.compose_project,
                "com.docker.compose.service": "spire-" + role,
            }.items()
        ):
            raise ValueError("SPIRE_COMPONENT_FOREIGN_RUNTIME_REFUSED")
        if record["State"]["Status"] != "running":
            raise ValueError("SPIRE_COMPONENT_STOPPED_REQUIRES_OPERATOR_REVIEW")
    return records


def runtime_image(installer: Installer, reference: str) -> str:
    try:
        raw = installer.docker("image", "inspect", reference, "--format", "{{.Id}}")
    except ValueError:
        if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", reference):
            raise ValueError("SPIRE_COMPONENT_IMAGE_UNAVAILABLE") from None
        installer.docker("pull", reference)
        raw = installer.docker("image", "inspect", reference, "--format", "{{.Id}}")
    image = raw.decode().strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("SPIRE_COMPONENT_IMAGE_UNAVAILABLE")
    return image


def install(
    installer: Installer, role: str, plan: dict[str, Any], source: Path
) -> None:
    observation = validate(plan, role)
    if plan["environment"] == "production" and not re.fullmatch(
        r".+@sha256:[0-9a-f]{64}", installer.image
    ):
        raise ValueError("SPIRE_COMPONENT_PRODUCTION_ADMISSION_REQUIRED")
    saved = installer.root / "component-deployment.json"
    if (installer.root / "deployment.json").exists() and not saved.exists():
        raise ValueError("SPIRE_COMPONENT_EXISTING_INSTALLATION_PRESERVED")
    expected = {
        "owner": OWNER,
        "role": role,
        "plan": plan,
        "image": installer.image,
        "image_id": installer.image_id,
    }
    records = existing(installer, role)
    if saved.exists():
        if read(saved) != expected:
            # Technical configuration changes require a separately reviewed
            # relocation/update transaction, never an implicit reinstallation.
            raise ValueError("SPIRE_COMPONENT_EXISTING_INSTALLATION_PRESERVED")
        if not records:
            raise ValueError("SPIRE_COMPONENT_RUNTIME_RECONCILIATION_REQUIRED")
    elif records:
        raise ValueError("SPIRE_COMPONENT_EXISTING_INSTALLATION_PRESERVED")
    provision = (
        role == "status-publisher"
        and not (installer.root / "component-provisioned.json").exists()
    )
    model = render(installer, role, plan, provision=provision)
    from .sender_recovery import apply_override, require_running

    if role in {"observer", "telemetry-sender"}:
        recovery_role = "observer" if role == "observer" else "sender"
        require_running(installer, role=recovery_role)
        apply_override(
            installer.root, model["services"]["spire-" + role], role=recovery_role
        )
    image_id = runtime_image(installer, model["services"]["spire-" + role]["image"])
    revision = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    if records and (
        records[0]["Image"] != image_id
        or records[0]["Config"]["Labels"].get("io.aelev.configuration-revision")
        != revision
    ):
        raise ValueError("SPIRE_COMPONENT_RUNTIME_DRIFT")
    for name, definition in model["volumes"].items():
        suffix = definition["name"].removeprefix(installer.project + "_")
        volume = installer.volume(suffix)
        if volume != definition["name"]:
            raise ValueError("SPIRE_COMPONENT_VOLUME_INVALID")
        model["volumes"][name] = {"name": volume, "external": True}
    sets = {
        "observer": [("observer", "OBSERVER_SECRETS")],
        "status-publisher": [("status", "STATUS_SECRETS")],
        "telemetry-sender": [("sender", "SENDER_SECRETS")],
    }[role]
    if provision:
        sets.append(("status-admin", "STATUS_ADMIN"))
    for kind, key in sets:
        inputs = source / kind
        if not inputs.exists() and saved.exists():
            continue
        files = {
            name: material(
                inputs / name,
                private=name.endswith(".key") or name in {"admin-password", "token"},
            )
            for name in SECRET_FILES[kind]
        }
        deliver(installer, installer.project + "_" + VOLUMES[key], kind, files)
    if role == "telemetry-sender":
        deliver(
            installer,
            installer.project + "_observation-configuration",
            "configuration",
            documents(observation),
        )
    state_name = {
        "observer": "observation",
        "status-publisher": "status",
        "telemetry-sender": "telemetry",
    }[role]
    installer.docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "128m",
        "--pids-limit",
        "64",
        "--entrypoint",
        "python",
        "-v",
        installer.project + "_" + state_name + ":/var/lib/spire",
        installer.image,
        "-c",
        "from pathlib import Path; import sys; "
        "from aelev_spire.files import reserve; "
        "reserve(Path('/var/lib/spire'), "
        "{'owner':'infrastructure-spire','role':sys.argv[1]})",
        state_name,
    )
    installer.preserve_restart_policies(model)
    path = installer.root / "component-compose.json"
    atomic(path, model)
    atomic(saved, expected)
    atomic(installer.root / "deployment.json", expected)
    common = (
        "compose",
        "--env-file",
        os.devnull,
        "-p",
        installer.compose_project,
        "-f",
        str(path),
    )
    if provision:
        installer.docker(*common, "run", "--rm", "--no-deps", "spire-status-provision")
        atomic(
            installer.root / "component-provisioned.json",
            {
                "owner": OWNER,
                "component": role,
                "module_id": observation["presentation"]["module_id"],
            },
        )
    if not records:
        installer.docker(
            *common,
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "90",
            "spire-" + role,
        )
    records = existing(installer, role)
    if (
        not records
        or records[0]["Image"] != image_id
        or records[0]["Config"]["Labels"].get("io.aelev.configuration-revision")
        != revision
        or records[0]["State"].get("Health", {}).get("Status") != "healthy"
    ):
        raise ValueError("SPIRE_COMPONENT_RUNTIME_NOT_HEALTHY")
    atomic(
        installer.root / "component-result.json",
        {
            "owner": OWNER,
            "role": role,
            "container_id": records[0]["Id"],
            "image": records[0]["Image"],
            "configuration_sha256": hashlib.sha256(
                json.dumps(plan, sort_keys=True).encode()
            ).hexdigest(),
            "checked_at": time.time(),
            "runtime_healthy": True,
            "ready": False,
            "status": "COMPONENT_RUNNING_AWAITING_EXTERNAL_ACCEPTANCE",
        },
    )
