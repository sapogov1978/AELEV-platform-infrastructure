"""Test the actual shared infrastructure, then remove only created resources."""

from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.parse import urlsplit

from resources import (
    Inventory,
    cleanup_plan,
    inventory,
    mounted_volumes,
    owned_containers,
    preflight,
    verify_preserved,
)

ROOT = Path(__file__).resolve().parents[1]
REPORT = Path(__file__).with_name("lifecycle-result.md")
PROFILES = ["--profile", "ui"]


def interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def execute(arguments: list[str], cwd: Path = ROOT) -> str:
    result = subprocess.run(
        arguments, cwd=cwd, capture_output=True, text=True, timeout=1200
    )
    if result.returncode:
        # Compose diagnostics may contain expanded credentials; do not echo them.
        raise RuntimeError(
            "command failed (exit "
            + str(result.returncode)
            + "): "
            + " ".join(arguments)
        )
    return result.stdout


def execute_provision_task(arguments: list[str], task: str, output: Path) -> str:
    result = subprocess.run(
        arguments, cwd=ROOT, capture_output=True, text=True, timeout=1200
    )
    if not result.returncode:
        return result.stdout
    markers = re.findall(
        r"Provisioning failed: [A-Za-z][A-Za-z0-9_]*",
        result.stdout + "\n" + result.stderr,
    )
    payload = {
        "schema_version": 1,
        "task": task,
        "code": "PROVISION_TASK_FAILED",
        "exit_code": result.returncode,
        # Provisioners emit this fixed type-only marker. Never persist their
        # raw stdout/stderr because Compose may expand credential-bearing env.
        "safe_marker": markers[-1] if markers else "NO_SAFE_MARKER",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    raise RuntimeError(
        "command failed (exit " + str(result.returncode) + "): " + " ".join(arguments)
    )


def compose_prefix() -> list[str]:
    return ["docker", "compose", "-f", str(ROOT / "compose.yml")]


def validate_active_networks(model: dict[str, Any], active_services: set[str]) -> None:
    active_networks = {
        network
        for name in active_services
        for network in model["services"][name].get("networks", {})
    }
    networks = model.get("networks", {})
    managed_names = {
        row.get("name", name)
        for name, row in networks.items()
        if not row.get("external")
    }
    if any(
        networks.get(network, {}).get("external")
        and networks[network].get("name", network) not in managed_names
        for network in active_networks
    ):
        raise RuntimeError("clean combined start must not require external networks")


def validate_active_mounts(
    model: dict[str, Any], active_services: set[str], root: Path = ROOT
) -> None:
    for name in active_services:
        row = model["services"][name]
        for mount in row.get("volumes", []):
            if mount["type"] != "bind":
                continue
            source = Path(mount["source"])
            own_socket = (
                row.get("command") == ["fencing-serve"]
                and mount.get("target") == "/run/docker.sock"
            )
            if (
                not source.exists() or not source.is_relative_to(root)
            ) and not own_socket:
                raise RuntimeError("missing or nonportable owner mount")


def model_validation(model: dict[str, Any], active_services: set[str]) -> None:
    services = model["services"]
    if list(services).count("kafka") != 1:
        raise RuntimeError("exactly one canonical Kafka is required")
    for name in ("postgresql-1", "postgresql-2", "postgresql-3"):
        row = services[name]
        mechanism = row["environment"]["AELEV_INFRASTRUCTURE_POSTGRESQL_FENCING_MODE"]
        if mechanism not in {"docker", "kubernetes", "watchdog"}:
            raise RuntimeError("explicit PostgreSQL fencing is required")
        if bool(row.get("devices")) != (mechanism == "watchdog"):
            raise RuntimeError("PostgreSQL device does not match selected fencing")
        if mechanism == "docker" and "postgresql-fencer" not in services:
            raise RuntimeError("Docker fencing executor disappeared")
    if services["translator-topic-provisioner"]["depends_on"]["kafka"] != {
        "condition": "service_healthy",
        "required": True,
    }:
        raise RuntimeError("Kafka readiness barrier disappeared")
    for name in (
        "translator-api",
        "translator-priority-consumer",
        "translator-normal-consumer",
        "translator-outbox",
    ):
        if (
            services[name]["depends_on"]["translator-topic-provisioner"]["condition"]
            != "service_completed_successfully"
        ):
            raise RuntimeError("topic provisioning barrier disappeared")
    if "configuration-applier" in services:
        raise RuntimeError("unbound generic configuration template")
    validate_active_networks(model, active_services)
    validate_active_mounts(model, active_services)
    for row in services.values():
        if row.get("restart") not in {
            "no",
            "on-failure",
            "on-failure:3",
            "unless-stopped",
        }:
            raise RuntimeError("missing canonical restart policy")


def states(project: str) -> dict[str, dict[str, Any]]:
    identities = execute(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=com.docker.compose.project=" + project,
        ]
    ).splitlines()
    result: dict[str, dict[str, Any]] = {}
    for identity in identities:
        name = execute(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "com.docker.compose.service"}}',
                identity,
            ]
        ).strip()
        result[name] = json.loads(
            execute(["docker", "inspect", "--format", "{{json .State}}", identity])
        )
    return result


def bounded_container_logs(identity: str) -> str:
    result = subprocess.run(
        ["docker", "logs", "--tail", "100", identity],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        return "DOCKER_LOGS_UNAVAILABLE"
    return (result.stdout + result.stderr)[-16384:]


def capture_startup_diagnostic(
    project: str, service: str, output: Path, code: str
) -> None:
    identities = execute(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=com.docker.compose.project=" + project,
            "--filter",
            "label=com.docker.compose.service=" + service,
        ]
    ).splitlines()
    if len(identities) != 1:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "service": service,
            "code": code + "_CONTAINER_CARDINALITY",
            "containers": len(identities),
        }
    else:
        identity = identities[0]
        state = json.loads(
            execute(["docker", "inspect", "--format", "{{json .State}}", identity])
        )
        health = state.get("Health", {})
        payload = {
            "schema_version": 1,
            "service": service,
            "code": code,
            "container_id": identity,
            "status": state.get("Status"),
            "exit_code": state.get("ExitCode"),
            "oom_killed": state.get("OOMKilled"),
            "error": str(state.get("Error", ""))[-1024:],
            "health": {
                "status": health.get("Status"),
                "failing_streak": health.get("FailingStreak"),
                "log": [
                    {
                        "exit_code": row.get("ExitCode"),
                        "output": str(row.get("Output", ""))[-1024:],
                    }
                    for row in health.get("Log", [])[-10:]
                ],
            },
            # Selected startup roles do not emit credential values. Keep only a
            # bounded tail; never serialize environment or inspect configuration.
            "startup_tail": bounded_container_logs(identity),
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def capture_kafka_startup_diagnostic(project: str, output: Path) -> None:
    capture_startup_diagnostic(project, "kafka", output, "KAFKA_STARTUP_NOT_READY")


def grafana_startup_markers(raw: str) -> dict[str, Any]:
    """Keep only fixed public markers; never persist native message values."""
    normalized = raw[-16384:].lower()
    signatures = {
        "DATABASE_LOCKED": ("database is locked", "database table is locked"),
        "DATABASE_OPEN_FAILED": (
            "unable to open database",
            "failed to connect to database",
        ),
        "DATABASE_MIGRATION_FAILED": ("migration failed", "failed to run migration"),
        "PERMISSION_DENIED": ("permission denied", "is not writable"),
        "READONLY_FILESYSTEM": ("read-only file system", "readonly filesystem"),
        "PORT_CONFLICT": ("address already in use", "bind: permission denied"),
        "DISK_FULL": ("no space left on device", "database or disk is full"),
        "TLS_LOAD_FAILED": (
            "failed to load certificate",
            "failed to load private key",
            "failed to parse private key",
        ),
        "CONFIGURATION_INVALID": (
            "failed to parse configuration",
            "failed to read config",
            "invalid configuration",
        ),
        "RESOURCE_EXHAUSTED": (
            "cannot allocate memory",
            "resource temporarily unavailable",
            "failed to create new os thread",
        ),
        "RUNTIME_PANIC": ("panic:", "fatal error:"),
        "LOGS_UNAVAILABLE": ("docker_logs_unavailable",),
    }
    return {
        "codes": [
            code
            for code, markers in signatures.items()
            if any(marker in normalized for marker in markers)
        ]
        or ["UNCLASSIFIED_STARTUP_FAILURE"],
        "path_markers": [
            path
            for path in (
                "/var/lib/grafana",
                "/etc/grafana",
                "/usr/share/grafana",
                "/var/log/grafana",
                "/run/secrets",
            )
            if path in normalized
        ],
    }


def capture_grafana_startup_markers(project: str) -> dict[str, Any]:
    try:
        identities = execute(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=com.docker.compose.project=" + project,
                "--filter",
                "label=com.docker.compose.service=grafana",
            ]
        ).splitlines()
        if len(identities) != 1:
            return {
                "codes": ["CONTAINER_CARDINALITY_INVALID"],
                "containers": len(identities),
            }
        return grafana_startup_markers(bounded_container_logs(identities[0]))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return {"codes": ["STARTUP_DIAGNOSTIC_UNAVAILABLE"]}


def capture_group_startup_diagnostic(
    project: str,
    services: dict[str, Any],
    output: Path,
    *,
    stage: str = "unspecified",
    reason_code: str = "COMPOSE_UP_FAILED",
    exit_code: int | None = None,
) -> None:
    current = states(project)
    rows: dict[str, dict[str, Any]] = {}
    for name in sorted(services):
        state = current.get(name)
        if state is None:
            rows[name] = {"status": "not-created"}
            continue
        health = state.get("Health", {})
        rows[name] = {
            "status": state.get("Status"),
            "running": state.get("Running"),
            "exit_code": state.get("ExitCode"),
            "oom_killed": state.get("OOMKilled"),
            "error": str(state.get("Error", ""))[-1024:],
            # Owner healthchecks emit bounded type/path diagnostics. Never
            # serialize environment, inspect configuration or container logs.
            "health": {
                "status": health.get("Status"),
                "failing_streak": health.get("FailingStreak"),
                "log": [
                    {
                        "exit_code": row.get("ExitCode"),
                        "output": str(row.get("Output", ""))[-1024:],
                    }
                    for row in health.get("Log", [])[-3:]
                ],
            },
        }
        if (
            name == "grafana"
            and state.get("Status") in {"dead", "exited"}
            and state.get("ExitCode") != 0
        ):
            rows[name]["startup_markers"] = capture_grafana_startup_markers(project)
    payload = {
        "schema_version": 1,
        "code": "GROUP_STARTUP_FAILED",
        "stage": stage,
        "reason_code": reason_code,
        "exit_code": exit_code,
        "services": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def classify_group_start_failure(output: str) -> str:
    normalized = output.lower()
    classifications = (
        (
            "EXTERNAL_NETWORK_MISSING",
            (
                "declared as external, but could not be found",
                "network declared as external, but could not be found",
            ),
        ),
        (
            "BIND_MOUNT_UNAVAILABLE",
            (
                "bind source path does not exist",
                "invalid mount config",
                "error while creating mount source path",
            ),
        ),
        (
            "IMAGE_UNAVAILABLE",
            ("no such image", "pull access denied", "manifest unknown"),
        ),
        (
            "HOST_PORT_CONFLICT",
            ("port is already allocated", "address already in use"),
        ),
        (
            "CONTAINER_NAME_CONFLICT",
            ("container name", "is already in use by container"),
        ),
        (
            "DEPENDENCY_NOT_READY",
            (
                "dependency failed to start",
                "dependency failed to complete",
                "didn't complete successfully",
                "is unhealthy",
            ),
        ),
        ("RUNTIME_PERMISSION_DENIED", ("permission denied",)),
    )
    for code, markers in classifications:
        if any(marker in normalized for marker in markers):
            return code
    return "COMPOSE_UP_FAILED"


def execute_group_start(
    prefix: list[str],
    project: str,
    services: dict[str, Any],
    output: Path,
    stage: str,
    targets: tuple[str, ...] = (),
) -> str:
    arguments = [
        *prefix,
        *PROFILES,
        "up",
        "-d",
        "--no-build",
        "--pull",
        "never",
        *targets,
    ]
    try:
        result = subprocess.run(
            arguments, cwd=ROOT, capture_output=True, text=True, timeout=1200
        )
    except subprocess.TimeoutExpired:
        reason_code = "COMPOSE_UP_TIMEOUT"
        exit_code = None
    else:
        if not result.returncode:
            return result.stdout
        reason_code = classify_group_start_failure(result.stdout + "\n" + result.stderr)
        exit_code = result.returncode
    try:
        capture_group_startup_diagnostic(
            project,
            services,
            output,
            stage=stage,
            reason_code=reason_code,
            exit_code=exit_code,
        )
    except Exception:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "code": "GROUP_STARTUP_FAILED",
                    "stage": stage,
                    "reason_code": reason_code,
                    "exit_code": exit_code,
                    "diagnostic_code": "GROUP_STATE_CAPTURE_FAILED",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    raise RuntimeError("group start failed: stage=" + stage + "; reason=" + reason_code)


def validate_owner_models(model: dict[str, Any]) -> None:
    for owner in (
        "keycloak",
        "platform-ui",
        "observability",
        "translator-service",
        "postgresql",
        "public-egress-gateway",
    ):
        compose_files = ["-f", "compose.yml"]
        if owner == "keycloak":
            compose_files.extend(["-f", "compose.local-bridge.yml"])
        if owner == "platform-ui":
            compose_files.extend(
                [
                    "-f",
                    "compose.local-keycloak.yml",
                    "-f",
                    "compose.local-bridge.yml",
                ]
            )
        native = json.loads(
            execute(
                [
                    "docker",
                    "compose",
                    *compose_files,
                    "--profile",
                    "*",
                    "config",
                    "--format",
                    "json",
                ],
                cwd=ROOT / owner,
            )
        )
        for name, service in native["services"].items():
            if model["services"].get(name) != service:
                raise RuntimeError(
                    "owner/orchestrated model differs: " + owner + "/" + name
                )


def status_provision_tasks(
    model: dict[str, Any], active_services: set[str]
) -> list[str]:
    return sorted(
        name
        for name in model["services"]
        if name.endswith("-status-provision")
        and name.replace("-status-provision", "-status-publisher") in active_services
    )


def wait_ready(project: str, services: dict[str, Any], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = states(project)
        pending = []
        terminal = []
        for name, description in services.items():
            state = current.get(name, {})
            restart = str(description["restart"])
            health_contract = bool(description.get("healthcheck")) or bool(
                state.get("Health")
            )
            one_shot = (
                restart == "no"
                or restart == "on-failure"
                or restart.startswith("on-failure:")
            )
            if health_contract:
                ready = (
                    state.get("Running") is True
                    and state.get("Health", {}).get("Status") == "healthy"
                )
            elif one_shot:
                ready = state.get("Status") == "exited" and state.get("ExitCode") == 0
            else:
                ready = state.get("Running") is True
            if not ready:
                pending.append(name)
                environment = description.get("environment", {})
                labels = description.get("labels", {})
                fencer = current.get("postgresql-fencer", {})
                native_return = (
                    labels.get("io.aelev.postgresql.node")
                    in {"postgresql-1", "postgresql-2", "postgresql-3"}
                    and bool(labels.get("io.aelev.postgresql.cluster"))
                    and labels.get("io.aelev.postgresql.cluster")
                    == environment.get("AELEV_INFRASTRUCTURE_POSTGRESQL_CLUSTER")
                    and environment.get("AELEV_INFRASTRUCTURE_POSTGRESQL_FENCING_MODE")
                    == "docker"
                    and fencer.get("Running") is True
                    and fencer.get("Health", {}).get("Status") == "healthy"
                    and state.get("Status") == "exited"
                    and state.get("ExitCode") in {0, 137}
                    and state.get("OOMKilled") is False
                )
                if (
                    health_contract
                    and description.get("restart") == "no"
                    and state.get("Status") in {"dead", "exited"}
                    and not native_return
                ):
                    terminal.append(name)
        if terminal:
            raise RuntimeError(
                "service exited before readiness: " + ", ".join(sorted(terminal))
            )
        if not pending:
            return
        time.sleep(3)
    raise RuntimeError("readiness deadline: " + ", ".join(sorted(pending)))


def ui_status_diagnostic(
    ids: set[str], api_status: int, rows: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "code": "PLATFORM_UI_EXPECTED_MODULES_NOT_ONLINE",
        "api_status": api_status,
        "expected_module_ids": sorted(ids),
        "missing_module_ids": sorted(ids - set(rows)),
        "modules": [
            {
                "module_id": identity,
                "state": row.get("status", {}).get("state", "MISSING"),
                "reported_state": row.get("status", {}).get(
                    "reported_state", "MISSING"
                ),
                "connection": row.get("status", {}).get("connection", "MISSING"),
                "components": [
                    {
                        "id": part.get("id", "MISSING"),
                        "state": part.get("state", "MISSING"),
                    }
                    for part in row.get("status", {}).get("components", [])
                ],
            }
            for identity, row in sorted(rows.items())
        ],
    }


def write_ui_status_diagnostic(
    path: Path, ids: set[str], api_status: int, rows: dict[str, Any]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            ui_status_diagnostic(ids, api_status, rows), indent=2, sort_keys=True
        )
        + "\n"
    )


def ui_snapshot(args: argparse.Namespace, ids: set[str]) -> dict[str, Any]:
    # Model/resource regressions do not execute a browser. Load its platform-
    # specific dependency only for the actual operator UI exercise.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch(
            executable_path=str(args.chromium), headless=True
        )
        try:
            page = browser.new_page(
                ignore_https_errors=args.allow_local_certificate,
                viewport={"width": 1440, "height": 1100},
            )
            page.goto(args.ui_url)
            page.locator("#username").fill(args.viewer_user)
            password = args.viewer_password_file.read_text().strip()
            try:
                page.locator("#password").fill(password)
            except Exception:
                raise RuntimeError("password entry failed") from None
            del password
            with page.expect_response(
                lambda response: response.url == args.ui_url + "/api/modules"
            ) as initial:
                page.locator("#kc-login").click()
            if initial.value.status != 200:
                raise RuntimeError("real OIDC viewer request failed")
            authorization = initial.value.request.all_headers()["authorization"]
            deadline = time.monotonic() + args.timeout
            last_status = 0
            rows: dict[str, Any] = {}
            while time.monotonic() < deadline:
                response = page.request.get(
                    args.ui_url + "/api/modules",
                    headers={"Authorization": authorization},
                )
                last_status = response.status
                if response.status == 401:
                    with page.expect_response(
                        lambda reply: reply.url == args.ui_url + "/api/modules"
                    ) as refreshed:
                        page.reload()
                    if refreshed.value.status != 200:
                        raise RuntimeError("viewer token refresh failed")
                    authorization = refreshed.value.request.all_headers()[
                        "authorization"
                    ]
                    continue
                if response.status == 200:
                    rows = {
                        row["module_id"]: row
                        for row in response.json()["modules"]
                        if row["module_id"] in ids
                    }
                    if set(rows) == ids and all(
                        row["service_kind"] == "INFRASTRUCTURE"
                        and not row["sections"]
                        and row["status"]["state"] == "ONLINE"
                        and row["components"]
                        and all(
                            part["state"] == "ONLINE"
                            for part in row["status"]["components"]
                        )
                        for row in rows.values()
                    ):
                        with page.expect_response(
                            lambda reply: reply.url == args.ui_url + "/api/modules"
                        ) as refreshed:
                            page.reload()
                        if refreshed.value.status != 200:
                            raise RuntimeError("authenticated UI reload failed")
                        for identity, row in rows.items():
                            group = page.locator(
                                'tbody[data-module-id="' + identity + '"]'
                            )
                            group.wait_for(state="visible")
                            toggle = group.locator("button[aria-expanded]")
                            if toggle.get_attribute("aria-expanded") != "true":
                                toggle.click()
                            if group.locator(".status-component").count() != len(
                                row["components"]
                            ):
                                raise RuntimeError("API/DOM composition differs")
                        page.screenshot(
                            path=str(args.report.with_suffix(".png")), full_page=True
                        )
                        return rows
                time.sleep(3)
            diagnostic_path = args.report.with_name("ui-status-diagnostic.json")
            write_ui_status_diagnostic(diagnostic_path, ids, last_status, rows)
            raise RuntimeError("injected state did not become ONLINE")
        finally:
            browser.close()


def assert_sequence_advanced(previous: dict[str, Any], current: dict[str, Any]) -> None:
    if previous.keys() != current.keys():
        raise RuntimeError("publisher identities changed")
    for identity, row in previous.items():
        if current[identity]["status"]["sequence"] <= row["status"]["sequence"]:
            raise RuntimeError("publisher sequence did not advance")
        if current[identity]["components"] != row["components"]:
            raise RuntimeError("owner composition changed")


def cleanup(
    before: Inventory, project: str, witnessed: set[str], compose: list[str]
) -> dict[str, int]:
    current = inventory(execute)
    containers = owned_containers(current, project)
    witnessed.update(mounted_volumes(execute, containers))
    try:
        execute([*compose, *PROFILES, "down"])
    except RuntimeError:
        # Still remove only resources proved to be created by this run.
        pass
    plan = cleanup_plan(before, inventory(execute), project, witnessed)
    # Every remaining resource is individually owned and absent from the baseline.
    if plan["containers"]:
        execute(["docker", "container", "rm", "-f", *plan["containers"]])
    if plan["networks"]:
        execute(["docker", "network", "rm", *plan["networks"]])
    if plan["volumes"]:
        execute(["docker", "volume", "rm", *plan["volumes"]])
    after = inventory(execute)
    verify_preserved(before, after, project)
    for name in witnessed - before["volumes"].keys():
        if name in after["volumes"]:
            raise RuntimeError("created mounted volume remains")
    return {kind: len(before[kind]) for kind in before}


def run(args: argparse.Namespace) -> int:
    args.report = getattr(args, "report", REPORT)
    started = datetime.now(timezone.utc)
    checks: list[tuple[str, str]] = []
    before: Inventory | None = None
    prefix = compose_prefix()
    project = ""
    witnessed: set[str] = set()
    rows: dict[str, Any] = {}
    protected: dict[str, int] = {}
    failure = ""
    admitted = False

    def passed(name: str, detail: str = "") -> None:
        checks.append((name, detail))
        print("PASS: " + name, flush=True)

    try:
        regular_names = set(
            execute([*prefix, *PROFILES, "config", "--services"]).splitlines()
        )
        normal = json.loads(
            execute([*compose_prefix(), "--profile", "*", "config", "--format", "json"])
        )
        model_validation(normal, regular_names)
        passed(
            "Каноническая композиция, один Kafka и явно выбранное ограждение PostgreSQL"
        )
        validate_owner_models(normal)
        passed("Самостоятельные и оркестрируемые роли владельцев полностью совпадают")
        model = json.loads(
            execute([*prefix, "--profile", "*", "config", "--format", "json"])
        )
        project = model["name"]
        regular = {
            name: row
            for name, row in model["services"].items()
            if name in regular_names
        }
        ids = {
            str(value)
            for name, row in regular.items()
            if name.endswith("-status-publisher")
            for key, value in row.get("environment", {}).items()
            if key.endswith("_UI_MODULE_ID")
        }
        if not ids:
            raise RuntimeError("no owner publisher identities in model")
        before = inventory(execute)
        preflight(
            execute,
            before,
            model,
            args.reuse_existing_data,
            regular_names,
        )
        admitted = True
        passed("Исходные ресурсы учтены; существующих писателей общих томов нет")
        execute([*prefix, "up", "-d", "--no-build", "--pull", "never", "kafka"])
        try:
            # The image health contract exhausts its own 20 x 10-second retry
            # window after a 20-second start period. A five-minute outer bound
            # retains scheduling margin without hiding a deterministic failure.
            wait_ready(project, {"kafka": regular["kafka"]}, min(args.timeout, 300))
        except RuntimeError:
            capture_kafka_startup_diagnostic(
                project, args.report.with_name("kafka-startup-diagnostic.json")
            )
            raise
        if any(name.startswith("translator-") for name in states(project)):
            raise RuntimeError("Kafka startup unexpectedly created translator roles")
        passed("Kafka самостоятельно healthy: ни одна роль переводчика не запущена")
        for task in sorted(
            name
            for name in model["services"]
            if name.endswith("-configuration-init")
            and name != "platform-ui-configuration-init"
        ):
            execute([*prefix, "--profile", "provision", "run", "--rm", task])
            passed("Настоящая инициализация собственного тома: " + task + " — exit 0")
        execute([*prefix, "up", "-d", "--no-build", "--pull", "never", "keycloak"])
        try:
            wait_ready(
                project,
                {name: regular[name] for name in ("keycloak-database", "keycloak")},
                args.timeout,
            )
        except RuntimeError:
            capture_startup_diagnostic(
                project,
                "keycloak",
                args.report.with_name("keycloak-startup-diagnostic.json"),
                "KEYCLOAK_STARTUP_NOT_READY",
            )
            raise
        passed("Настоящие Keycloak, его БД, TLS и импорт собственного realm")
        for task in [
            "platform-ui-configuration-init",
            "grafana-oidc-provision",
            "blackbox-exporter-operator-provision",
            "prometheus-operator-provision",
            "translator-api-provision",
        ] + status_provision_tasks(model, regular_names):
            execute_provision_task(
                [*prefix, "--profile", "provision", "run", "--rm", task],
                task,
                args.report.with_name("provision-task-diagnostic.json"),
            )
            passed("Реальная задача владельца: " + task + " — exit 0")
        execute(
            [
                *prefix,
                "--profile",
                "provision",
                "run",
                "--rm",
                "site-replication-applier",
            ]
        )
        passed("Реальная проверка/настройка MinIO site replication: exit 0")
        try:
            execute_group_start(
                prefix,
                project,
                regular,
                args.report.with_name("group-startup-diagnostic.json"),
                "initial",
            )
        except RuntimeError:
            capture_startup_diagnostic(
                project,
                "postgresql-fencer",
                args.report.with_name("postgresql-fencer-startup-diagnostic.json"),
                "POSTGRESQL_FENCER_STARTUP_FAILED",
            )
            raise
        try:
            wait_ready(project, regular, args.timeout)
        except RuntimeError:
            capture_group_startup_diagnostic(
                project,
                regular,
                args.report.with_name("group-readiness-diagnostic.json"),
                stage="initial-readiness",
                reason_code="READINESS_FAILED",
            )
            raise
        passed(
            "Общий старт: healthcheck, автоматические миграции и topics готовы",
            str(len(regular))
            + " ролей; "
            + str(sum(bool(row.get("healthcheck")) for row in regular.values()))
            + " healthcheck; остальные постоянные роли RUNNING и далее проверяются владельцами через UI",
        )
        rows = ui_snapshot(args, ids)
        passed(
            "Настоящие OIDC, API и DOM: все собственные строки ONLINE",
            str(len(rows)) + " строк",
        )
        first_ids = set(owned_containers(inventory(execute), project))
        execute([*prefix, *PROFILES, "stop"])
        if any(row.get("Running") for row in states(project).values()):
            raise RuntimeError("a project container is still running after stop")
        passed("Общая остановка: ни один контейнер группы не работает")
        execute_group_start(
            prefix,
            project,
            regular,
            args.report.with_name("group-startup-diagnostic.json"),
            "stop-up",
        )
        wait_ready(project, regular, args.timeout)
        resumed = ui_snapshot(args, ids)
        assert_sequence_advanced(rows, resumed)
        if set(owned_containers(inventory(execute), project)) != first_ids:
            raise RuntimeError("stop/up unexpectedly replaced containers")
        rows = resumed
        passed(
            "Повторный старт: прежние контейнеры, identities и возрастающие последовательности"
        )
        current = inventory(execute)
        witnessed.update(mounted_volumes(execute, owned_containers(current, project)))
        execute([*prefix, *PROFILES, "down"])
        if owned_containers(inventory(execute), project):
            raise RuntimeError("project containers remain after down")
        data_names = {row["name"] for row in model["volumes"].values()}
        if (
            data_names.intersection(current["volumes"])
            - inventory(execute)["volumes"].keys()
        ):
            raise RuntimeError("persistent data missing")
        passed("Общее down сняло контейнеры; постоянные тома сохранены")
        identity = {name: regular[name] for name in ("keycloak-database", "keycloak")}
        execute_group_start(
            prefix,
            project,
            identity,
            args.report.with_name("group-startup-diagnostic.json"),
            "down-up-identity",
            ("keycloak",),
        )
        wait_ready(project, identity, args.timeout)
        execute_group_start(
            prefix,
            project,
            regular,
            args.report.with_name("group-startup-diagnostic.json"),
            "down-up",
        )
        wait_ready(project, regular, args.timeout)
        recreated = ui_snapshot(args, ids)
        assert_sequence_advanced(rows, recreated)
        if first_ids & set(owned_containers(inventory(execute), project)):
            raise RuntimeError("down/up did not recreate all containers")
        rows = recreated
        passed("Down/up: новые контейнеры работают с прежними данными и identities")
    except (Exception, KeyboardInterrupt) as error:
        failure = type(error).__name__
        if isinstance(error, RuntimeError):
            failure += ": " + str(error)
        print("FAIL: " + failure, flush=True)
    finally:
        if admitted and before is not None:
            try:
                protected = cleanup(before, project, witnessed, prefix)
                passed(
                    "Очистка: созданное снято, все исходные ресурсы и чужие состояния сохранены"
                )
            except Exception as error:
                failure += "; cleanup: " + type(error).__name__ + ": " + str(error)
                print("FAIL: cleanup", flush=True)
        result = "FAIL" if failure else "PASS"
        report = "# Проверка общей инфраструктурной оркестрации\n\n"
        report += f"Начало: {started.isoformat()}. Итог: {result}.\n\n"
        report += "Использованы реальные поставки и собственные env, удостоверения и тома. Поддельных Kafka, IdP, API или издателей нет. Данные существовавших до запуска томов не удалялись; технические отчёты и реальные задачи владельцев обновлялись штатно.\n\n"
        report += f"Механизм ограждения берётся из собственного env PostgreSQL, тест его не отключает. Повторное использование существующих данных: {args.reuse_existing_data}. WAN, аппаратное ограждение, продукционная PKI, нагрузка и независимое резервное восстановление не проверялись.\n\n"
        report += "| Проверка | Результат | Подробность |\n|---|---|---|\n"
        for name, detail in checks:
            report += f"| {name} | PASS | {detail} |\n"
        if failure:
            report += f"\nОтказ: {failure}. Невыполненные последующие этапы не считаются успешными.\n"
        if rows:
            report += "\n| Название владельца | Компоненты | Последовательность |\n|---|---|---|\n"
            for row in rows.values():
                title = row["title"]["ru"].replace("|", "\\|")
                report += f"| {title} | {len(row['components'])} ONLINE | {row['status']['sequence']} |\n"
            report += (
                "\nСнимок страницы состояния: `"
                + args.report.with_suffix(".png").name
                + "`.\n"
            )
        if protected:
            report += (
                "\nСверены сохранённые исходные ресурсы: "
                + ", ".join(f"{kind}: {count}" for kind, count in protected.items())
                + ". Созданных контейнеров, сетей и томов теста не осталось; рабочие env, секреты и образы сохранены.\n"
            )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report)
    return int(bool(failure))


def main() -> int:
    signal.signal(signal.SIGTERM, interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui-url", required=True)
    parser.add_argument("--viewer-user", required=True)
    parser.add_argument("--viewer-password-file", type=Path, required=True)
    parser.add_argument("--chromium", type=Path, required=True)
    parser.add_argument(
        "--report",
        type=Path,
        default=REPORT,
    )
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--reuse-existing-data", action="store_true")
    parser.add_argument("--allow-local-certificate", action="store_true")
    args = parser.parse_args()
    if urlsplit(args.ui_url).scheme != "https" or args.timeout <= 0:
        parser.error("HTTPS UI and a positive timeout required")
    if args.allow_local_certificate and urlsplit(args.ui_url).hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        parser.error("certificate exception is limited to loopback")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
