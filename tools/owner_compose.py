"""Execute one explicitly prepared, source-pinned owner Compose role."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, cast

from tools import node_execution as node

FIELDS = {
    "schema_version",
    "executor_root",
    "executor_files",
    "owner_root",
    "owner_files",
    "catalog",
    "role",
    "recipes",
    "project_directory",
    "project",
    "env_file",
    "inputs",
    "image",
    "configuration_sha256",
}


class CommandFailure(node.ExecutionError):
    def __init__(
        self, exit_code: int, stdout: bytes = b"", stderr: bytes = b""
    ) -> None:
        super().__init__("OWNER_COMPOSE_COMMAND_FAILED")
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class EffectUnknown(node.ExecutionError):
    def __init__(self) -> None:
        super().__init__("OWNER_COMPOSE_RESULT_UNKNOWN_RECONCILIATION_REQUIRED")


def require(value: bool, code: str) -> None:
    node.require(value, code)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def environment() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if name in {"PATH", "HOME", "LANG", "TMPDIR", "DOCKER_HOST", "DOCKER_CONFIG"}
    }


def call(command: list[str], *, effectful: bool = False) -> bytes:
    try:
        result = subprocess.run(
            command,
            env=environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        if effectful:
            raise EffectUnknown() from error
        raise
    if result.returncode:
        raise CommandFailure(result.returncode, result.stdout, result.stderr)
    return result.stdout


def source_tree(root_value: Any, files: Any) -> tuple[Path, dict[str, Any]]:
    root = node.absolute(root_value)
    require(isinstance(files, dict) and bool(files), "OWNER_COMPOSE_SOURCE_REQUIRED")
    for name, expected in files.items():
        require(
            isinstance(name, str)
            and bool(name)
            and not Path(name).is_absolute()
            and ".." not in Path(name).parts
            and isinstance(expected, str)
            and re.fullmatch(r"[a-f0-9]{64}", expected) is not None,
            "OWNER_COMPOSE_SOURCE_INVALID",
        )
        path = node.absolute(str(root / name))
        require(
            path.is_file() and digest(path.read_bytes()) == expected,
            "OWNER_COMPOSE_SOURCE_DRIFT",
        )
    return root, cast(dict[str, Any], files)


def definition(value: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    require(
        set(value) == FIELDS
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1,
        "OWNER_COMPOSE_REQUEST_INVALID",
    )
    executor_root, executor_files = source_tree(
        value["executor_root"], value["executor_files"]
    )
    root, files = source_tree(value["owner_root"], value["owner_files"])
    require(
        "tools/owner_compose.py" in executor_files
        and "tools/node_execution.py" in executor_files
        and digest(Path(__file__).read_bytes())
        == executor_files["tools/owner_compose.py"],
        "OWNER_COMPOSE_EXECUTOR_MISMATCH",
    )
    catalog_path = value["catalog"]
    require(
        isinstance(catalog_path, str) and catalog_path in files,
        "OWNER_COMPOSE_CATALOG_REQUIRED",
    )
    catalog = node.read(root / catalog_path)
    roles = [
        row
        for row in catalog["roles"]
        if catalog["product"] + "/" + row["id"] == value["role"]
    ]
    require(len(roles) == 1, "OWNER_COMPOSE_ROLE_INVALID")
    role = roles[0]
    require(
        value["role"] != "platform-infrastructure/postgresql-fencing-node-install",
        "OWNER_COMPOSE_NATIVE_OPERATION_REQUIRED",
    )
    require(
        role["kind"] in {"runtime", "operation", "node"}, "OWNER_COMPOSE_ROLE_INVALID"
    )
    recipes = value["recipes"]
    require(
        isinstance(recipes, list)
        and bool(recipes)
        and len(recipes) == len(set(recipes))
        and all(
            isinstance(name, str) and name in files and name in role["recipes"]
            for name in recipes
        ),
        "OWNER_COMPOSE_RECIPE_INVALID",
    )
    workspace = node.absolute(value["project_directory"])
    node.private(workspace, directory=True)
    require(
        not workspace.is_relative_to(root)
        and not workspace.is_relative_to(executor_root),
        "OWNER_COMPOSE_PRIVATE_WORKSPACE_REQUIRED",
    )
    require(
        isinstance(value["project"], str)
        and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", value["project"]) is not None,
        "OWNER_COMPOSE_PROJECT_INVALID",
    )
    inputs = value["inputs"]
    require(
        isinstance(inputs, dict) and value["env_file"] in inputs,
        "OWNER_COMPOSE_INPUTS_REQUIRED",
    )
    for name, expected in inputs.items():
        path = node.absolute(name)
        node.private(path)
        require(
            isinstance(expected, str)
            and re.fullmatch(r"[a-f0-9]{64}", expected) is not None
            and digest(path.read_bytes()) == expected,
            "OWNER_COMPOSE_INPUT_DRIFT",
        )
    require(
        isinstance(value["image"], str)
        and re.fullmatch(
            r"(?:sha256:[a-f0-9]{64}|[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64})",
            value["image"],
        )
        is not None,
        "OWNER_COMPOSE_IMMUTABLE_IMAGE_REQUIRED",
    )
    require(
        value["configuration_sha256"] is None
        or isinstance(value["configuration_sha256"], str)
        and re.fullmatch(r"[a-f0-9]{64}", value["configuration_sha256"]) is not None,
        "OWNER_COMPOSE_CONFIGURATION_INVALID",
    )
    command = [
        "docker",
        "compose",
        "--project-name",
        value["project"],
        "--project-directory",
        str(workspace),
        "--env-file",
        value["env_file"],
    ]
    for recipe in recipes:
        command += ["--file", str(root / recipe)]
    return command, role


def inspect_role(
    value: dict[str, Any], service: str, config_hash: str, image_id: str
) -> list[dict[str, Any]]:
    ids = (
        call(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=" + value["project"],
                "--filter",
                "label=com.docker.compose.service=" + service,
            ]
        )
        .decode()
        .split()
    )
    require(len(ids) <= 1, "OWNER_COMPOSE_REPLICA_HANDOFF_REQUIRED")
    if not ids:
        return []
    require(
        all(re.fullmatch(r"[a-f0-9]{12,64}", item) is not None for item in ids),
        "OWNER_COMPOSE_INSPECT_INVALID",
    )
    rows = json.loads(call(["docker", "inspect", *ids]))
    require(isinstance(rows, list) and len(rows) == 1, "OWNER_COMPOSE_INSPECT_INVALID")
    for row in rows:
        labels = row.get("Config", {}).get("Labels", {})
        require(
            labels.get("com.docker.compose.project") == value["project"]
            and labels.get("com.docker.compose.service") == service
            and labels.get("com.docker.compose.config-hash") == config_hash
            and labels.get("com.docker.compose.oneoff", "False").lower() == "false"
            and row.get("Image") == image_id,
            "OWNER_COMPOSE_OWNER_HANDOFF_REQUIRED",
        )
        require(
            row.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") == "no",
            "OWNER_COMPOSE_NATIVE_RESTART_REFUSED",
        )
    return cast(list[dict[str, Any]], rows)


def apply_model(
    value: dict[str, Any], action: str, command: list[str], role: dict[str, Any]
) -> dict[str, Any]:
    service = role["id"]
    parts = (
        call([*command, "--profile", "*", "config", "--hash", service]).decode().split()
    )
    require(
        len(parts) == 2
        and parts[0] == service
        and re.fullmatch(r"[a-f0-9]{64}", parts[1]) is not None,
        "OWNER_COMPOSE_CONFIGURATION_INVALID",
    )
    config_hash = parts[1]
    result: dict[str, Any] = {
        "status": "OWNER_COMPOSE_PLANNED",
        "role": value["role"],
        "project": value["project"],
        "configuration_sha256": config_hash,
        "image": value["image"],
        "owner_readiness_verified": False,
        "queue_resume_issued": False,
        "physical_distribution_verified": False,
    }
    if action == "plan":
        return result
    require(
        value["configuration_sha256"] == config_hash,
        "OWNER_COMPOSE_CONFIGURATION_DRIFT",
    )
    images = json.loads(call(["docker", "image", "inspect", value["image"]]))
    require(
        isinstance(images, list)
        and len(images) == 1
        and isinstance(images[0].get("Id"), str)
        and re.fullmatch(r"sha256:[a-f0-9]{64}", images[0]["Id"]) is not None,
        "OWNER_COMPOSE_NATIVE_IMAGE_REQUIRED",
    )
    image_id = images[0]["Id"]
    rows = inspect_role(value, service, config_hash, image_id)
    if action == "task":
        require(not rows, "OWNER_COMPOSE_OWNER_HANDOFF_REQUIRED")
        call(
            [
                *command,
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "never",
                "-T",
                "--interactive=false",
                service,
            ],
            effectful=True,
        )
        result["status"] = "OWNER_TASK_COMPLETED"
        return result
    if not rows and action == "start":
        call(
            [
                *command,
                "up",
                "--detach",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "--no-recreate",
                service,
            ],
            effectful=True,
        )
        rows = inspect_role(value, service, config_hash, image_id)
        require(bool(rows), "OWNER_COMPOSE_START_NOT_OBSERVED")
    if not rows:
        result["status"] = "OWNER_ROLE_ABSENT"
        return result
    row = rows[0]
    state = row["State"]
    running = (
        state.get("Running") is True
        and not state.get("Paused")
        and not state.get("Restarting")
    )
    require(action != "start" or running, "OWNER_COMPOSE_OWNER_HANDOFF_REQUIRED")
    result.update(
        status="OWNER_ROLE_RUNNING" if running else "OWNER_ROLE_STOPPED",
        container_id=row["Id"],
        native_image_id=image_id,
        health=state.get("Health", {}).get("Status", "not_configured"),
    )
    return result


def execute(value: dict[str, Any], action: str) -> dict[str, Any]:
    require(
        action in {"plan", "start", "task", "status"}, "OWNER_COMPOSE_ACTION_INVALID"
    )
    command, role = definition(value)
    require(
        action != "task" or role["kind"] == "operation",
        "OWNER_COMPOSE_ACTION_KIND_MISMATCH",
    )
    require(
        action != "start" or role["kind"] in {"runtime", "node"},
        "OWNER_COMPOSE_ACTION_KIND_MISMATCH",
    )
    model = json.loads(call([*command, "--profile", "*", "config", "--format", "json"]))
    require(role["id"] in model.get("services", {}), "OWNER_COMPOSE_SERVICE_MISSING")
    selected = model["services"][role["id"]]
    require(selected.get("image") == value["image"], "OWNER_COMPOSE_IMAGE_MISMATCH")
    require(
        selected.get("restart", "no") == "no", "OWNER_COMPOSE_NATIVE_RESTART_REFUSED"
    )
    require(
        selected.get("pull_policy") != "build", "OWNER_COMPOSE_IMPLICIT_BUILD_REFUSED"
    )
    require(
        not selected.get("links")
        and not selected.get("volumes_from")
        and not any(
            str(selected.get(name, "")).startswith("service:")
            for name in ("network_mode", "pid", "ipc")
        ),
        "OWNER_COMPOSE_SHARED_RUNTIME_REFUSED",
    )
    with tempfile.TemporaryDirectory(prefix="owner-compose-") as directory:
        snapshot = Path(directory) / "compose.json"
        snapshot.write_text(
            json.dumps(model, sort_keys=True, separators=(",", ":"), allow_nan=False)
        )
        snapshot.chmod(0o600)
        empty_env = Path(directory) / "empty.env"
        empty_env.write_bytes(b"")
        empty_env.chmod(0o600)
        frozen = [
            "docker",
            "compose",
            "--project-name",
            value["project"],
            "--project-directory",
            value["project_directory"],
            "--env-file",
            str(empty_env),
            "--file",
            str(snapshot),
        ]
        return apply_model(value, action, frozen, role)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "start", "task", "status"))
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--request-sha256", required=True)
    args = parser.parse_args()
    try:
        node.private(node.absolute(str(args.request.absolute())))
        data = args.request.read_bytes()
        require(
            len(data) <= node.LIMIT and digest(data) == args.request_sha256,
            "OWNER_COMPOSE_REQUEST_DRIFT",
        )

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            parsed: dict[str, Any] = {}
            for key, item in pairs:
                require(key not in parsed, "OWNER_COMPOSE_DUPLICATE_JSON_KEY")
                parsed[key] = item
            return parsed

        value = json.loads(data, object_pairs_hook=unique)
        require(isinstance(value, dict), "OWNER_COMPOSE_REQUEST_INVALID")
        result = execute(value, args.action)
    except EffectUnknown as error:
        print(json.dumps({"status": "RESULT_UNKNOWN", "code": str(error)}))
        return 3
    except CommandFailure as error:
        retained = False
        directory = os.environ.get("AELEV_NODE_OPERATION_DIRECTORY")
        if directory:
            private_directory = node.absolute(directory)
            node.private(private_directory, directory=True)
            diagnostic = private_directory / "owner-command-failure.json"
            descriptor = os.open(
                diagnostic, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w") as stream:
                json.dump(
                    {
                        "exit_code": error.exit_code,
                        "stdout": error.stdout[:1048576].decode(errors="replace"),
                        "stderr": error.stderr[:1048576].decode(errors="replace"),
                        "truncated": max(len(error.stdout), len(error.stderr))
                        > 1048576,
                    },
                    stream,
                )
                stream.flush()
                os.fsync(stream.fileno())
            node.sync_directory(private_directory)
            retained = True
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "code": str(error),
                    "owner_exit_code": error.exit_code,
                    "command_diagnostics_retained_privately": retained,
                }
            )
        )
        return 2
    except node.ExecutionError as error:
        print(json.dumps({"status": "BLOCKED", "code": str(error)}))
        return 2
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        subprocess.TimeoutExpired,
    ):
        print(
            json.dumps(
                {"status": "BLOCKED", "code": "OWNER_COMPOSE_INPUT_OR_RESULT_INVALID"}
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
