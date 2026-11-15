"""Start the complete canonical local runtime without acceptance scenarios."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools import full_local, spire_local, spire_renewal

FIXTURE_VARIABLE = "AELEV_INFRASTRUCTURE_FULL_LOCAL_PROVIDER_FIXTURE"
SPIRE_COMPOSITION_VARIABLE = "AELEV_INFRASTRUCTURE_SPIRE_INSTALLED_COMPOSE"
SPIRE_ROLES = {
    "spire-server",
    "spire-observer",
    "spire-status-publisher",
    "spire-telemetry-sender",
}


def spire_projection(root: Path) -> Path:
    """Resolve only an explicit public binding, including an external export."""
    selected = os.environ.get(SPIRE_COMPOSITION_VARIABLE)
    if selected is not None:
        path = Path(selected)
        if not path.is_absolute():
            raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_PUBLIC_EXPORT_REQUIRED")
        return path
    directory = root / "spire"
    try:
        selected = (
            full_local.read_env(root / ".env", root).get(SPIRE_COMPOSITION_VARIABLE)
            if (root / ".env").exists()
            else None
        )
        if selected:
            path = Path(selected)
            if not path.is_absolute():
                raise ValueError
            return path
        lines = (directory / ".env").read_text().splitlines()
        if len(lines) != 2 or lines[0] != "# AELEV SPIRE installed composition v1":
            raise ValueError
        key, separator, value = lines[1].partition("=")
        if key != SPIRE_COMPOSITION_VARIABLE or not separator:
            raise ValueError
        if (
            re.fullmatch(
                r"\./runtime/installed-composition/[a-f0-9]{64}/compose\.json", value
            )
            is None
        ):
            raise ValueError
        return directory / value
    except (OSError, ValueError) as error:
        raise full_local.FullLocalError(
            "FULL_RUNTIME_SPIRE_PUBLIC_EXPORT_REQUIRED"
        ) from error


def spire_binding(root: Path) -> dict[str, Any]:
    """Require the owner's complete public export, never template defaults."""
    try:
        path = spire_projection(root)
        generation = path.parent.name
        if (
            path.name != "compose.json"
            or re.fullmatch(r"[a-f0-9]{64}", generation) is None
        ):
            raise ValueError
        composition = json.loads(path.read_text())
        receipt = json.loads((path.parent.parent / "receipt.json").read_text())
        if (
            receipt["generation"] != generation
            or set(receipt) != SPIRE_ROLES | {"generation"}
            or set(composition["services"]) != SPIRE_ROLES
        ):
            raise ValueError
        for name in SPIRE_ROLES:
            row = receipt[name]
            if (
                re.fullmatch(r"[a-f0-9]{64}", row["container_id"]) is None
                or re.fullmatch(r"sha256:[a-f0-9]{64}", row["image"]) is None
                or composition["services"][name]["image"] != row["image"]
                or type(row["native_restarts"]) is not int
                or row["native_restarts"] < 0
                or row["restart"] != composition["services"][name]["restart"]
                or re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", row["compose_project"])
                is None
            ):
                raise ValueError
        if len({receipt[name]["compose_project"] for name in SPIRE_ROLES}) != 1:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise full_local.FullLocalError(
            "FULL_RUNTIME_SPIRE_PUBLIC_EXPORT_REQUIRED"
        ) from error
    return receipt


def verify_spire_binding(
    root: Path, model: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Verify exact owner containers in their existing physical coordinator."""
    receipt = spire_binding(root)
    project = receipt["spire-server"]["compose_project"]
    current = containers(project, root)
    verified = {}
    for name in SPIRE_ROLES:
        expected = receipt[name]
        definition = model["services"].get(name, {})
        if (
            definition.get("image") != expected["image"]
            or definition.get("restart") != expected["restart"]
        ):
            raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_EXPORT_STALE")
        rows = [
            row
            for row in current
            if (row["Config"].get("Labels") or {}).get("com.docker.compose.service")
            == name
            and (row["Config"].get("Labels") or {})
            .get("com.docker.compose.oneoff", "false")
            .lower()
            != "true"
        ]
        if (
            len(rows) != 1
            or rows[0]["Id"] != expected["container_id"]
            or rows[0]["Image"] != expected["image"]
            or rows[0]["RestartCount"] != expected["native_restarts"]
            or rows[0]["Config"]["Labels"].get("io.aelev.owner")
            != "infrastructure-spire"
        ):
            raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_EXPORT_STALE")
        policy = rows[0]["HostConfig"]["RestartPolicy"]
        limit = policy.get("MaximumRetryCount")
        if policy.get("Name") == "no" and limit == 0:
            remaining = "no"
        elif (
            policy.get("Name") == "on-failure"
            and type(limit) is int
            and 1 <= limit <= 3
        ):
            attempts = max(0, limit - rows[0]["RestartCount"])
            remaining = "on-failure:" + str(attempts) if attempts else "no"
        else:
            raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_EXPORT_STALE")
        if remaining != expected["restart"]:
            raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_EXPORT_STALE")
        verified[name] = rows[0]
    return verified


def initial_binding_required(root: Path) -> bool:
    """Distinguish a new installation from an invalid existing public binding."""
    if SPIRE_COMPOSITION_VARIABLE in os.environ:
        return False
    if (root / ".env").exists() and SPIRE_COMPOSITION_VARIABLE in full_local.read_env(
        root / ".env", root
    ):
        return False
    path = root / "spire/.env"
    return not (
        path.exists()
        and path.read_text().startswith("# AELEV SPIRE installed composition v1\n")
    )


def bootstrap_spire(root: Path, source: Path, manifest: dict[str, Any]) -> None:
    """Delegate native installation, then persist only its public projection."""
    owner = full_local.read_env(root / "spire/.env", root)
    images = {
        row["target"]: row["image"]
        for row in manifest["local_builds"]
        if row["context"] == "spire"
    }
    result = json.loads(
        command(
            [
                sys.executable,
                str(root / "spire/bootstrap.py"),
                "up",
                "--project",
                owner["AELEV_INFRASTRUCTURE_SPIRE_COMPOSE_PROJECT"],
                "--runtime-image",
                images["runtime"],
                "--installer-image",
                images["operator-installer"],
                "--initial-input",
                str(source.resolve()),
                "--output-root",
                str(root / "runtime/spire-installed"),
            ],
            root,
        )
    )
    if (
        result.get("status") != "SPIRE_RUNTIME_INSTALLED"
        or result.get("project") != owner["AELEV_INFRASTRUCTURE_SPIRE_COMPOSE_PROJECT"]
    ):
        raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_BOOTSTRAP_INCOMPLETE")
    projection = Path(result["composition"])
    if (
        not projection.is_absolute()
        or not projection.is_file()
        or any(character in str(projection) for character in ("\n", "\r", "$"))
    ):
        raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_PUBLIC_EXPORT_REQUIRED")
    path = root / ".env"
    original = path.read_bytes()
    if SPIRE_COMPOSITION_VARIABLE in full_local.read_env(path, root):
        raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_BINDING_ALREADY_EXISTS")
    raw = original + (b"" if original.endswith(b"\n") else b"\n")
    raw += (SPIRE_COMPOSITION_VARIABLE + "=" + str(projection) + "\n").encode()
    temporary = root / "runtime"
    temporary.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, pending = tempfile.mkstemp(prefix=".spire-binding-", dir=temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if path.read_bytes() != original:
            raise full_local.FullLocalError("FULL_RUNTIME_ENV_CHANGED")
        os.replace(pending, path)
        if os.name != "nt":
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(pending).unlink(missing_ok=True)
    spire_binding(root)


def require_spire_ready(root: Path, model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A stopped or unhealthy native owner requires its own recovery interface."""
    rows = verify_spire_binding(root, model)
    if any(
        row["State"].get("Running") is not True
        or row["State"].get("Health", {}).get("Status") != "healthy"
        for row in rows.values()
    ):
        raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_OWNER_RUNTIME_NOT_READY")
    return rows


def record_command_failure(
    root: Path,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
    *,
    unknown: bool,
) -> None:
    """Keep sensitive native diagnostics private, without changing the outcome."""
    directory = root / "runtime"
    try:
        if directory.is_symlink():
            return
        directory.mkdir(mode=0o700, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix="full-runtime-failure-", dir=directory))
        for name, value in (("stdout", stdout), ("stderr", stderr)):
            spire_local.private(
                output / name,
                value.encode() if isinstance(value, str) else value or b"",
            )
        spire_local.save(
            output / "result.json", {"status": "UNKNOWN" if unknown else "FAILED"}
        )
    except OSError:
        # Diagnostic-storage failure must not erase an uncertain operation result.
        pass


def command(arguments: list[str], root: Path) -> str:
    try:
        result = subprocess.run(
            arguments, cwd=root, capture_output=True, text=True, timeout=1200
        )
    except subprocess.TimeoutExpired as error:
        record_command_failure(root, error.stdout, error.stderr, unknown=True)
        raise full_local.FullLocalError(
            "FULL_RUNTIME_COMMAND_RESULT_UNKNOWN"
        ) from error
    if result.returncode:
        record_command_failure(
            root,
            result.stdout,
            result.stderr,
            unknown=result.stderr.strip() == "SPIRE_BOOTSTRAP_RESULT_UNKNOWN",
        )
        if len(arguments) > 1 and arguments[1] == str(root / "spire/bootstrap.py"):
            code = result.stderr.strip()
            if re.fullmatch(r"SPIRE_BOOTSTRAP_[A-Z_]{1,96}", code):
                if code == "SPIRE_BOOTSTRAP_RESULT_UNKNOWN":
                    raise full_local.FullLocalError(
                        "FULL_RUNTIME_COMMAND_RESULT_UNKNOWN"
                    )
                raise full_local.FullLocalError("FULL_RUNTIME_" + code)
        raise full_local.FullLocalError("FULL_RUNTIME_COMMAND_FAILED")
    return result.stdout


def contract(root: Path) -> dict[str, Any]:
    manifest, errors = full_local.validate_contract(root)
    if errors:
        raise full_local.FullLocalError("FULL_RUNTIME_CONTRACT_INVALID")
    return manifest


def plan(root: Path) -> dict[str, Any]:
    manifest = contract(root)
    return {
        "schema_version": 1,
        "profile": "full-runtime",
        "release": manifest["release"],
        "owners": manifest["owners"],
        "services": manifest["root_compose"]["required_services"],
        "compose_profiles": manifest["root_compose"]["profiles"],
        "provider_fixture_selected": False,
        "inference_requested": False,
        "process_control_changed": False,
        "spire_initial_input_supported": True,
        "spire_local_input_supported": True,
        "spire_renewal_input_supported": True,
    }


def containers(project: str, root: Path) -> list[dict[str, Any]]:
    identifiers = command(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.ID}}",
        ],
        root,
    ).splitlines()
    if not identifiers:
        return []
    return json.loads(command(["docker", "inspect", *identifiers], root))


def guard_existing(
    root: Path,
    prefix: list[str],
    model: dict[str, Any],
    required: set[str],
    *,
    delegated: dict[str, str] | None = None,
) -> None:
    current = containers(str(model["name"]), root)
    if not current:
        return
    hashes = dict(
        line.split()
        for line in command(
            [*prefix, "--profile", "*", "config", "--hash", "*"], root
        ).splitlines()
        if line.strip()
    )
    replicas: Counter[str] = Counter()
    image_ids: dict[str, str] = {}
    for row in current:
        labels = row["Config"].get("Labels") or {}
        if labels.get("com.docker.compose.oneoff", "false").lower() == "true":
            continue
        service = labels.get("com.docker.compose.service")
        if delegated and service in delegated:
            if row["Id"] != delegated[service]:
                raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_EXPORT_STALE")
            continue
        if service not in required:
            if row["State"].get("Running") is True:
                raise full_local.FullLocalError("FULL_RUNTIME_UNDECLARED_WRITER")
            continue
        replicas[service] += 1
        if (
            service not in hashes
            or labels.get("com.docker.compose.config-hash") != hashes[service]
        ):
            raise full_local.FullLocalError("FULL_RUNTIME_OWNER_HANDOFF_REQUIRED")
        reference = model["services"][service]["image"]
        if reference not in image_ids:
            image_ids[reference] = command(
                ["docker", "image", "inspect", "--format", "{{.Id}}", reference], root
            ).strip()
        if row["Image"] != image_ids[reference]:
            raise full_local.FullLocalError("FULL_RUNTIME_IMAGE_HANDOFF_REQUIRED")
    for service, count in replicas.items():
        expected = model["services"][service].get("deploy", {}).get("replicas", 1)
        if count != expected:
            raise full_local.FullLocalError("FULL_RUNTIME_REPLICA_HANDOFF_REQUIRED")


def completed_dependencies(model: dict[str, Any]) -> set[str]:
    return {
        name
        for service in model["services"].values()
        for name, requirement in service.get("depends_on", {}).items()
        if requirement.get("condition") == "service_completed_successfully"
    }


def states(
    model: dict[str, Any], required: set[str], root: Path
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {name: [] for name in required}
    delegated = verify_spire_binding(root, model) if required & SPIRE_ROLES else {}
    for row in containers(str(model["name"]), root):
        labels = row["Config"].get("Labels") or {}
        if labels.get("com.docker.compose.oneoff", "false").lower() == "true":
            continue
        name = labels.get("com.docker.compose.service")
        if name in delegated and row["Id"] != delegated[name]["Id"]:
            raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_EXPORT_STALE")
        if name in result and name not in delegated:
            result[name].append(row["State"])
    for name, row in delegated.items():
        if name in result:
            result[name].append(row["State"])
    return result


def pending_services(
    model: dict[str, Any], required: set[str], root: Path
) -> list[str]:
    current = states(model, required, root)
    completed = completed_dependencies(model)
    pending = []
    for name in sorted(required):
        description = model["services"][name]
        rows = current[name]
        replicas = description.get("deploy", {}).get("replicas", 1)
        ready = len(rows) == replicas
        for row in rows:
            if name in completed:
                ready = (
                    ready and row.get("Status") == "exited" and row.get("ExitCode") == 0
                )
            elif description.get("healthcheck") or row.get("Health"):
                ready = (
                    ready
                    and row.get("Running") is True
                    and row.get("Health", {}).get("Status") == "healthy"
                )
            else:
                ready = ready and row.get("Running") is True
        if not ready:
            pending.append(name)
    return pending


def wait_ready(
    model: dict[str, Any], required: set[str], root: Path, *, timeout: int = 600
) -> None:
    deadline = time.monotonic() + timeout
    pending = sorted(required)
    while time.monotonic() < deadline:
        pending = pending_services(model, required, root)
        if not pending:
            return
        time.sleep(3)
    raise full_local.FullLocalError(
        "FULL_RUNTIME_READINESS_DEADLINE:" + ",".join(pending[:10])
    )


def start(prefix: list[str], names: list[str], root: Path) -> None:
    if names:
        command(
            [
                *prefix,
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
                "--no-recreate",
                *names,
            ],
            root,
        )


def up(
    root: Path,
    manifest: dict[str, Any],
    *,
    spire_initial_input: Path | None = None,
    spire_local_input: Path | None = None,
    spire_renewal_input: Path | None = None,
) -> dict[str, Any]:
    prefix = full_local.compose_prefix(manifest, root)
    model = json.loads(
        command([*prefix, "--profile", "*", "config", "--format", "json"], root)
    )
    required = set(manifest["root_compose"]["required_services"])
    if not required <= model["services"].keys():
        raise full_local.FullLocalError("FULL_RUNTIME_SERVICE_MISSING")
    fresh = initial_binding_required(root) and (
        spire_initial_input is not None or spire_local_input is not None
    )
    native = {} if fresh else require_spire_ready(root, model)
    guard_existing(
        root,
        prefix,
        model,
        required,
        delegated={name: row["Id"] for name, row in native.items()},
    )
    print("[full-runtime] starting Kafka", file=sys.stderr, flush=True)
    start(prefix, ["kafka"], root)
    wait_ready(model, {"kafka"}, root, timeout=300)
    print("[full-runtime] preparing owner configuration", file=sys.stderr, flush=True)
    for name in sorted(model["services"]):
        if (
            name.endswith("-configuration-init")
            and name != "platform-ui-configuration-init"
            and not name.startswith("spire-")
        ):
            command(
                [*prefix, "--profile", "provision", "run", "--rm", "--no-deps", name],
                root,
            )
    print("[full-runtime] starting operator identity", file=sys.stderr, flush=True)
    start(prefix, ["keycloak"], root)
    wait_ready(model, {"keycloak-database", "keycloak"}, root)
    tasks = [
        "platform-ui-configuration-init",
        "grafana-oidc-provision",
        "blackbox-exporter-operator-provision",
        "prometheus-operator-provision",
        "translator-api-provision",
    ]
    tasks.extend(
        sorted(
            name
            for name in model["services"]
            if name.endswith("-status-provision")
            and not name.startswith("spire-")
            and name.replace("-status-provision", "-status-publisher") in required
        )
    )
    for name in tasks:
        if name not in model["services"]:
            raise full_local.FullLocalError("FULL_RUNTIME_PROVISION_TASK_MISSING")
        print("[full-runtime] provisioning " + name, file=sys.stderr, flush=True)
        command(
            [*prefix, "--profile", "provision", "run", "--rm", "--no-deps", name], root
        )
    print(
        "[full-runtime] preparing object-store replication", file=sys.stderr, flush=True
    )
    replication = "site-replication-applier"
    dependencies = list(model["services"][replication].get("depends_on", {}))
    start(prefix, dependencies, root)
    wait_ready(model, set(dependencies), root)
    command(
        [*prefix, "--profile", "provision", "run", "--rm", "--no-deps", replication],
        root,
    )
    print("[full-runtime] starting declared core roles", file=sys.stderr, flush=True)
    start(prefix, sorted(required - SPIRE_ROLES), root)
    if fresh:
        wait_ready(model, required - SPIRE_ROLES, root)
        selected = spire_initial_input
        if spire_local_input is not None:
            print(
                "[full-runtime] preparing local SPIRE owner inputs",
                file=sys.stderr,
                flush=True,
            )
            selected = spire_local.prepare(root, spire_local_input)
        if selected is None:
            raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_PUBLIC_EXPORT_REQUIRED")
        bootstrap_spire(root, selected, manifest)
        model = json.loads(
            command([*prefix, "--profile", "*", "config", "--format", "json"], root)
        )
        require_spire_ready(root, model)
    print("[full-runtime] checking all owner coordinators", file=sys.stderr, flush=True)
    wait_ready(model, required, root)
    renewal = None
    if spire_renewal_input is not None:
        print(
            "[full-runtime] checking three SPIRE renewal deliveries",
            file=sys.stderr,
            flush=True,
        )
        renewal = spire_renewal.apply(
            root, spire_renewal_input, require_spire_ready(root, model)
        )
    provisioner_retired = None
    if (
        spire_local_input is not None
        and (root / "runtime/spire-local-input/receipt.json").is_file()
    ):
        provisioner_retired = spire_local.retire(root)
    return {
        **plan(root),
        "status": "RUNTIME_HEALTHY",
        "owner_coordinators": {
            "core": model["name"],
            "spire": spire_binding(root)["spire-server"]["compose_project"],
        },
        "provider_admission_proven": False,
        "spire_local_provisioner_retired": provisioner_retired,
        "spire_renewal": renewal,
    }


def execute_full(
    operation: str,
    root: Path,
    *,
    no_build: bool,
    verify_scope: Callable[[], None],
    initialize_core: Callable[[], None] | None = None,
    spire_initial_input: Path | None = None,
    spire_local_input: Path | None = None,
    spire_renewal_input: Path | None = None,
) -> None:
    if spire_initial_input is not None and spire_local_input is not None:
        raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_INPUT_OPTIONS_CONFLICT")
    if spire_renewal_input is not None:
        if operation != "up":
            raise full_local.FullLocalError("FULL_RUNTIME_SPIRE_RENEWAL_UP_REQUIRED")
        if (
            os.environ.get("DRP_SPIRE_DOCKER_SOCKET_APPROVAL")
            != "operator-root-equivalent-v1"
        ):
            raise full_local.FullLocalError(
                "FULL_RUNTIME_SPIRE_DOCKER_SOCKET_APPROVAL_REQUIRED"
            )
        spire_renewal.configuration(spire_renewal_input)
    if operation == "plan":
        print(json.dumps(plan(root), indent=2))
        return
    if FIXTURE_VARIABLE in os.environ:
        raise full_local.FullLocalError("FULL_RUNTIME_FIXTURE_ENVIRONMENT_REFUSED")
    if (root / ".env").exists():
        verify_scope()
    manifest = contract(root)
    prefix = full_local.compose_prefix(manifest, root)
    required = set(manifest["root_compose"]["required_services"])
    if operation == "down":
        model = json.loads(
            command([*prefix, "--profile", "*", "config", "--format", "json"], root)
        )
        verify_spire_binding(root, model)
        receipt = spire_binding(root)
        project = receipt["spire-server"]["compose_project"]
        command([*prefix, "stop", *sorted(required - SPIRE_ROLES)], root)
        verify_spire_binding(root, model)
        command(
            [
                "docker",
                "compose",
                "--project-name",
                project,
                "--file",
                str(spire_projection(root)),
                "--profile",
                "*",
                "stop",
                *sorted(SPIRE_ROLES),
            ],
            root,
        )
        return
    if operation == "status":
        model = json.loads(
            command([*prefix, "--profile", "*", "config", "--format", "json"], root)
        )
        verify_spire_binding(root, model)
        pending = pending_services(model, required, root)
        print(
            json.dumps(
                {
                    **plan(root),
                    "status": "PENDING" if pending else "RUNTIME_HEALTHY",
                    "pending_services": pending,
                    "owner_coordinators": {
                        "core": model["name"],
                        "spire": spire_binding(root)["spire-server"]["compose_project"],
                    },
                },
                indent=2,
            )
        )
        if pending:
            raise full_local.FullLocalError("FULL_RUNTIME_NOT_READY")
        return
    if operation == "up":
        if initial_binding_required(root) and (
            spire_initial_input is not None or spire_local_input is not None
        ):
            if (
                os.environ.get("DRP_SPIRE_DOCKER_SOCKET_APPROVAL")
                != "operator-root-equivalent-v1"
            ):
                raise full_local.FullLocalError(
                    "FULL_RUNTIME_SPIRE_DOCKER_SOCKET_APPROVAL_REQUIRED"
                )
            if spire_local_input is not None:
                spire_local.configuration(spire_local_input)
            elif spire_initial_input is not None:
                command(
                    [
                        sys.executable,
                        str(root / "spire/bootstrap.py"),
                        "validate",
                        "--initial-input",
                        str(spire_initial_input.resolve()),
                    ],
                    root,
                )
        else:
            spire_binding(root)
    if operation == "build" or (operation == "up" and not no_build):
        full_local.build_profile(
            root, provider_fixture=False, initialize_core=initialize_core
        )
    else:
        full_local.initialize_profile(
            root, provider_fixture=False, initialize_core=initialize_core
        )
    if operation == "up":
        verify_scope()
        print(
            json.dumps(
                up(
                    root,
                    manifest,
                    spire_initial_input=spire_initial_input,
                    spire_local_input=spire_local_input,
                    spire_renewal_input=spire_renewal_input,
                ),
                indent=2,
            )
        )
