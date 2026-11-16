"""Bounded real-Docker qualification of the generic executor, not platform E2E."""

import argparse
import copy
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any, cast

QUALIFICATION_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--image", required=True)
args = parser.parse_args()
SOURCE = Path(__file__).resolve().parents[1]
BASE: Path = args.output.resolve()
assert not BASE.is_relative_to(SOURCE)
BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
assert re.fullmatch(
    r"(?:sha256:[a-f0-9]{64}|[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64})", args.image
)
sys.dont_write_bytecode = True
sys.path.insert(0, str(SOURCE))
from tools import node_execution as node  # noqa: E402

os.umask(0o077)
ROOT = BASE / ("owner-compose-live-" + uuid.uuid4().hex[:10])
ROOT.mkdir(mode=0o700)
SCOPE = "aelev-owner-" + uuid.uuid4().hex[:10]
PROJECTS = [SCOPE + "-one", SCOPE + "-two"]
ROWS: list[dict[str, Any]] = []


def call(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if check and result.returncode:
        raise RuntimeError(
            "QUALIFICATION_COMMAND_FAILED:" + args[0] + ":" + str(result.returncode)
        )
    return result


def baseline() -> dict[str, list[str]]:
    listing = call(
        [
            "docker",
            "ps",
            "--all",
            "--no-trunc",
            "--format",
            '{{.ID}}|{{.Image}}|{{.Label "com.docker.compose.project"}}',
        ]
    ).stdout
    ids = []
    for line in listing.splitlines():
        identifier, image, project = line.split("|")
        if (
            image.startswith(("aelev/", "drp/", "ghcr.io/sapogov1978/"))
            or project.startswith(("aelev", "drp"))
        ) and not project.startswith(SCOPE):
            ids.append(identifier)
    if not ids:
        return {}
    template = '{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.State.Status}}|{{.HostConfig.AutoRemove}}|{{.Config.Image}}|{{index .Config.Labels "com.docker.compose.project"}}'
    inspected = call(["docker", "inspect", "--format", template, *ids], check=False)
    assert inspected.returncode in (0, 1)
    assert not inspected.returncode or all(
        "No such object:" in line for line in inspected.stderr.splitlines() if line
    )
    output = inspected.stdout
    selected = {}
    for line in output.splitlines():
        parts = line.split("|")
        if (
            parts[4] == "false"
            and (
                parts[5].startswith(("aelev/", "drp/", "ghcr.io/sapogov1978/"))
                or parts[6].startswith(("aelev", "drp"))
            )
            and not parts[6].startswith(SCOPE)
        ):
            selected[parts[0]] = parts[1:5]
    return selected


def record(case: str, **values: Any) -> None:
    row = {"case": case, "status": "PASS", **values}
    ROWS.append(row)
    print(json.dumps(row), flush=True)


original = baseline()
image = json.loads(call(["docker", "image", "inspect", args.image]).stdout)[0]
assert image["Architecture"] in {"arm64", "amd64"}
IMAGE = image["Id"]
network_ids = call(["docker", "network", "ls", "--quiet"]).stdout.split()
allocated = []
if network_ids:
    for line in call(
        [
            "docker",
            "network",
            "inspect",
            "--format",
            "{{json .IPAM.Config}}",
            *network_ids,
        ]
    ).stdout.splitlines():
        for item in json.loads(line) or []:
            if item.get("Subnet"):
                allocated.append(ipaddress.ip_network(item["Subnet"], strict=False))
subnets = []
for candidate in ipaddress.ip_network("10.250.0.0/16").subnets(new_prefix=28):
    if not any(
        candidate.overlaps(existing) for existing in allocated if existing.version == 4
    ):
        subnets.append(str(candidate))
    if len(subnets) == len(PROJECTS):
        break
assert len(subnets) == len(PROJECTS)
fixture = ROOT / "source"
(fixture / "tools").mkdir(parents=True)
for name in ("node_execution.py", "owner_compose.py"):
    shutil.copyfile(SOURCE / "tools" / name, fixture / "tools" / name)
owner_source = ROOT / "owner-source"
owner_source.mkdir()
worker = """import os,time
with open('/data/history','ab') as stream:
 stream.write(b'worker-start\\n');stream.flush();os.fsync(stream.fileno())
time.sleep(900)
"""
task = "import os; f=open('/data/history','ab'); f.write(b'task-effect\\n'); f.flush(); os.fsync(f.fileno()); f.close()"
fail_task = "import os; f=open('/data/history','ab'); f.write(b'failed-task-effect\\n'); f.flush(); os.fsync(f.fileno()); f.close(); raise SystemExit(7)"
common = {"image": IMAGE, "restart": "no", "volumes": ["state:/data"]}
model = {
    "services": {
        "worker": {
            **common,
            "command": ["python", "-c", worker],
            "environment": {"QUALIFICATION_LITERAL": "literal$$QUALIFICATION_UNSET"},
            "depends_on": {"neighbor": {"condition": "service_started"}},
        },
        "neighbor": {
            "image": IMAGE,
            "restart": "no",
            "command": ["python", "-c", "import time;time.sleep(900)"],
        },
        "task": {
            **common,
            "command": ["python", "-c", task],
            "build": {"context": ".", "dockerfile": "MUST_NOT_BUILD.Dockerfile"},
        },
        "fail-task": {**common, "command": ["python", "-c", fail_task]},
    },
    "volumes": {
        "state": {"name": "${OWNER_VOLUME}", "labels": {"aelev.qualification": SCOPE}}
    },
    "networks": {
        "default": {
            "labels": {"aelev.qualification": SCOPE},
            "ipam": {"config": [{"subnet": "${OWNER_SUBNET}"}]},
        }
    },
}
for service in model["services"].values():
    service["labels"] = {"aelev.qualification": SCOPE}
(owner_source / "compose.json").write_text(json.dumps(model))
catalog = {
    "schema_version": 1,
    "product": "qualification",
    "roles": [
        {
            "id": name,
            "owner": ".",
            "kind": "operation" if name in {"task", "fail-task"} else "runtime",
            "recipes": ["compose.json"],
            "opaque": False,
        }
        for name in model["services"]
    ],
}
(owner_source / "deployment-catalog.json").write_text(json.dumps(catalog))
FILES = {
    str(path.relative_to(fixture)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in fixture.rglob("*")
    if path.is_file()
}
OWNER_FILES = {
    str(path.relative_to(owner_source)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in owner_source.rglob("*")
    if path.is_file()
}


def definition(project: str, role: str) -> dict[str, Any]:
    workspace = ROOT / project
    workspace.mkdir(mode=0o700, exist_ok=True)
    env = workspace / "owner.env"
    env.write_text(
        "OWNER_VOLUME="
        + project
        + "-state\nOWNER_SUBNET="
        + subnets[PROJECTS.index(project)]
        + "\n"
    )
    env.chmod(0o600)
    return {
        "schema_version": 1,
        "executor_root": str(fixture),
        "executor_files": FILES,
        "owner_root": str(owner_source),
        "owner_files": OWNER_FILES,
        "catalog": "deployment-catalog.json",
        "role": "qualification/" + role,
        "recipes": ["compose.json"],
        "project_directory": str(workspace),
        "project": project,
        "env_file": str(env),
        "inputs": {str(env): node.checksum(env.read_bytes())},
        "image": IMAGE,
        "configuration_sha256": None,
    }


def execute(
    value: dict[str, Any],
    action: str,
    *,
    expect: str = "COMMAND_SUCCEEDED",
    request: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if request is None:
        operation = str(uuid.uuid4())
        definition_path = ROOT / (operation + ".json")
        definition_path.write_text(json.dumps(value))
        definition_path.chmod(0o600)
        digest = node.checksum(definition_path.read_bytes())
        request = {
            "schema_version": 1,
            "operation": operation,
            "source_root": value["executor_root"],
            "source_files": value["executor_files"],
            "state_root": str(ROOT / (value["project"] + "-journal")),
            "procedure": "owner-compose",
            "arguments": [
                action,
                "--request",
                str(definition_path),
                "--request-sha256",
                digest,
            ],
            "inputs": {**value["inputs"], str(definition_path): digest},
            "environment": {},
            "timeout_seconds": 120,
        }
    receipt = node.invoke({"kind": "local"}, request, "execute")
    assert receipt["status"] == expect, (action, receipt)
    output = Path(request["state_root"]) / request["operation"] / "stdout"
    result = cast(dict[str, Any], json.loads(output.read_text()))
    return result, request, receipt


def inspect(identifier: str) -> dict[str, Any]:
    return cast(
        dict[str, Any], json.loads(call(["docker", "inspect", identifier]).stdout)[0]
    )


def history(identifier: str) -> bytes:
    target = ROOT / ("history-" + uuid.uuid4().hex)
    call(["docker", "cp", identifier + ":/data/history", str(target)])
    return target.read_bytes()


try:
    for project in PROJECTS:
        assert not call(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ]
        ).stdout.strip()
    first = definition(PROJECTS[0], "worker")
    assert first["owner_root"] != first["executor_root"]
    assert "tools/owner_compose.py" not in first["owner_files"]
    record(
        "independent-executor-and-owner-source-generations",
        separate_roots=True,
        helper_copied_into_owner=False,
    )
    planned, _, _ = execute(first, "plan")
    first["configuration_sha256"] = planned["configuration_sha256"]
    started, request, receipt = execute(first, "start")
    identifier = started["container_id"]
    before = inspect(identifier)
    for _readiness_attempt in range(30):
        try:
            first_history = history(identifier)
            break
        except RuntimeError:
            time.sleep(0.2)
    else:
        raise RuntimeError("QUALIFICATION_WORKER_NOT_READY")
    assert history(identifier) == b"worker-start\n"
    assert not call(
        [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project=" + PROJECTS[0],
            "--filter",
            "label=com.docker.compose.service=neighbor",
        ]
    ).stdout.strip()
    assert (
        "QUALIFICATION_LITERAL=literal$QUALIFICATION_UNSET" in before["Config"]["Env"]
    )
    record("selected-role-and-literal-config", neighbor_started=False)
    _, _, again = execute(first, "start", request=request)
    assert again == receipt
    repeated, _, _ = execute(first, "start")
    assert (
        repeated["container_id"] == identifier
        and history(identifier) == b"worker-start\n"
    )
    assert inspect(identifier)["State"]["StartedAt"] == before["State"]["StartedAt"]
    record("same-operation-and-new-operation-repeat", same_container=True, starts=1)
    changed = copy.deepcopy(first)
    changed["configuration_sha256"] = "a" * 64
    rejected, _, _ = execute(changed, "start", expect="OWNER_COMMAND_FAILED")
    assert rejected["code"] == "OWNER_COMPOSE_CONFIGURATION_DRIFT"
    assert inspect(identifier)["Id"] == identifier
    record("configuration-drift-refused", container_preserved=True)
    second = definition(PROJECTS[1], "worker")
    planned, _, _ = execute(second, "plan")
    second["configuration_sha256"] = planned["configuration_sha256"]
    replica, _, _ = execute(second, "start")
    replica_id = replica["container_id"]
    for _replica_readiness_attempt in range(30):
        try:
            second_history = history(replica_id)
            break
        except RuntimeError:
            time.sleep(0.2)
    assert second_history == b"worker-start\n"
    assert {
        row["Name"] for row in inspect(identifier)["Mounts"] if row["Type"] == "volume"
    }.isdisjoint(
        {
            row["Name"]
            for row in inspect(replica_id)["Mounts"]
            if row["Type"] == "volume"
        }
    )
    record("independent-replica", distinct_container=True, distinct_volume=True)
    task_value = definition(PROJECTS[0], "task")
    plan, _, _ = execute(task_value, "plan")
    task_value["configuration_sha256"] = plan["configuration_sha256"]
    _, task_request, task_receipt = execute(task_value, "task")
    _, _, repeated_task = execute(task_value, "task", request=task_request)
    assert repeated_task == task_receipt
    assert history(identifier) == b"worker-start\ntask-effect\n"
    record("one-shot-task-and-replay", task_effects=1, implicit_build=False)
    failing = definition(PROJECTS[0], "fail-task")
    plan, _, _ = execute(failing, "plan")
    failing["configuration_sha256"] = plan["configuration_sha256"]
    failed, failed_request, failed_receipt = execute(
        failing, "task", expect="OWNER_COMMAND_FAILED"
    )
    assert failed["owner_exit_code"] == 7
    _, _, failed_again = execute(
        failing, "task", expect="OWNER_COMMAND_FAILED", request=failed_request
    )
    assert failed_again == failed_receipt
    assert history(identifier).count(b"failed-task-effect") == 1
    record("failed-task-not-replayed", native_exit_code=7, task_effects=1)
    call(["docker", "stop", "--time", "5", identifier])
    stopped_before = inspect(identifier)
    refused, _, _ = execute(first, "start", expect="OWNER_COMMAND_FAILED")
    assert refused["code"] == "OWNER_COMPOSE_OWNER_HANDOFF_REQUIRED"
    stopped_after = inspect(identifier)
    assert not stopped_after["State"]["Running"]
    assert stopped_before["State"]["FinishedAt"] == stopped_after["State"]["FinishedAt"]
    assert history(identifier).count(b"worker-start") == 1
    record(
        "stopped-role-not-resumed", same_stopped_container=True, preserved_history=True
    )
    record(
        "qualification-complete",
        generic_executor_only=True,
        full_platform_e2e_verified=False,
    )
except BaseException:
    for project in PROJECTS:
        ids = call(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ]
        ).stdout.split()
        for identifier in ids:
            data = inspect(identifier)
            (ROOT / (identifier + "-failure-state.json")).write_text(
                json.dumps({"state": data["State"], "image": data["Image"]}, indent=2)
            )
            logs = call(["docker", "logs", identifier], check=False)
            (ROOT / (identifier + "-failure.stdout")).write_text(logs.stdout)
            (ROOT / (identifier + "-failure.stderr")).write_text(logs.stderr)
    raise
finally:
    for project in PROJECTS:
        ids = call(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ]
        ).stdout.split()
        if ids:
            assert all(
                inspect(identifier)["Config"]["Labels"].get("aelev.qualification")
                == SCOPE
                for identifier in ids
            )
            call(["docker", "rm", "--force", *ids])
        volumes = call(
            [
                "docker",
                "volume",
                "ls",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ]
        ).stdout.split()
        for name in volumes:
            data = json.loads(call(["docker", "volume", "inspect", name]).stdout)[0]
            assert data["Labels"].get("aelev.qualification") == SCOPE
            call(["docker", "volume", "rm", name])
        networks = call(
            [
                "docker",
                "network",
                "ls",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ]
        ).stdout.split()
        for name in networks:
            data = json.loads(call(["docker", "network", "inspect", name]).stdout)[0]
            assert (
                data["Labels"].get("aelev.qualification") == SCOPE
                and not data["Containers"]
            )
            call(["docker", "network", "rm", name])
    after = baseline()
    assert all(after.get(identifier) == state for identifier, state in original.items())
    record(
        "cleanup-and-working-baseline",
        containers_preserved=len(original),
        owned_resources_removed=True,
    )
    output = {
        "status": (
            "PASS"
            if any(row["case"] == "qualification-complete" for row in ROWS)
            else "FAIL"
        ),
        "scope": "Generic owner-compose through durable node executor on one native Docker Engine",
        "image": IMAGE,
        "architecture": image["Architecture"],
        "qualification_source_sha256": QUALIFICATION_SOURCE_SHA256,
        "code_sha256": {
            name: FILES["tools/" + name]
            for name in ("node_execution.py", "owner_compose.py")
        },
        "cases": ROWS,
        "physical_distribution_verified": False,
        "full_platform_e2e_verified": False,
        "inference_requested": False,
        "queue_resume_issued": False,
    }
    (ROOT / "verification.json").write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps({"evidence": str(ROOT), "status": output["status"]}), flush=True)
