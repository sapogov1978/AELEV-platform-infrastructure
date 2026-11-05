"""Read-only export of an installed provider, never a second deployment plan."""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .files import atomic, read

OWNER = "infrastructure-spire"
PREFIX = "AELEV_INFRASTRUCTURE_SPIRE_"
ROLES = {
    "spire-server": ("compose.json", ["server"]),
    "spire-observer": ("observation-compose.json", ["serve"]),
    "spire-status-publisher": ("observation-compose.json", ["publish"]),
    "spire-telemetry-sender": (
        "observation-compose.json",
        ["run", "--config", "/etc/aelev/sender.json"],
    ),
}
FIELDS = {
    "image",
    "command",
    "entrypoint",
    "environment",
    "labels",
    "restart",
    "read_only",
    "cap_drop",
    "security_opt",
    "tmpfs",
    "pids_limit",
    "mem_limit",
    "stop_grace_period",
    "cpus",
    "healthcheck",
    "volumes",
    "networks",
    "ports",
    "profiles",
    "extra_hosts",
}
PUBLIC_ENV = {
    "OBSERVATION_ROOT",
    "OTLP_ENDPOINT",
    "UI_URL",
    "UI_IDENTITY_URL",
    "UI_REALM",
    "UI_CLIENT_ID",
    "UI_AUDIENCE",
    "UI_MODULE_ID",
    "UI_INTERVAL_SECONDS",
    "UI_TIMEOUT_SECONDS",
}


def docker(*arguments: str) -> Any:
    """Issue only fixed read commands; never persist full inspect output."""
    result = subprocess.run(
        ["docker", *arguments], capture_output=True, timeout=30, check=False
    )
    if result.returncode or len(result.stdout) > 2 * 1024**2:
        raise ValueError("SPIRE_COMPOSITION_DOCKER_READ_FAILED")
    if arguments[:1] == ("ps",):
        return result.stdout.decode().splitlines()
    if arguments[:1] == ("info",):
        return result.stdout.decode().strip()
    return json.loads(result.stdout)


def _public_service(service: dict[str, Any], role: str) -> dict[str, Any]:
    if set(service) - FIELDS or not service.get("read_only"):
        raise ValueError("SPIRE_COMPOSITION_SERVICE_UNSUPPORTED")
    expected = ROLES[role][1]
    if service.get("command") != expected and not (
        role == "spire-server"
        and service.get("command") == ["server", "--bootstrap", "/run/authority"]
    ):
        raise ValueError("SPIRE_COMPOSITION_COMMAND_UNSUPPORTED")
    entrypoint = (
        ["python", "-m", "aelev_spire"]
        if role == "spire-server"
        else (
            ["python", "/opt/aelev/delivery.py"]
            if role == "spire-telemetry-sender"
            else ["python", "-m", "aelev_spire.observation"]
        )
    )
    if service.get("entrypoint") != entrypoint:
        raise ValueError("SPIRE_COMPOSITION_COMMAND_UNSUPPORTED")
    for key, value in service.get("labels", {}).items():
        if (
            key
            not in {
                "io.aelev.owner",
                "io.aelev.installation",
                "io.aelev.configuration-revision",
                "io.aelev.sender-recovery",
                "io.aelev.observer-recovery",
            }
            or not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
        ):
            raise ValueError("SPIRE_COMPOSITION_PRIVATE_LABEL_REFUSED")
    for key, value in service.get("environment", {}).items():
        if not key.startswith(PREFIX) or key[len(PREFIX) :] not in PUBLIC_ENV:
            raise ValueError("SPIRE_COMPOSITION_SECRET_ENV_REFUSED")
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise ValueError("SPIRE_COMPOSITION_PUBLIC_VALUE_INVALID")
        if key.endswith(("URL", "ENDPOINT")):
            url = urlsplit(value)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("SPIRE_COMPOSITION_PUBLIC_VALUE_INVALID")
        elif not re.fullmatch(r"[A-Za-z0-9._:/-]+", value):
            raise ValueError("SPIRE_COMPOSITION_PUBLIC_VALUE_INVALID")
    result = copy.deepcopy(service)
    # Administrative one-shot services are deliberately not part of this export.
    result["image"] = ""  # Filled only by a verified immutable installed image.
    return result


def remaining_restart_policy(value: dict[str, Any], native: int) -> str:
    if type(native) is not int or native < 0:
        raise ValueError("SPIRE_COMPOSITION_RESTART_HISTORY_INVALID")
    if value == {"Name": "no", "MaximumRetryCount": 0}:
        return "no"
    limit = value.get("MaximumRetryCount")
    if (
        value.get("Name") != "on-failure"
        or type(limit) is not int
        or not 1 <= limit <= 3
    ):
        raise ValueError("SPIRE_COMPOSITION_UNBOUNDED_RESTART_REFUSED")
    remaining = max(0, limit - native)
    return "on-failure:" + str(remaining) if remaining else "no"


def export(root: Path, project: str, daemon: str) -> dict[str, Any]:
    """Verify own saved plans against two live reads and export public bindings."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", project):
        raise ValueError("SPIRE_COMPOSITION_PROJECT_INVALID")
    if docker("info", "--format", "{{.ID}}") != daemon:
        raise ValueError("SPIRE_COMPOSITION_DAEMON_MISMATCH")
    marker = read(root / "owner.json")
    if (
        any(
            marker.get(key) != value
            for key, value in {
                "owner": OWNER,
                "role": "installer",
                "project": project,
                "daemon_id": daemon,
            }.items()
        )
        or (root / ".restore-incomplete").exists()
    ):
        raise ValueError("SPIRE_COMPOSITION_OWNER_MISMATCH")
    files = ["owner.json", "compose.json", "observation-compose.json"]
    for recovery_role in ("sender", "observer"):
        if (
            marker.get(recovery_role + "_recovery_schema") == 1
            and not (root / (recovery_role + "-recovery.json")).is_file()
        ):
            raise ValueError("SPIRE_COMPOSITION_RECOVERY_MISMATCH")
    files += [
        role + "-recovery.json"
        for role in ("sender", "observer")
        if (root / (role + "-recovery.json")).exists()
    ]
    snapshot = {name: read(root / name) for name in files}
    models = {
        name: value for name, value in snapshot.items() if name.endswith("compose.json")
    }
    result: dict[str, Any] = {
        "owner": OWNER,
        "schema_version": 1,
        "project": project,
        "daemon_id": daemon,
    }
    exported: dict[str, Any] = {}
    receipts: dict[str, Any] = {}
    for role, (file, _command) in ROLES.items():
        model = models[file]
        if model.get("name") != project:
            raise ValueError("SPIRE_COMPOSITION_OWNER_MISMATCH")
        source = copy.deepcopy(model["services"][role])
        image = docker("image", "inspect", source["image"])[0]
        if source.get("entrypoint") is None:
            source["entrypoint"] = image["Config"].get("Entrypoint")
        public = _public_service(source, role)
        ids = docker(
            "ps",
            "-aq",
            "--filter",
            "label=io.aelev.installation=" + project,
            "--filter",
            "label=com.docker.compose.service=" + role,
        )
        if len(ids) != 1:
            raise ValueError("SPIRE_COMPOSITION_EXACT_CONTAINER_REQUIRED")
        row = docker("inspect", ids[0])[0]
        labels = row["Config"]["Labels"]
        if (
            labels.get("io.aelev.owner") != OWNER
            or labels.get("io.aelev.installation") != project
        ):
            raise ValueError("SPIRE_COMPOSITION_FOREIGN_CONTAINER_REFUSED")
        if any(
            labels.get(key) != value for key, value in source.get("labels", {}).items()
        ):
            raise ValueError("SPIRE_COMPOSITION_LABEL_MISMATCH")
        environment = {
            **dict(value.split("=", 1) for value in image["Config"].get("Env", [])),
            **source.get("environment", {}),
        }
        if (
            row["Image"] != image["Id"]
            or dict(value.split("=", 1) for value in row["Config"]["Env"])
            != environment
        ):
            raise ValueError("SPIRE_COMPOSITION_RELEASE_MISMATCH")
        if (
            row["Config"]["Cmd"] != source["command"]
            or row["Config"]["Entrypoint"] != source["entrypoint"]
        ):
            raise ValueError("SPIRE_COMPOSITION_COMMAND_MISMATCH")
        if not row["HostConfig"]["ReadonlyRootfs"] or row["HostConfig"].get(
            "Privileged"
        ):
            raise ValueError("SPIRE_COMPOSITION_SECURITY_MISMATCH")
        if (
            set(row["HostConfig"].get("CapDrop", [])) != set(source.get("cap_drop", []))
            or set(row["HostConfig"].get("SecurityOpt", []))
            != set(source.get("security_opt", []))
            or row["Config"].get("Healthcheck", {}).get("Test")
            != source.get("healthcheck", {}).get("test")
        ):
            raise ValueError("SPIRE_COMPOSITION_SECURITY_MISMATCH")
        public["image"] = row["Image"]
        public["restart"] = remaining_restart_policy(
            row["HostConfig"]["RestartPolicy"], row["RestartCount"]
        )
        for recovery_role in ("sender", "observer"):
            if role != (
                "spire-telemetry-sender"
                if recovery_role == "sender"
                else "spire-observer"
            ):
                continue
            path = root / (recovery_role + "-recovery.json")
            if path.exists():
                recovery = read(path)
                if (
                    recovery.get("owner") != OWNER
                    or recovery.get("state") != "COMPLETE"
                    or marker.get(recovery_role + "_recovery_schema") != 1
                    or recovery.get("restart") != "no"
                    or public["restart"] != "no"
                ):
                    raise ValueError("SPIRE_COMPOSITION_RECOVERY_MISMATCH")
        selected: dict[str, Any] = {
            "services": {role: public},
            "volumes": {},
            "networks": {},
        }
        mounts = {mount["Destination"]: mount for mount in row["Mounts"]}
        if len(mounts) != len(source.get("volumes", [])):
            raise ValueError("SPIRE_COMPOSITION_MOUNT_MISMATCH")
        for mount in source.get("volumes", []):
            actual = mounts.get(mount["target"], {})
            name = model.get("volumes", {}).get(mount["source"], {}).get("name")
            if mount["type"] == "volume":
                volume = docker("volume", "inspect", name)[0]
                if (
                    volume.get("Labels", {}).get("io.aelev.owner") != OWNER
                    or volume.get("Labels", {}).get("io.aelev.installation") != project
                ):
                    raise ValueError("SPIRE_COMPOSITION_FOREIGN_VOLUME_REFUSED")
                selected["volumes"][mount["source"]] = {"name": name, "external": True}
                matches = actual.get("Type") == "volume" and actual.get("Name") == name
            else:
                matches = (
                    mount["type"] == "bind"
                    and actual.get("Type") == "bind"
                    and actual.get("Source") == mount["source"]
                )
            if not matches or actual.get("RW") != (not mount.get("read_only", False)):
                raise ValueError("SPIRE_COMPOSITION_MOUNT_MISMATCH")
        for key in source.get("networks", {}):
            name = model["networks"][key]["name"]
            if name not in row["NetworkSettings"]["Networks"]:
                raise ValueError("SPIRE_COMPOSITION_NETWORK_MISMATCH")
            selected["networks"][key] = {"name": name, "external": True}
        if set(row["NetworkSettings"]["Networks"]) != {
            definition["name"] for definition in selected["networks"].values()
        }:
            raise ValueError("SPIRE_COMPOSITION_NETWORK_MISMATCH")
        again = docker("inspect", row["Id"])[0]
        if any(
            again[key] != row[key]
            for key in ("Id", "Image", "RestartCount", "Config", "HostConfig")
        ) or sorted(again["Mounts"], key=lambda item: item["Destination"]) != sorted(
            row["Mounts"], key=lambda item: item["Destination"]
        ):
            raise ValueError("SPIRE_COMPOSITION_CHANGED_DURING_EXPORT")
        receipts[role] = {
            "container_id": row["Id"],
            "image": row["Image"],
            "native_restarts": row["RestartCount"],
            "restart": public["restart"],
            "compose_project": labels.get("com.docker.compose.project"),
        }
        exported[role] = selected
    if any(read(root / name) != data for name, data in snapshot.items()):
        raise ValueError("SPIRE_COMPOSITION_CHANGED_DURING_EXPORT")
    projects = {row["compose_project"] for row in receipts.values()}
    if len(projects) != 1 or not all(
        isinstance(project, str) and re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", project)
        for project in projects
    ):
        raise ValueError("SPIRE_COMPOSITION_COORDINATOR_MIXED")
    if docker("info", "--format", "{{.ID}}") != daemon:
        raise ValueError("SPIRE_COMPOSITION_DAEMON_MISMATCH")
    result["models"], result["roles"] = exported, receipts
    return result


def publish(value: dict[str, Any], output: Path) -> None:
    """Publish one immutable generation; caller activates both references together."""
    raw = json.dumps(value, sort_keys=True).encode()
    generation = hashlib.sha256(raw).hexdigest()
    destination = output / generation
    destination.mkdir(parents=True, exist_ok=True, mode=0o755)
    models: dict[str, Any] = {}
    for model in value["models"].values():
        for section in ("services", "volumes", "networks"):
            for key, definition in model[section].items():
                current = models.setdefault(section, {})
                if key in current and current[key] != definition:
                    raise ValueError("SPIRE_COMPOSITION_GENERATION_CONFLICT")
                current[key] = definition
    # One file makes a mixed server/observation generation impossible.
    path = destination / "compose.json"
    if path.exists() and read(path, 262144) != models:
        raise ValueError("SPIRE_COMPOSITION_GENERATION_CONFLICT")
    if not path.exists():
        atomic(path, models, mode=0o644)
    atomic(
        output / "receipt.json",
        {**value["roles"], "generation": generation},
        mode=0o644,
    )
    atomic(
        output / "binding.env",
        (
            "# AELEV SPIRE installed composition v1\n"
            "AELEV_INFRASTRUCTURE_SPIRE_INSTALLED_COMPOSE=./runtime/installed-composition/"
            + generation
            + "/compose.json\n"
        ).encode(),
        mode=0o644,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--daemon-id", required=True)
    parser.add_argument("--root", type=Path, default=Path("/installation"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (args.root / "installation.lock").is_symlink():
        raise ValueError("SPIRE_COMPOSITION_LOCK_INVALID")
    with (args.root / "installation.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        value = export(args.root, args.project, args.daemon_id)
        publish(value, args.output)


if __name__ == "__main__":
    main()
