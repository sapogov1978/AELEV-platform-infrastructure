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
    raw = json.loads(
        _execute([docker, "context", "inspect", context], capture=True)
    )
    if not isinstance(raw, list) or len(raw) != 1:
        raise RuntimeError("TRANSLATOR_COMPUTE_LOCAL_DOCKER_REQUIRED")
    row = cast(dict[str, Any], raw[0])
    endpoint = row.get("Endpoints", {}).get("docker", {}).get("Host")
    system = platform.system()
    local = (
        isinstance(endpoint, str)
        and (
            (system == "Windows" and endpoint.startswith("npipe:////./pipe/"))
            or (
                system != "Windows"
                and re.fullmatch(r"unix:///[^,\r\n\x00]+", endpoint) is not None
            )
        )
    )
    if not local:
        raise RuntimeError("TRANSLATOR_COMPUTE_REMOTE_DOCKER_FORBIDDEN")
    info = json.loads(
        _execute(
            [docker, "--host", cast(str, endpoint), "info", "--format", "{{json .}}"],
            capture=True,
        )
    )
    if not isinstance(info, dict) or info.get("OSType") != "linux":
        raise RuntimeError("TRANSLATOR_COMPUTE_LINUX_CONTAINERS_REQUIRED")
    return cast(dict[str, Any], info), cast(str, endpoint)


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
    raw = json.loads(_execute(command + ["config", "--format", "json"], capture=True))
    if not isinstance(raw, dict):
        raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
    services = raw.get("services")
    if not isinstance(services, dict) or set(services) != {
        "translator-model",
        "translator-worker",
    }:
        raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
    for service in services.values():
        if not isinstance(service, dict) or not PINNED_IMAGE.fullmatch(
            str(service.get("image", ""))
        ):
            raise RuntimeError("TRANSLATOR_COMPUTE_PINNED_IMAGE_REQUIRED")
    model = cast(dict[str, Any], services["translator-model"])
    worker = cast(dict[str, Any], services["translator-worker"])
    if model["image"] != worker["image"] or model.get("network_mode") != "none":
        raise RuntimeError("TRANSLATOR_COMPUTE_CONFIGURATION_INVALID")
    return raw


def run(action: str, env_file: Path, timeout: int) -> None:
    docker = _docker()
    info, endpoint = _local_engine(docker)
    command = _compose(docker, endpoint, env_file)
    _execute(command + ["version"], capture=True)
    configuration = _configuration(command)
    if action == "prepare":
        _execute(command + ["pull"])
    elif action == "start":
        _execute(
            command
            + ["up", "--detach", "--wait", "--wait-timeout", str(timeout)]
        )
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
