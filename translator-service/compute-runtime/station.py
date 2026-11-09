"""Portable owner-local lifecycle for one Translator compute station."""

from __future__ import annotations

import argparse
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


def _compose(docker: str, endpoint: str, env_file: Path) -> list[str]:
    if (
        not env_file.is_file()
        or env_file.is_symlink()
        or env_file.stat().st_size > 1024 * 1024
    ):
        raise RuntimeError("TRANSLATOR_COMPUTE_ENV_FILE_INVALID")
    return [
        docker,
        "--host",
        endpoint,
        "compose",
        "--env-file",
        str(env_file.resolve()),
        "-f",
        str(COMPOSE),
    ]


def _configuration(command: list[str]) -> dict[str, Any]:
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
        or len(typed_volumes) != len(provider_volumes)
        or len(secret_mounts) != 1
        or secret_mounts[0].get("type") != "bind"
        or secret_mounts[0].get("read_only") is not True
        or not isinstance(secret_bind, dict)
        or cast(dict[str, object], secret_bind).get("create_host_path") is not False
    ):
        raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
    return cast(dict[str, Any], configuration)


def run(action: str, env_file: Path, timeout: int) -> None:
    docker = _docker()
    info, endpoint = _local_engine(docker)
    command = _compose(docker, endpoint, env_file)
    _execute(command + ["version"], capture=True)
    configuration = _configuration(command)
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
    args = parser.parse_args()
    if not 1 <= args.timeout <= 1800:
        raise SystemExit("TRANSLATOR_COMPUTE_TIMEOUT_INVALID")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        run(args.action, args.env_file, args.timeout)
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
