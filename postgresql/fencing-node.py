#!/usr/bin/env python3.13
"""Fixed SSH forced command for one PostgreSQL node; no remote Docker proxy."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PREFIX = "AELEV_INFRASTRUCTURE_POSTGRESQL_"
LABEL = "io.aelev.postgresql."
ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C", "DOCKER_CONFIG": "/nonexistent"}
DOCKER = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]


def configuration(path: Path) -> dict[str, Any]:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_mode & 0o077
    ):
        raise ValueError("NODE_LAUNCHER_CONFIG_INVALID")
    raw = path.read_bytes()
    value = json.loads(raw)
    common = {
        "schema",
        "cluster",
        "node",
        "daemon_id",
        "image",
        "state_volume",
        "etcd",
        "timeout_seconds",
    }
    if (
        len(raw) > 8192
        or not isinstance(value, dict)
        or set(value) not in (common | {"secrets_root"}, common | {"secrets_volume"})
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or not isinstance(value["cluster"], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value["cluster"])
        or value["node"] not in {"postgresql-1", "postgresql-2", "postgresql-3"}
        or not isinstance(value["daemon_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9:_-]{8,128}", value["daemon_id"])
        or not isinstance(value["image"], str)
        or not re.fullmatch(
            r"(?:[a-z0-9][a-z0-9._/-]*@)?sha256:[a-f0-9]{64}", value["image"]
        )
        or not isinstance(value["state_volume"], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value["state_volume"])
        or type(value["timeout_seconds"]) is not int
        or not 5 <= value["timeout_seconds"] <= 60
        or not isinstance(value["etcd"], list)
        or len(value["etcd"]) != 3
        or not isinstance(value.get("secrets_root", value.get("secrets_volume")), str)
    ):
        raise ValueError("NODE_LAUNCHER_CONFIG_INVALID")
    if "secrets_root" in value:
        secrets = Path(value["secrets_root"])
        if (
            not secrets.is_absolute()
            or secrets.is_symlink()
            or not secrets.is_dir()
            or secrets.stat().st_mode & 0o077
            or "," in str(secrets)
        ):
            raise ValueError("NODE_LAUNCHER_SECRETS_INVALID")
    elif (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value["secrets_volume"])
        or value["secrets_volume"] == value["state_volume"]
    ):
        raise ValueError("NODE_LAUNCHER_SECRETS_INVALID")
    for endpoint in value["etcd"]:
        if (
            not isinstance(endpoint, dict)
            or set(endpoint) != {"host", "port"}
            or not isinstance(endpoint["host"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", endpoint["host"])
            or type(endpoint["port"]) is not int
            or not 1 <= endpoint["port"] <= 65535
        ):
            raise ValueError("NODE_LAUNCHER_DCS_INVALID")
    return value


def inspect(arguments: list[str]) -> Any:
    try:
        result = subprocess.run(
            [*DOCKER, *arguments],
            capture_output=True,
            check=True,
            timeout=10,
            env=ENVIRONMENT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("NODE_LAUNCHER_DOCKER_UNCONFIRMED") from error
    return json.loads(result.stdout)


def command(config: dict[str, Any], *, initialize: bool) -> list[str]:
    """Inspect existing owner resources; Docker must not create a new ledger."""
    info = inspect(["info", "--format", "{{json .}}"])
    if info["ID"] != config["daemon_id"]:
        raise ValueError("NODE_LAUNCHER_DAEMON_CHANGED")
    volumes = inspect(["volume", "inspect", config["state_volume"]])
    labels = volumes[0].get("Labels") or {}
    if (
        labels.get(LABEL + "cluster") != config["cluster"]
        or labels.get(LABEL + "node") != config["node"]
    ):
        raise ValueError("NODE_LAUNCHER_VOLUME_NOT_OWNED")
    if "secrets_volume" in config:
        credentials = inspect(["volume", "inspect", config["secrets_volume"]])[0]
        labels = credentials.get("Labels") or {}
        if (
            labels.get(LABEL + "cluster") != config["cluster"]
            or labels.get(LABEL + "node") != config["node"]
            or labels.get(LABEL + "fencing-resource") != "credentials"
        ):
            raise ValueError("NODE_LAUNCHER_SECRETS_NOT_OWNED")
        secrets_mount = (
            "type=volume,source="
            + config["secrets_volume"]
            + ",target=/run/aelev/secrets,readonly"
        )
    else:
        secrets_mount = (
            "type=bind,source="
            + config["secrets_root"]
            + ",target=/run/aelev/secrets,readonly"
        )
    image = inspect(["image", "inspect", config["image"]])[0]
    if image.get("Os") != "linux" or image.get("Architecture") != info.get(
        "Architecture"
    ):
        # Docker reports aarch64/x86_64 while OCI reports arm64/amd64.
        native = {"aarch64": "arm64", "x86_64": "amd64"}.get(
            info.get("Architecture"), info.get("Architecture")
        )
        if image.get("Os") != "linux" or image.get("Architecture") != native:
            raise ValueError("NODE_LAUNCHER_NATIVE_IMAGE_REQUIRED")
    values = {
        "CLUSTER": config["cluster"],
        "FENCING_DOCKER_LOCAL_NODES": config["node"],
        "FENCING_DOCKER_DAEMON_ID": config["daemon_id"],
        "FENCING_TIMEOUT_SECONDS": str(config["timeout_seconds"]),
    }
    for number, endpoint in enumerate(config["etcd"], 1):
        values[f"NODE_{number}_ETCD_HOST"] = endpoint["host"]
        values[f"NODE_{number}_ETCD_CLIENT_PORT"] = str(endpoint["port"])
    args = [
        *DOCKER,
        "run",
        "--rm",
        "--interactive",
        "--pull",
        "never",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "32",
        "--memory",
        "128m",
        "--tmpfs",
        "/run/aelev-tmp:size=8m,mode=0700",
        "--env",
        "TMPDIR=/run/aelev-tmp",
        "--network",
        "bridge",
        "--label",
        "io.aelev.postgresql.operation=node-fencing",
        "--label",
        "io.aelev.postgresql.fencing-cluster=" + config["cluster"],
        "--label",
        "io.aelev.postgresql.fencing-target=" + config["node"],
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/run/docker.sock",
        "--mount",
        "type=volume,source="
        + config["state_volume"]
        + ",target=/var/lib/aelev-fencing",
        "--mount",
        secrets_mount,
        "--env",
        "SSH_ORIGINAL_COMMAND=aelev-postgresql-fencing-node-v1",
    ]
    for key, value in values.items():
        args.extend(["--env", PREFIX + key + "=" + value])
    return [
        *args,
        "--entrypoint",
        "python",
        config["image"],
        "-m",
        "aelev_postgresql.fencing_node",
        "initialize" if initialize else "execute",
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("/etc/aelev-postgresql-fencing/node.json")
    )
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        config = configuration(args.config)
        body = b"" if args.initialize else sys.stdin.buffer.read(4097)
        if len(body) > 4096:
            raise ValueError("NODE_LAUNCHER_REQUEST_TOO_LARGE")
        result = subprocess.run(
            command(config, initialize=args.initialize),
            input=body,
            stdout=sys.stdout.buffer,
            stderr=sys.stderr.buffer,
            check=False,
            env=ENVIRONMENT,
        )
        raise SystemExit(result.returncode)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        allowed = {
            "NODE_LAUNCHER_CONFIG_INVALID",
            "NODE_LAUNCHER_SECRETS_INVALID",
            "NODE_LAUNCHER_SECRETS_NOT_OWNED",
            "NODE_LAUNCHER_DCS_INVALID",
            "NODE_LAUNCHER_DOCKER_UNCONFIRMED",
            "NODE_LAUNCHER_DAEMON_CHANGED",
            "NODE_LAUNCHER_VOLUME_NOT_OWNED",
            "NODE_LAUNCHER_NATIVE_IMAGE_REQUIRED",
            "NODE_LAUNCHER_REQUEST_TOO_LARGE",
        }
        code = (
            str(error)
            if type(error) is ValueError and str(error) in allowed
            else "NODE_LAUNCHER_UNCONFIRMED"
        )
        print(json.dumps({"ok": False, "reason_code": code}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
