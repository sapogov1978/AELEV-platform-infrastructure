#!/usr/bin/env python3
"""Invoke pinned public owner procedures on one local or SSH target node."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from typing import Any
import uuid

LIMIT = 8 * 1024 * 1024
PROCEDURES = {
    "spire-install": ("sh", "spire/install.sh"),
    "spire-export": ("sh", "spire/export.sh"),
    "spire-bootstrap": ("python", "spire/bootstrap.py"),
    "translator-station": ("python", "translator-service/compute-runtime/station.py"),
}
ENVIRONMENT = {
    "DRP_SPIRE_DOCKER_SOCKET_APPROVAL",
    "AELEV_INFRASTRUCTURE_SPIRE_INITIAL_INPUT",
    "AELEV_INFRASTRUCTURE_SPIRE_AUTHORITY_SOURCE",
    "DOCKER_CONFIG",
    "DOCKER_HOST",
}
BOOTSTRAP = """import base64,hashlib,json,sys
v=json.loads(sys.stdin.buffer.read(8388609))
c=base64.b64decode(v['code'],validate=True)
assert hashlib.sha256(c).hexdigest()==v['code_sha256']
n={'__name__':'node_carrier','__file__':'node-carrier.py','__node_code_sha256__':hashlib.sha256(c).hexdigest()}
exec(compile(c,'node-carrier.py','exec'),n)
try:
 r=n['target'](v['request'],v['action'])
except n['ExecutionError'] as e:
 r={'status':'BLOCKED','code':str(e),'operation':v['request']['operation'],'request_sha256':n['checksum'](n['encode'](v['request']))}
except (OSError,ValueError,KeyError,TypeError,IndexError):
 r={'status':'BLOCKED','code':'NODE_TARGET_INPUT_OR_ENVIRONMENT_INVALID','operation':v['request']['operation'],'request_sha256':n['checksum'](n['encode'](v['request']))}
print(json.dumps(r))
"""


class ExecutionError(ValueError):
    pass


def require(value: bool, code: str) -> None:
    if not value:
        raise ExecutionError(code)


def encode(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def absolute(value: Any, *, inspect: bool = True) -> Path:
    require(isinstance(value, str) and "\0" not in value, "NODE_PATH_INVALID")
    path = Path(value)
    require(path.is_absolute() and ".." not in path.parts, "NODE_PATH_INVALID")
    require(
        not inspect or not any(p.is_symlink() for p in [path, *path.parents]),
        "NODE_SYMLINK_REFUSED",
    )
    return path


def private(path: Path, *, directory: bool = False) -> None:
    info = path.stat()
    require(
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
        "NODE_PRIVATE_PATH_INVALID",
    )
    require(
        info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0,
        "NODE_PRIVATE_PERMISSIONS_REQUIRED",
    )


def read(path: Path) -> dict[str, Any]:
    require(path.stat().st_size <= LIMIT, "NODE_INPUT_TOO_LARGE")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            require(key not in result, "NODE_DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    value = json.loads(path.read_bytes(), object_pairs_hook=pairs)
    require(isinstance(value, dict), "NODE_OBJECT_REQUIRED")
    return value


def save(path: Path, value: dict[str, Any]) -> None:
    descriptor, pending = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encode(value) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(pending).unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate(
    request: dict[str, Any], *, target_filesystem: bool = True
) -> tuple[Path, Path, list[str]]:
    require(
        set(request)
        == {
            "schema_version",
            "operation",
            "source_root",
            "source_files",
            "state_root",
            "procedure",
            "arguments",
            "inputs",
            "environment",
            "timeout_seconds",
        },
        "NODE_REQUEST_FIELDS_INVALID",
    )
    require(
        type(request["schema_version"]) is int and request["schema_version"] == 1,
        "NODE_VERSION_INVALID",
    )
    require(
        str(uuid.UUID(request["operation"])) == request["operation"],
        "NODE_OPERATION_INVALID",
    )
    root, state = (
        absolute(request["source_root"], inspect=target_filesystem),
        absolute(request["state_root"], inspect=target_filesystem),
    )
    require(not state.is_relative_to(root), "NODE_STATE_OUTSIDE_SOURCE_REQUIRED")
    procedure = request["procedure"]
    require(
        isinstance(procedure, str) and procedure in PROCEDURES, "NODE_PROCEDURE_INVALID"
    )
    arguments = request["arguments"]
    require(
        isinstance(arguments, list)
        and 1 <= len(arguments) <= 96
        and all(
            isinstance(v, str) and len(v) <= 4096 and not any(c in v for c in "\0\r\n")
            for v in arguments
        ),
        "NODE_ARGUMENTS_INVALID",
    )
    require("--update" not in arguments, "NODE_OWNER_HANDOFF_REQUIRED")
    if procedure == "translator-station":
        require(
            arguments[0] in {"doctor", "prepare", "start", "status"}
            and arguments.count("--role") == 1
            and not any(v.startswith("--role=") for v in arguments)
            and arguments[arguments.index("--role") + 1 : arguments.index("--role") + 2]
            in [["provider"], ["worker"]],
            "NODE_INDEPENDENT_ROLE_REQUIRED",
        )
        require(
            arguments.count("--env-file") == 1
            and not any(v.startswith("--env-file=") for v in arguments),
            "NODE_CONFIGURATION_BINDING_REQUIRED",
        )
        selected = arguments[
            arguments.index("--env-file") + 1 : arguments.index("--env-file") + 2
        ]
        require(
            len(selected) == 1 and selected[0] in request["inputs"],
            "NODE_CONFIGURATION_BINDING_REQUIRED",
        )
    elif procedure == "spire-bootstrap":
        require(arguments[0] == "validate", "NODE_PROCEDURE_ACTION_INVALID")
    elif procedure == "spire-install":
        require(
            arguments[0]
            in {
                "server",
                "agent",
                "inspect",
                "authorize",
                "export-bootstrap",
                "observer",
                "status-publisher",
                "telemetry-sender",
            },
            "NODE_PROCEDURE_ACTION_INVALID",
        )
    if procedure == "spire-install":
        require(len(arguments) >= 4, "NODE_SPIRE_ARGUMENTS_INVALID")
        require(
            all(
                re.fullmatch(
                    r"(?:sha256:[a-f0-9]{64}|[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64})",
                    v,
                )
                for v in arguments[2:4]
            ),
            "NODE_SPIRE_IMMUTABLE_IMAGES_REQUIRED",
        )
    elif procedure == "spire-export":
        require(
            len(arguments) == 4
            and arguments[3]
            in {"server", "observer", "status-publisher", "telemetry-sender"},
            "NODE_INDEPENDENT_ROLE_REQUIRED",
        )
    environment = request["environment"]
    require(
        isinstance(environment, dict)
        and environment.keys() <= ENVIRONMENT
        and all(
            isinstance(v, str) and not any(c in v for c in "\0\r\n")
            for v in environment.values()
        ),
        "NODE_ENVIRONMENT_INVALID",
    )
    for key, value in environment.items():
        if key == "DRP_SPIRE_DOCKER_SOCKET_APPROVAL":
            require(value == "operator-root-equivalent-v1", "NODE_SOCKET_SCOPE_INVALID")
        elif key == "DOCKER_HOST":
            require(value.startswith("unix:///"), "NODE_LOCAL_DAEMON_REQUIRED")
            absolute(value.removeprefix("unix://"), inspect=target_filesystem)
        else:
            absolute(value, inspect=target_filesystem)
    require(
        type(request["timeout_seconds"]) is int
        and 1 <= request["timeout_seconds"] <= 7200,
        "NODE_TIMEOUT_INVALID",
    )
    source = request["source_files"]
    require(
        isinstance(source, dict)
        and bool(source)
        and PROCEDURES[procedure][1] in source
        and "tools/node_execution.py" in source,
        "NODE_SOURCE_MANIFEST_REQUIRED",
    )
    helper_hash = (
        globals()["__node_code_sha256__"]
        if "__node_code_sha256__" in globals()
        else checksum(Path(__file__).read_bytes())
    )
    require(
        source["tools/node_execution.py"] == helper_hash,
        "NODE_EXECUTOR_SOURCE_MISMATCH",
    )
    for name, value in source.items():
        require(
            isinstance(name, str)
            and bool(name)
            and not Path(name).is_absolute()
            and ".." not in Path(name).parts
            and isinstance(value, str)
            and re.fullmatch(r"[a-f0-9]{64}", value) is not None,
            "NODE_SOURCE_MANIFEST_INVALID",
        )
    require(isinstance(request["inputs"], dict), "NODE_INPUTS_INVALID")
    for name, value in request["inputs"].items():
        absolute(name, inspect=target_filesystem)
        require(
            isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None,
            "NODE_INPUT_HASH_INVALID",
        )
    runtime, entrypoint = PROCEDURES[procedure]
    return (
        root,
        state,
        [
            sys.executable if runtime == "python" else "sh",
            str(root / entrypoint),
            *arguments,
        ],
    )


def target(request: dict[str, Any], action: str) -> dict[str, Any]:
    """Own only a technical command receipt; never infer owner readiness."""
    require(action in {"execute", "status"}, "NODE_ACTION_INVALID")
    root, state, command = validate(request)
    operation = request["operation"]
    request_hash = checksum(encode(request))
    directory = state / operation
    receipt_path = directory / "receipt.json"
    if action == "status" and not receipt_path.exists():
        return {
            "status": "NOT_OBSERVED",
            "operation": operation,
            "request_sha256": request_hash,
            "owner_readiness_verified": False,
        }
    if action == "status":
        private(state, directory=True)
        private(directory, directory=True)
        require(not receipt_path.is_symlink(), "NODE_SYMLINK_REFUSED")
        private(receipt_path)
        result = read(receipt_path)
        require(result["request_sha256"] == request_hash, "NODE_OPERATION_CONFLICT")
        return result
    created = [path for path in [state, *state.parents] if not path.exists()]
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    private(state, directory=True)
    for path in created:
        sync_directory(path)
        sync_directory(path.parent)
    directory.mkdir(mode=0o700, exist_ok=True)
    private(directory, directory=True)
    sync_directory(state)
    lock_path = state / "scope.lock"
    require(
        not lock_path.is_symlink() and not receipt_path.is_symlink(),
        "NODE_SYMLINK_REFUSED",
    )
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ExecutionError("NODE_OPERATION_BUSY") from error
        if receipt_path.exists():
            private(receipt_path)
            result = read(receipt_path)
            require(result["request_sha256"] == request_hash, "NODE_OPERATION_CONFLICT")
            if action == "execute":
                require(
                    result["status"] != "INTENT",
                    "NODE_RESULT_UNKNOWN_RECONCILIATION_REQUIRED",
                )
            return result
        require(action == "execute", "NODE_RECEIPT_MISSING")
        for name, expected in request["source_files"].items():
            path = absolute(str(root / name))
            require(
                path.is_file() and checksum(path.read_bytes()) == expected,
                "NODE_SOURCE_DRIFT",
            )
        for name, expected in request["inputs"].items():
            path = absolute(name)
            private(path)
            require(checksum(path.read_bytes()) == expected, "NODE_INPUT_DRIFT")
        result = {
            "schema_version": 1,
            "status": "INTENT",
            "operation": operation,
            "request_sha256": request_hash,
            "owner_readiness_verified": False,
            "physical_distribution_verified": False,
            "queue_resume_issued": False,
        }
        save(receipt_path, result)
        environment = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "TMPDIR")
            if key in os.environ
        }
        environment.update(request["environment"])
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            with (
                (directory / "stdout").open("xb") as output,
                (directory / "stderr").open("xb") as error,
            ):
                os.chmod(directory / "stdout", 0o600)
                os.chmod(directory / "stderr", 0o600)
                completed = subprocess.run(
                    command,
                    cwd=root / Path(PROCEDURES[request["procedure"]][1]).parent,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=error,
                    timeout=request["timeout_seconds"],
                    check=False,
                )
            result.update(
                status="COMMAND_SUCCEEDED"
                if completed.returncode == 0
                else "OWNER_COMMAND_FAILED",
                exit_code=completed.returncode,
            )
        except (OSError, subprocess.TimeoutExpired):
            result["status"] = "RESULT_UNKNOWN"
        result["owner_output_retained_privately"] = True
        for name in ("stdout", "stderr"):
            path = directory / name
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
                result[name + "_sha256"] = checksum(path.read_bytes())
        save(receipt_path, result)
        return result


def ssh_command(binding: dict[str, Any]) -> list[str]:
    require(
        set(binding)
        == {
            "kind",
            "host",
            "user",
            "port",
            "identity_file",
            "known_hosts_file",
            "evidence_root",
        }
        and binding["kind"] == "ssh",
        "NODE_SSH_BINDING_INVALID",
    )
    require(
        isinstance(binding["host"], str)
        and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]{0,252}", binding["host"])
        is not None
        and isinstance(binding["user"], str)
        and re.fullmatch(r"[a-z_][a-z0-9_-]{0,63}", binding["user"]) is not None,
        "NODE_SSH_DESTINATION_INVALID",
    )
    require(
        type(binding["port"]) is int and 1 <= binding["port"] <= 65535,
        "NODE_SSH_PORT_INVALID",
    )
    identity, hosts = (
        absolute(binding["identity_file"]),
        absolute(binding["known_hosts_file"]),
    )
    private(identity)
    require(
        hosts.is_file() and hosts.stat().st_mode & 0o022 == 0,
        "NODE_KNOWN_HOSTS_INVALID",
    )
    options = [
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "GlobalKnownHostsFile=/dev/null",
        "UserKnownHostsFile=" + str(hosts),
        "IdentitiesOnly=yes",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "ForwardAgent=no",
        "ClearAllForwardings=yes",
        "ControlMaster=no",
        "ControlPath=none",
        "ConnectTimeout=20",
    ]
    return [
        "ssh",
        "-F",
        "/dev/null",
        *[part for option in options for part in ("-o", option)],
        "-i",
        str(identity),
        "-p",
        str(binding["port"]),
        "--",
        binding["user"] + "@" + binding["host"],
        shlex.join(["python3.13", "-c", BOOTSTRAP]),
    ]


def invoke(
    binding: dict[str, Any], request: dict[str, Any], action: str
) -> dict[str, Any]:
    validate(request, target_filesystem=binding == {"kind": "local"})
    if binding == {"kind": "local"}:
        return target(request, action)
    command = ssh_command(binding)
    evidence = absolute(binding["evidence_root"])
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    private(evidence, directory=True)
    directory = Path(tempfile.mkdtemp(prefix=request["operation"] + "-", dir=evidence))

    def retain(
        stdout: bytes | None, stderr: bytes | None, status: str, exit_code: int | None
    ) -> None:
        for name, data in (("stdout", stdout), ("stderr", stderr)):
            descriptor = os.open(
                directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(data or b"")
                output.flush()
                os.fsync(output.fileno())
        save(
            directory / "result.json",
            {
                "status": status,
                "exit_code": exit_code,
                "operation": request["operation"],
                "request_sha256": checksum(encode(request)),
            },
        )

    code = Path(__file__).read_bytes()
    payload = encode(
        {
            "code": base64.b64encode(code).decode(),
            "code_sha256": checksum(code),
            "request": request,
            "action": action,
        }
    )
    require(len(payload) <= LIMIT, "NODE_INPUT_TOO_LARGE")
    try:
        result = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            timeout=request["timeout_seconds"] + 60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        retain(
            getattr(error, "stdout", None),
            getattr(error, "stderr", None),
            "TRANSPORT_RESULT_UNKNOWN",
            None,
        )
        raise ExecutionError("NODE_TRANSPORT_UNKNOWN_RECONCILE_BEFORE_RETRY") from error
    retain(result.stdout, result.stderr, "TRANSPORT_EXITED", result.returncode)
    require(result.returncode == 0, "NODE_TRANSPORT_UNKNOWN_RECONCILE_BEFORE_RETRY")
    require(len(result.stdout) <= 65536, "NODE_REPLY_INVALID")
    reply = json.loads(result.stdout)
    require(
        isinstance(reply, dict)
        and reply.get("request_sha256") == checksum(encode(request))
        and reply.get("operation") == request["operation"],
        "NODE_REPLY_INVALID",
    )
    return reply


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("execute", "status"))
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        private(absolute(str(args.binding.absolute())))
        private(absolute(str(args.request.absolute())))
        result = invoke(read(args.binding), read(args.request), args.action)
    except ExecutionError as error:
        print(json.dumps({"status": "BLOCKED", "code": str(error)}))
        return 2
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "code": "NODE_EXECUTION_REFUSED_OR_RESULT_UNKNOWN",
                }
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"COMMAND_SUCCEEDED", "NOT_OBSERVED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
