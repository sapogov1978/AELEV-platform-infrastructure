"""Portable owner-local lifecycle for one Translator compute station."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

LOGGER = logging.getLogger("translator.compute_station")
ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.compute.yml"
PINNED_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


def _execute(arguments: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(
        arguments,
        check=True,
        text=True,
        capture_output=capture,
    )
    return result.stdout.strip() if capture else ""


def _docker() -> str:
    selected = shutil.which("docker")
    if not selected or not Path(selected).is_absolute():
        raise RuntimeError("TRANSLATOR_COMPUTE_LOCAL_DOCKER_REQUIRED")
    return selected


def _local_engine(docker: str) -> tuple[dict[str, Any], str]:
    context = _execute([docker, "context", "show"], capture=True)
    raw = cast(
        object,
        json.loads(_execute([docker, "context", "inspect", context], capture=True)),
    )
    if not isinstance(raw, list):
        raise RuntimeError("TRANSLATOR_COMPUTE_LOCAL_DOCKER_REQUIRED")
    typed_raw = cast(list[object], raw)
    if len(typed_raw) != 1:
        raise RuntimeError("TRANSLATOR_COMPUTE_LOCAL_DOCKER_REQUIRED")
    row_value = typed_raw[0]
    if not isinstance(row_value, dict):
        raise RuntimeError("TRANSLATOR_COMPUTE_LOCAL_DOCKER_REQUIRED")
    row = cast(dict[str, object], row_value)
    endpoints_value = row.get("Endpoints")
    if not isinstance(endpoints_value, dict):
        raise RuntimeError("TRANSLATOR_COMPUTE_LOCAL_DOCKER_REQUIRED")
    endpoints = cast(dict[str, object], endpoints_value)
    docker_value = endpoints.get("docker")
    if not isinstance(docker_value, dict):
        raise RuntimeError("TRANSLATOR_COMPUTE_LOCAL_DOCKER_REQUIRED")
    endpoint = cast(dict[str, object], docker_value).get("Host")
    system = platform.system()
    local = isinstance(endpoint, str) and (
        (system == "Windows" and endpoint.startswith("npipe:////./pipe/"))
        or (
            system != "Windows"
            and re.fullmatch(r"unix:///[^,\r\n\x00]+", endpoint) is not None
        )
    )
    if not local or not isinstance(endpoint, str):
        raise RuntimeError("TRANSLATOR_COMPUTE_REMOTE_DOCKER_FORBIDDEN")
    info = cast(
        object,
        json.loads(
            _execute(
                [docker, "--host", endpoint, "info", "--format", "{{json .}}"],
                capture=True,
            )
        ),
    )
    if not isinstance(info, dict):
        raise RuntimeError("TRANSLATOR_COMPUTE_LINUX_CONTAINERS_REQUIRED")
    typed_info = cast(dict[str, object], info)
    if typed_info.get("OSType") != "linux":
        raise RuntimeError("TRANSLATOR_COMPUTE_LINUX_CONTAINERS_REQUIRED")
    return cast(dict[str, Any], typed_info), endpoint


def _compose(
    docker: str, endpoint: str, env_file: Path, role: str = "bundle"
) -> list[str]:
    if role not in {"bundle", "provider", "worker"}:
        raise RuntimeError("TRANSLATOR_COMPUTE_ROLE_INVALID")
    recipe = COMPOSE if role == "bundle" else ROOT / f"compose.compute-{role}.mtls.yml"
    if (
        not env_file.is_file()
        or env_file.is_symlink()
        or env_file.stat().st_size > 1024 * 1024
    ):
        raise RuntimeError("TRANSLATOR_COMPUTE_ENV_FILE_INVALID")
    project = ""
    for row in env_file.read_text(encoding="utf-8").splitlines():
        key, separator, value = row.partition("=")
        if (
            key == "AELEV_INFRASTRUCTURE_TRANSLATOR_COMPUTE_COMPOSE_PROJECT"
            and separator
        ):
            project = value.strip()
            break
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,62}", project) is None:
        raise RuntimeError("TRANSLATOR_COMPUTE_PROJECT_INVALID")
    return [
        docker,
        "--host",
        endpoint,
        "compose",
        "--project-name",
        project,
        "--env-file",
        str(env_file.resolve()),
        "-f",
        str(recipe),
    ]


def _configuration(command: list[str], role: str = "bundle") -> dict[str, Any]:
    raw = cast(
        object,
        json.loads(_execute(command + ["config", "--format", "json"], capture=True)),
    )
    if not isinstance(raw, dict):
        raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
    configuration = cast(dict[str, object], raw)
    services = configuration.get("services")
    if not isinstance(services, dict):
        raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
    typed_services = cast(dict[str, object], services)
    if role != "bundle":
        return _network_configuration(cast(dict[str, Any], configuration), role)
    if set(typed_services) != {
        "translation-provider",
        "translator-worker",
    }:
        raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
    for service in typed_services.values():
        if not isinstance(service, dict) or not PINNED_IMAGE.fullmatch(
            str(cast(dict[str, object], service).get("image", ""))
        ):
            raise RuntimeError("TRANSLATOR_COMPUTE_PINNED_IMAGE_REQUIRED")
    provider = cast(dict[str, object], typed_services["translation-provider"])
    worker = cast(dict[str, object], typed_services["translator-worker"])
    provider_volumes = provider.get("volumes")
    if not isinstance(provider_volumes, list):
        raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
    typed_volumes = [
        cast(dict[str, object], volume)
        for volume in cast(list[object], provider_volumes)
        if isinstance(volume, dict)
    ]
    secret_mounts = [
        volume
        for volume in typed_volumes
        if volume.get("target") == "/run/secrets/provider/credential.env"
    ]
    secret_bind = secret_mounts[0].get("bind") if len(secret_mounts) == 1 else None
    if (
        provider["image"] == worker["image"]
        or provider.get("network_mode") is not None
        or provider.get("command") is not None
        or provider.get("entrypoint") is not None
        or "environment" in provider
        or "healthcheck" in provider
        or "ports" in provider
        or "expose" in provider
        or len(typed_volumes) != len(cast(list[object], provider_volumes))
        or len(secret_mounts) != 1
        or secret_mounts[0].get("type") != "bind"
        or secret_mounts[0].get("read_only") is not True
        or not isinstance(secret_bind, dict)
        or cast(dict[str, object], secret_bind).get("create_host_path") is not False
    ):
        raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
    return cast(dict[str, Any], configuration)


def _network_configuration(configuration: dict[str, Any], role: str) -> dict[str, Any]:
    service_name = {
        "provider": "translation-provider",
        "worker": "translator-worker",
    }.get(role)
    if service_name is None or set(configuration["services"]) != {service_name}:
        raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
    service_value: object = configuration["services"][service_name]
    if not isinstance(service_value, dict):
        raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
    service = cast(dict[str, Any], service_value)
    if not PINNED_IMAGE.fullmatch(str(service.get("image", ""))):
        raise RuntimeError("TRANSLATOR_COMPUTE_PINNED_IMAGE_REQUIRED")
    if (
        service.get("read_only") is not True
        or service.get("cap_drop") != ["ALL"]
        or "no-new-privileges:true" not in service.get("security_opt", [])
        or service.get("restart") != "on-failure:3"
        or any(
            service.get(key)
            for key in (
                "privileged",
                "cap_add",
                "devices",
                "pid",
                "ipc",
                "network_mode",
                "container_name",
                "user",
                "depends_on",
            )
        )
    ):
        raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
    expected = {
        "/run/secrets/compute-provider",
        "/run/secrets/compute-provider/identity.json",
        *(
            (
                "/run/secrets/provider/credential.env",
                "/run/secrets/compute-provider/transport.json",
            )
            if role == "provider"
            else ("/run/secrets/database", "/run/secrets/telemetry/client")
        ),
    }
    mounts = service.get("volumes", [])
    if (
        len(mounts) != len(expected)
        or any(not isinstance(mount, dict) for mount in mounts)
        or {mount.get("target") for mount in mounts} != expected
        or any(mount.get("read_only") is not True for mount in mounts)
    ):
        raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
    for mount in mounts:
        if mount["target"] == "/run/secrets/compute-provider":
            declared = configuration.get("volumes", {}).get(mount.get("source"), {})
            if mount.get("type") != "volume" or declared.get("external") is not True:
                raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
        elif (
            mount.get("type") != "bind"
            or mount.get("bind", {}).get("create_host_path") is not False
            or not Path(str(mount.get("source", ""))).is_absolute()
        ):
            raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
    if role == "provider":
        if any(
            service.get(key) is not None for key in ("command", "entrypoint")
        ) or any(key in service for key in ("environment", "healthcheck", "expose")):
            raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
        ports = service.get("ports", [])
        try:
            if (
                len(ports) != 1
                or ports[0]["target"] != 18487
                or not 1 <= int(ports[0]["published"]) <= 65535
            ):
                raise ValueError
            ipaddress.ip_address(ports[0]["host_ip"])
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID") from None
    else:
        environment = service.get("environment", {})
        if (
            any(
                key != "AELEV_ENVIRONMENT" and not key.startswith("AELEV_TRANSLATOR_")
                for key in environment
            )
            or "ports" in service
            or "expose" in service
            or service.get("command")
            != ["python", "-m", "translator_service.runtime", "worker"]
            or environment.get("AELEV_TRANSLATOR_PROVIDER_TRANSPORT") != "mtls"
            or environment.get("AELEV_TRANSLATOR_PROVIDER_TLS_ROOT")
            != "/run/secrets/compute-provider"
            or not str(environment.get("AELEV_TRANSLATOR_PROVIDER_URL", "")).startswith(
                "https://"
            )
            or "AELEV_TRANSLATOR_SOCKET_PATH" in environment
            or service.get("entrypoint") is not None
        ):
            raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
    return configuration


def _network_owner(
    docker: str,
    endpoint: str,
    command: list[str],
    configuration: dict[str, Any],
    action: str,
) -> None:
    identifiers = _execute(command + ["ps", "--all", "--quiet"], capture=True).split()
    if not identifiers:
        return
    if len(identifiers) != 1 or any(
        re.fullmatch(r"[a-f0-9]{12,64}", value) is None for value in identifiers
    ):
        raise RuntimeError("TRANSLATOR_COMPUTE_OWNER_HANDOFF_REQUIRED")
    rows: object = json.loads(
        _execute([docker, "--host", endpoint, "inspect", *identifiers], capture=True)
    )
    service_name = next(iter(configuration["services"]))
    if not isinstance(rows, list):
        raise RuntimeError("TRANSLATOR_COMPUTE_OWNER_HANDOFF_REQUIRED")
    typed_rows = cast(list[object], rows)
    if len(typed_rows) != 1 or not isinstance(typed_rows[0], dict):
        raise RuntimeError("TRANSLATOR_COMPUTE_OWNER_HANDOFF_REQUIRED")
    row = cast(dict[str, Any], typed_rows[0])
    labels = row.get("Config", {}).get("Labels", {})
    if (
        labels.get("com.docker.compose.service") != service_name
        or labels.get("com.docker.compose.project.working_dir") != str(ROOT.resolve())
        or labels.get("com.docker.compose.project")
        != command[command.index("--project-name") + 1]
        or str(labels.get("com.docker.compose.oneoff", "false")).lower() != "false"
    ):
        raise RuntimeError("TRANSLATOR_COMPUTE_OWNER_HANDOFF_REQUIRED")
    if action == "start":
        image = configuration["services"][service_name]["image"]
        hashes = _execute(
            command + ["config", "--hash", service_name], capture=True
        ).split()
        image_id = _execute(
            [
                docker,
                "--host",
                endpoint,
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                image,
            ],
            capture=True,
        )
        if (
            len(hashes) != 2
            or hashes[0] != service_name
            or labels.get("com.docker.compose.config-hash") != hashes[1]
            or row.get("Config", {}).get("Image") != image
            or row.get("Image") != image_id
            or row.get("State", {}).get("Running") is not True
            or row.get("State", {}).get("Health", {}).get("Status") == "unhealthy"
        ):
            raise RuntimeError("TRANSLATOR_COMPUTE_OWNER_HANDOFF_REQUIRED")


def run(action: str, env_file: Path, timeout: int, role: str = "bundle") -> None:
    docker = _docker()
    info, endpoint = _local_engine(docker)
    command = _compose(docker, endpoint, env_file, role)
    _execute(command + ["version"], capture=True)
    configuration = _configuration(command, role)
    if role != "bundle" and action in {"start", "stop"}:
        _network_owner(docker, endpoint, command, configuration, action)
    if action == "prepare":
        _execute(command + ["pull"])
    elif action == "start":
        _execute(command + ["up", "--detach", "--wait", "--wait-timeout", str(timeout)])
    elif action == "status":
        _execute(command + ["ps"])
    elif action == "stop":
        _execute(command + ["stop", "--timeout", str(timeout)])
    elif action != "doctor":
        raise RuntimeError("TRANSLATOR_COMPUTE_ACTION_INVALID")
    LOGGER.info(
        json.dumps(
            {
                "event": "translator_compute_station",
                "action": action,
                "architecture": info.get("Architecture"),
                "services": sorted(configuration["services"]),
                "started": action == "start",
                "role": role,
            },
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("doctor", "prepare", "start", "status", "stop")
    )
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument(
        "--role", choices=("bundle", "provider", "worker"), default="bundle"
    )
    args = parser.parse_args()
    if not 1 <= args.timeout <= 1800:
        raise SystemExit("TRANSLATOR_COMPUTE_TIMEOUT_INVALID")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        run(args.action, args.env_file, args.timeout, args.role)
    except (
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
    ) as error:
        LOGGER.error(
            json.dumps(
                {
                    "event": "translator_compute_station_failed",
                    "code": str(error),
                    "action": args.action,
                },
                sort_keys=True,
            )
        )
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
