"""Delegate one pinned SPIRE runtime to its existing native owner lifecycle."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, cast

from tools import (
    node_execution as node,
    node_artifact as artifact,
    owner_compose as owner,
)

ROLES = {"server", "observer", "status-publisher", "telemetry-sender"}
APPROVAL = "DRP_SPIRE_DOCKER_SOCKET_APPROVAL"
APPROVAL_VALUE = "operator-root-equivalent-v1"
IMMUTABLE = r"(?:sha256:[a-f0-9]{64}|[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64})"
require = node.require


def invoke(
    command: list[str], *, initial: Path | None = None, effectful: bool = False
) -> bytes:
    environment = owner.environment()
    require(
        os.environ.get(APPROVAL) == APPROVAL_VALUE,
        "OWNER_NATIVE_SOCKET_APPROVAL_REQUIRED",
    )
    environment[APPROVAL] = APPROVAL_VALUE
    if initial is not None:
        environment["AELEV_INFRASTRUCTURE_SPIRE_INITIAL_INPUT"] = str(initial)
    try:
        result = subprocess.run(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        if effectful:
            raise owner.EffectUnknown() from error
        raise
    if result.returncode:
        raise owner.CommandFailure(result.returncode, result.stdout, result.stderr)
    return result.stdout


def image_id(reference: str) -> str:
    require(
        isinstance(reference, str) and re.fullmatch(IMMUTABLE, reference) is not None,
        "OWNER_NATIVE_IMMUTABLE_IMAGE_REQUIRED",
    )
    rows = json.loads(owner.call(["docker", "image", "inspect", reference]))
    require(
        isinstance(rows, list)
        and len(rows) == 1
        and isinstance(rows[0], dict)
        and rows[0].get("Os") == "linux"
        and re.fullmatch(r"sha256:[a-f0-9]{64}", rows[0].get("Id", "")) is not None,
        "OWNER_NATIVE_IMAGE_INVALID",
    )
    return cast(str, rows[0]["Id"])


def input_directory(path: Path) -> None:
    node.absolute(str(path))
    info = path.stat()
    require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) & 0o022 == 0,
        "OWNER_NATIVE_INPUT_DIRECTORY_PERMISSIONS",
    )


def definition(value: dict[str, Any]) -> tuple[dict[str, Any], str, Path, str]:
    require(
        set(value) == owner.FIELDS | {"native_lifecycle", "expected_runtime"},
        "OWNER_NATIVE_REQUEST_INVALID",
    )
    base = {
        name: body
        for name, body in value.items()
        if name not in {"native_lifecycle", "expected_runtime"}
    }
    _, role = owner.definition(base)
    require(
        role.get("kind") == "runtime"
        and role.get("owner") == "spire"
        and role.get("opaque") is False
        and value["role"]
        in {"platform-infrastructure/spire-" + name for name in ROLES},
        "OWNER_NATIVE_ROLE_INVALID",
    )
    require(
        value["executor_root"] == value["owner_root"]
        and value["executor_files"] == value["owner_files"]
        and value["executor_files"].get("tools/spire_runtime.py")
        == owner.digest(Path(__file__).read_bytes()),
        "OWNER_NATIVE_SOURCE_INVALID",
    )
    require(
        value["executor_files"].get("tools/node_artifact.py")
        == owner.digest(Path(artifact.__file__).read_bytes()),
        "OWNER_NATIVE_SOURCE_INVALID",
    )
    selected = value["native_lifecycle"]
    require(
        isinstance(selected, dict)
        and set(selected)
        == {"kind", "runtime_image", "installer_image", "input_directory", "server"}
        and selected.get("kind") == "spire",
        "OWNER_NATIVE_LIFECYCLE_INVALID",
    )
    component = role["id"].removeprefix("spire-")
    require(
        re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", value["project"]) is not None,
        "OWNER_NATIVE_PROJECT_INVALID",
    )
    workspace = node.absolute(value["project_directory"])
    initial = node.absolute(selected["input_directory"])
    require(
        initial.is_relative_to(workspace / "secrets")
        and initial != workspace / "secrets",
        "OWNER_NATIVE_INPUT_DIRECTORY_INVALID",
    )
    input_directory(initial)
    paths = list(initial.rglob("*"))
    require(0 < len(paths) <= 128, "OWNER_NATIVE_INPUT_LIMIT")
    files = set()
    for path in paths:
        node.absolute(str(path))
        if path.is_dir():
            input_directory(path)
        else:
            node.private(path)
        if path.is_file():
            require(
                path.stat().st_nlink == 1
                and 0 < path.stat().st_size <= 65536
                and str(path) in value["inputs"],
                "OWNER_NATIVE_UNDECLARED_INPUT",
            )
            files.add(str(path))
    declared = {
        name for name in value["inputs"] if node.absolute(name).is_relative_to(initial)
    }
    require(bool(files) and files == declared, "OWNER_NATIVE_INPUT_SET_INVALID")
    server = selected["server"]
    require(isinstance(server, dict), "OWNER_NATIVE_SERVER_OPTIONS_INVALID")
    if component == "server":
        require(
            set(server) == {"bind_address", "public_port"}
            and isinstance(server["bind_address"], str)
            and type(server["public_port"]) is int
            and 1 <= server["public_port"] <= 65535,
            "OWNER_NATIVE_SERVER_OPTIONS_INVALID",
        )
        ipaddress.ip_address(server["bind_address"])
        require(str(initial / "plan.json") in files, "OWNER_NATIVE_PLAN_REQUIRED")
        plan = node.read(initial / "plan.json")
        require(
            plan.get("bind_address") == server["bind_address"]
            and plan.get("public_port") == server["public_port"],
            "OWNER_NATIVE_SERVER_PLAN_MISMATCH",
        )
    else:
        require(
            not server and str(initial / "component.json") in files,
            "OWNER_NATIVE_PLAN_REQUIRED",
        )
    runtime = image_id(selected["runtime_image"])
    image_id(selected["installer_image"])
    expected = image_id(value["image"])
    if component == "telemetry-sender":
        plan = node.read(initial / "component.json")
        require(plan.get("component") == component, "OWNER_NATIVE_COMPONENT_MISMATCH")
        require(
            image_id(plan["observation"]["sender_image"]) == expected,
            "OWNER_NATIVE_ROLE_IMAGE_MISMATCH",
        )
    else:
        require(runtime == expected, "OWNER_NATIVE_ROLE_IMAGE_MISMATCH")
    material = {
        key: body
        for key, body in value.items()
        if key not in {"configuration_sha256", "expected_runtime"}
    }
    checksum = owner.digest(node.encode(material))
    return selected, component, initial, checksum


def stage_initial(value: dict[str, Any], initial: Path) -> Path:
    directory = node.absolute(os.environ["AELEV_NODE_OPERATION_DIRECTORY"])
    node.private(directory, directory=True)
    workspace = node.absolute(value["project_directory"])
    names = sorted(
        str(Path(name).relative_to(workspace))
        for name in value["inputs"]
        if Path(name).is_relative_to(initial)
    )
    archive = directory / "native-initial.zip"
    manifest = artifact.pack_inputs(workspace, names, "native-initial", archive)
    request = artifact.prepare_request(
        "owner-input",
        "native-initial",
        str(directory / "native-inputs"),
        manifest,
        archive,
    )
    delivered = artifact.deliver({"kind": "local"}, request, archive)
    require(
        delivered.get("status") == "ARTIFACT_MATERIALIZED",
        "OWNER_NATIVE_INPUT_DELIVERY_REFUSED",
    )
    payload = node.absolute(delivered["path"])
    result = payload / initial.relative_to(workspace)
    node.private(result, directory=True)
    for name in names:
        target = node.absolute(str(payload / name))
        node.private(target)
        require(
            owner.digest(target.read_bytes()) == value["inputs"][str(workspace / name)],
            "OWNER_NATIVE_DELIVERED_INPUT_CHANGED",
        )
    return result


def observe(
    value: dict[str, Any],
    component: str,
    selected: dict[str, Any],
    checksum: str,
    expected: dict[str, Any] | None,
) -> dict[str, Any]:
    directory = node.absolute(os.environ["AELEV_NODE_OPERATION_DIRECTORY"])
    node.private(directory, directory=True)
    output = directory / "native-export"
    require(not output.exists(), "OWNER_NATIVE_OBSERVATION_ALREADY_EXISTS")
    invoke(
        [
            "sh",
            str(Path(value["executor_root"]) / "spire/export.sh"),
            value["project"],
            selected["installer_image"],
            str(output),
            component,
        ]
    )
    receipt_path = node.absolute(
        str(output / "runtime/installed-composition/receipt.json")
    )
    require(receipt_path.stat().st_size <= 65536, "OWNER_NATIVE_EXPORT_INVALID")
    receipt = node.read(receipt_path)
    role = "spire-" + component
    require(
        set(receipt) == {role, "generation"}
        and isinstance(receipt["generation"], str)
        and re.fullmatch(r"[a-f0-9]{64}", receipt["generation"]) is not None,
        "OWNER_NATIVE_EXPORT_INVALID",
    )
    row = receipt[role]
    require(isinstance(row, dict), "OWNER_NATIVE_EXPORT_INVALID")
    require(
        row.get("compose_project") == value["project"]
        and row.get("restart") == "no"
        and isinstance(row.get("container_id"), str)
        and re.fullmatch(r"[a-f0-9]{64}", row["container_id"]) is not None
        and row.get("image") == image_id(value["image"]),
        "OWNER_NATIVE_EXPORT_BINDING_INVALID",
    )
    current = json.loads(owner.call(["docker", "inspect", row["container_id"]]))
    require(
        isinstance(current, list) and len(current) == 1,
        "OWNER_NATIVE_CONTAINER_INVALID",
    )
    container = current[0]
    labels = container["Config"].get("Labels", {})
    require(
        container["Id"] == row["container_id"]
        and container["Image"] == row["image"]
        and labels.get("com.docker.compose.project") == value["project"]
        and labels.get("com.docker.compose.service") == role
        and container["HostConfig"]["RestartPolicy"]["Name"] == "no"
        and container["RestartCount"] == row["native_restarts"],
        "OWNER_NATIVE_CONTAINER_DRIFT",
    )
    state = container["State"]
    record = {
        "schema_version": 1,
        "configuration_sha256": checksum,
        "container_id": row["container_id"],
        "image": row["image"],
        "started_at": state["StartedAt"],
        "native_restarts": container["RestartCount"],
    }
    require(
        expected is None
        or expected
        == {
            "configuration_sha256": checksum,
            "container_id": record["container_id"],
            "native_image_id": record["image"],
            "incarnation_sha256": owner.digest(node.encode(record)),
        },
        "OWNER_NATIVE_INCARNATION_HANDOFF_REQUIRED",
    )
    running = (
        state.get("Running") is True
        and not state.get("Paused")
        and not state.get("Restarting")
    )
    health = state.get("Health", {}).get("Status", "not_configured")
    require(
        health in {"healthy", "starting", "unhealthy", "not_configured"},
        "OWNER_NATIVE_HEALTH_INVALID",
    )
    return {
        "status": "OWNER_ROLE_RUNNING" if running else "OWNER_ROLE_STOPPED",
        "configuration_sha256": checksum,
        "container_id": record["container_id"],
        "native_image_id": record["image"],
        "incarnation_sha256": owner.digest(node.encode(record)),
        "health": health,
        "owner_readiness_verified": False,
        "queue_resume_issued": False,
    }


def execute(value: dict[str, Any], action: str) -> dict[str, Any]:
    require(action in {"plan", "start", "status"}, "OWNER_NATIVE_ACTION_INVALID")
    selected, component, initial, checksum = definition(value)
    require(
        os.environ.get(APPROVAL) == APPROVAL_VALUE,
        "OWNER_NATIVE_SOCKET_APPROVAL_REQUIRED",
    )
    expected = value["expected_runtime"]
    require(
        expected is None
        or isinstance(expected, dict)
        and set(expected)
        == {
            "configuration_sha256",
            "container_id",
            "native_image_id",
            "incarnation_sha256",
        },
        "OWNER_NATIVE_EXPECTED_RUNTIME_INVALID",
    )
    if expected is not None:
        for key, pattern in {
            "configuration_sha256": r"[a-f0-9]{64}",
            "container_id": r"[a-f0-9]{64}",
            "native_image_id": r"sha256:[a-f0-9]{64}",
            "incarnation_sha256": r"[a-f0-9]{64}",
        }.items():
            require(
                isinstance(expected[key], str)
                and re.fullmatch(pattern, expected[key]) is not None,
                "OWNER_NATIVE_EXPECTED_RUNTIME_INVALID",
            )
    result = {
        "configuration_sha256": checksum,
        "owner_readiness_verified": False,
        "queue_resume_issued": False,
    }
    if action == "plan":
        require(expected is None, "OWNER_NATIVE_EXPECTED_RUNTIME_INVALID")
        return {**result, "status": "OWNER_NATIVE_PLANNED"}
    require(
        value["configuration_sha256"] == checksum, "OWNER_NATIVE_CONFIGURATION_DRIFT"
    )
    if action == "status":
        require(expected is not None, "OWNER_NATIVE_START_RECEIPT_REQUIRED")
        return observe(value, component, selected, checksum, expected)
    require(expected is None, "OWNER_NATIVE_EXPECTED_RUNTIME_INVALID")
    volumes = (
        owner.call(
            [
                "docker",
                "volume",
                "ls",
                "--quiet",
                "--filter",
                "name=" + value["project"] + "_installer",
            ]
        )
        .decode()
        .split()
    )
    require(
        value["project"] + "_installer" not in volumes,
        "OWNER_NATIVE_UNBOUND_HANDOFF_REQUIRED",
    )
    command = [
        "sh",
        str(Path(value["executor_root"]) / "spire/install.sh"),
        component,
        value["project"],
        selected["runtime_image"],
        selected["installer_image"],
        "--native-restart-policy",
        "no",
    ]
    if component == "server":
        command += [
            "--bind-address",
            selected["server"]["bind_address"],
            "--public-port",
            str(selected["server"]["public_port"]),
        ]
    prepared_initial = stage_initial(value, initial)
    require(definition(value)[3] == checksum, "OWNER_NATIVE_INPUT_CHANGED")
    invoke(command, initial=prepared_initial, effectful=True)
    try:
        projection = observe(value, component, selected, checksum, None)
        require(
            projection["status"] == "OWNER_ROLE_RUNNING",
            "OWNER_NATIVE_START_NOT_RUNNING",
        )
        require(definition(value)[3] == checksum, "OWNER_NATIVE_INPUT_CHANGED")
    except (
        node.ExecutionError,
        owner.CommandFailure,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.TimeoutExpired,
    ) as error:
        raise owner.EffectUnknown() from error
    return projection


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "start", "status"))
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--request-sha256", required=True)
    args = parser.parse_args()
    try:
        node.private(node.absolute(str(args.request)))
        data = args.request.read_bytes()
        require(
            len(data) <= node.LIMIT and owner.digest(data) == args.request_sha256,
            "OWNER_NATIVE_REQUEST_DRIFT",
        )

        def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, body in items:
                require(key not in result, "OWNER_NATIVE_DUPLICATE_JSON_KEY")
                result[key] = body
            return result

        value = json.loads(data, object_pairs_hook=unique)
        require(isinstance(value, dict), "OWNER_NATIVE_REQUEST_INVALID")
        result = execute(value, args.action)
    except owner.EffectUnknown:
        print(
            json.dumps(
                {
                    "status": "RESULT_UNKNOWN",
                    "code": "OWNER_NATIVE_RESULT_UNKNOWN_RECONCILIATION_REQUIRED",
                }
            )
        )
        return 3
    except owner.CommandFailure as error:
        directory = node.absolute(os.environ["AELEV_NODE_OPERATION_DIRECTORY"])
        node.private(directory, directory=True)
        node.save(
            directory / "owner-command-failure.json",
            {
                "exit_code": error.exit_code,
                "stdout": error.stdout[:1048576].decode(errors="replace"),
                "stderr": error.stderr[:1048576].decode(errors="replace"),
            },
        )
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "code": "OWNER_NATIVE_COMMAND_FAILED",
                    "owner_exit_code": error.exit_code,
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
                {
                    "status": "BLOCKED",
                    "code": "OWNER_NATIVE_INPUT_OR_ENVIRONMENT_INVALID",
                }
            )
        )
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
