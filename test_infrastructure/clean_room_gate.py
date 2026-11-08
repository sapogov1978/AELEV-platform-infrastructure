#!/usr/bin/env python3
"""Run the real full-local lifecycle and emit independently verifiable evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


class GateError(RuntimeError):
    """A stable failure code that never contains secret values."""


@dataclass(frozen=True)
class Stage:
    identity: str
    functions: tuple[str, ...]
    command: tuple[str, ...]
    artifacts: tuple[Path, ...]
    cwd: Path = ROOT
    environment: tuple[tuple[str, str], ...] = ()
    timeout: int = 3600


FUNCTION_STAGES: dict[str, tuple[str, ...]] = {
    "root-lifecycle": ("identity-login", "platform-ui-owner-status"),
    "kafka-lifecycle": ("kafka-round-trip",),
    "postgresql-lifecycle": ("postgresql-transaction",),
    "minio-lifecycle": ("object-storage-round-trip",),
    "egress-lifecycle": ("public-egress-default-deny-and-exact-allow",),
    "telemetry-onboarding": ("telemetry-delivery-query",),
    "translator-lifecycle": ("translator-queue-preserves-work-without-provider",),
    "spire-lifecycle": ("workload-identity-issue-rotate-revoke",),
    "spire-observation": ("workload-identity-observation-delivery-recovery",),
    "recovery-lifecycle": ("bounded-recovery-preserves-state",),
}


def command(
    arguments: list[str] | tuple[str, ...],
    *,
    cwd: Path = ROOT,
    environment: dict[str, str] | None = None,
    timeout: int = 3600,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def docker_inventory() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for kind in ("container", "network", "volume", "image"):
        listing = command(
            ["docker", kind, "ls", "-aq"]
            if kind in {"container", "image"}
            else ["docker", kind, "ls", "-q"]
        )
        if listing.returncode:
            raise GateError(f"DOCKER_INVENTORY_FAILED:{kind}")
        result[kind] = sorted(listing.stdout.split())
    states: dict[str, str] = {}
    for identity in result["container"]:
        inspected = command(
            ["docker", "inspect", "--format", "{{.State.Status}}", identity]
        )
        if inspected.returncode:
            raise GateError("DOCKER_CONTAINER_INSPECT_FAILED")
        states[identity] = inspected.stdout.strip()
    result["container_states"] = states
    return result


def exact_image(reference: str) -> str:
    inspected = command(
        ["docker", "image", "inspect", "--format", "{{.Id}}", reference]
    )
    if inspected.returncode or not inspected.stdout.strip().startswith("sha256:"):
        raise GateError("GATE_IMAGE_NOT_AVAILABLE")
    return inspected.stdout.strip()


def rendered_root_model() -> dict[str, Any]:
    result = command(
        [
            sys.executable,
            "-c",
            (
                "import json; from tools.full_local import load_json,rendered_model; "
                "m=load_json(__import__('pathlib').Path('full-local.json')); "
                "print(json.dumps(rendered_model('root',m)))"
            ),
        ]
    )
    if result.returncode:
        raise GateError("ROOT_COMPOSE_MODEL_UNAVAILABLE")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise GateError("ROOT_COMPOSE_MODEL_INVALID")
    return value


def stage_plan(arguments: argparse.Namespace, output: Path) -> list[Stage]:
    model = rendered_root_model()
    services = model["services"]
    manifest = json.loads((ROOT / "full-local.json").read_text(encoding="utf-8"))
    sender_reference = next(
        item["admitted_reference"]
        for item in manifest["build_inputs"]
        if item["id"] == "observability-sender-image"
    )
    sender = exact_image(sender_reference)
    spire_sender = exact_image(services["spire-telemetry-sender"]["image"])
    grafana = exact_image(services["grafana"]["image"])
    recovery_image = exact_image(services["translator-api"]["image"])
    spire_image = exact_image("aelev/infrastructure-spire:local")
    stages = output / "stages"
    root_report = stages / "root-lifecycle.md"
    pg_report = stages / "postgresql-lifecycle.md"
    egress_report = stages / "egress-lifecycle.md"
    telemetry_report = stages / "telemetry-onboarding.md"
    translator_report = stages / "translator-lifecycle.json"
    recovery_output = stages / "recovery"
    root_command = [
        "poetry",
        "--directory",
        "test_infrastructure",
        "run",
        "python",
        "lifecycle.py",
        "--ui-url",
        arguments.ui_url,
        "--viewer-user",
        arguments.viewer_user,
        "--viewer-password-file",
        str(arguments.viewer_password_file),
        "--chromium",
        str(arguments.chromium),
        "--report",
        str(root_report),
    ]
    if arguments.allow_local_certificate:
        root_command.append("--allow-local-certificate")
    return [
        Stage(
            "root-lifecycle",
            FUNCTION_STAGES["root-lifecycle"],
            tuple(root_command),
            (root_report, root_report.with_suffix(".png")),
            timeout=2400,
        ),
        Stage(
            "kafka-lifecycle",
            FUNCTION_STAGES["kafka-lifecycle"],
            ("bash", "kafka/test/single-node-lifecycle.test.sh"),
            (),
            timeout=1200,
        ),
        Stage(
            "postgresql-lifecycle",
            FUNCTION_STAGES["postgresql-lifecycle"],
            (
                "poetry",
                "--directory",
                "postgresql",
                "run",
                "python",
                "test/cluster-lifecycle.py",
                "--report",
                str(pg_report),
            ),
            (pg_report,),
            timeout=3600,
        ),
        Stage(
            "minio-lifecycle",
            FUNCTION_STAGES["minio-lifecycle"],
            ("bash", "minio/test/multi-site-lifecycle.test.sh"),
            (),
            timeout=5400,
        ),
        Stage(
            "egress-lifecycle",
            FUNCTION_STAGES["egress-lifecycle"],
            (
                "poetry",
                "--directory",
                "public-egress-gateway",
                "run",
                "python",
                "test/lifecycle.py",
                "--report",
                str(egress_report),
            ),
            (egress_report,),
            timeout=2400,
        ),
        Stage(
            "telemetry-onboarding",
            FUNCTION_STAGES["telemetry-onboarding"],
            (
                "poetry",
                "--directory",
                "test_infrastructure",
                "run",
                "python",
                str(ROOT / "observability/grafana/test/onboarding.py"),
                "--sender-image",
                sender,
                "--chromium",
                str(arguments.chromium),
                "--report",
                str(telemetry_report),
            ),
            (telemetry_report, telemetry_report.with_suffix(".png")),
            timeout=2400,
        ),
        Stage(
            "translator-lifecycle",
            FUNCTION_STAGES["translator-lifecycle"],
            (
                "poetry",
                "--directory",
                "translator-service",
                "run",
                "python",
                "test/lifecycle.py",
                "--report",
                str(translator_report),
            ),
            (translator_report, translator_report.with_suffix(".md")),
            timeout=3600,
        ),
        Stage(
            "spire-lifecycle",
            FUNCTION_STAGES["spire-lifecycle"],
            (
                "poetry",
                "--directory",
                "spire",
                "run",
                "pytest",
                "-q",
                "test/test_real_spire.py::test_real_issue_rotation_revoke_and_node_restart",
            ),
            (),
            environment=(("AELEV_INFRASTRUCTURE_SPIRE_TEST_IMAGE", spire_image),),
            timeout=2400,
        ),
        Stage(
            "spire-observation",
            FUNCTION_STAGES["spire-observation"],
            (
                "poetry",
                "--directory",
                "spire",
                "run",
                "pytest",
                "-q",
                "test/test_real_observation.py::test_actual_collection_delivery_outage_and_recovery",
                "test/test_real_grafana.py::test_native_grafana_scoped_dashboard_rule_and_repeat",
            ),
            (),
            environment=(
                ("AELEV_INFRASTRUCTURE_SPIRE_TEST_IMAGE", spire_image),
                ("AELEV_INFRASTRUCTURE_SPIRE_SENDER_TEST_IMAGE", spire_sender),
                ("AELEV_INFRASTRUCTURE_SPIRE_GRAFANA_TEST_IMAGE", grafana),
            ),
            timeout=3600,
        ),
        Stage(
            "recovery-lifecycle",
            FUNCTION_STAGES["recovery-lifecycle"],
            (
                "poetry",
                "--directory",
                "test_infrastructure",
                "run",
                "python",
                "automatic_recovery_sandbox.py",
                "--image",
                recovery_image,
                "--output",
                str(recovery_output),
            ),
            (recovery_output / "result.json",),
            timeout=1200,
        ),
    ]


def run_logged(
    identity: str,
    arguments: tuple[str, ...] | list[str],
    output: Path,
    *,
    cwd: Path = ROOT,
    environment: dict[str, str] | None = None,
    timeout: int = 3600,
) -> Path:
    log = output / "logs" / f"{identity}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    result = command(arguments, cwd=cwd, environment=environment, timeout=timeout)
    log.write_text(result.stdout, encoding="utf-8")
    if result.returncode:
        raise GateError(f"STAGE_FAILED:{identity}")
    return log


def prepare_drivers(output: Path) -> None:
    if shutil.which("poetry") is None:
        raise GateError("TOOL_UNAVAILABLE:poetry")
    version = command(["poetry", "--version"])
    if version.returncode or "2.2.1" not in version.stdout:
        raise GateError("POETRY_VERSION_NOT_ADMITTED")
    for relative in (
        "test_infrastructure",
        "postgresql",
        "public-egress-gateway",
        "observability/grafana",
        "translator-service",
        "spire",
    ):
        identity = "driver-" + relative.replace("/", "-")
        run_logged(
            identity,
            [
                "poetry",
                "--directory",
                relative,
                "install",
                "--no-interaction",
                "--no-ansi",
            ],
            output,
            timeout=1200,
        )


def ensure_external_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise GateError("EVIDENCE_OUTPUT_MUST_BE_OUTSIDE_SOURCE")
    if resolved.exists():
        if not resolved.is_dir() or any(resolved.iterdir()):
            raise GateError("EVIDENCE_OUTPUT_NOT_EMPTY")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def run_diagnostics(arguments: argparse.Namespace, output: Path, bom_path: Path) -> int:
    """Exercise selected stages without manufacturing full acceptance evidence."""
    selected = set(arguments.diagnostic_stage)
    baseline = docker_inventory()
    results: list[dict[str, Any]] = []
    for repetition in range(1, arguments.diagnostic_repetitions + 1):
        attempt = output / f"attempt-{repetition}"
        for stage in stage_plan(arguments, attempt):
            if stage.identity not in selected:
                continue
            started = datetime.now(UTC)
            print(
                json.dumps(
                    {
                        "stage": stage.identity,
                        "attempt": repetition,
                        "status": "STARTED",
                    }
                ),
                flush=True,
            )
            environment = dict(os.environ)
            environment.update(dict(stage.environment))
            code: str | None = None
            try:
                stage_command = stage.command
                if stage.identity == "postgresql-lifecycle" and getattr(
                    arguments, "diagnostic_bootstrap_member", None
                ):
                    stage_command += (
                        "--bootstrap-member",
                        str(arguments.diagnostic_bootstrap_member),
                    )
                if (
                    stage.identity == "postgresql-lifecycle"
                    and arguments.diagnostic_repetitions == 3
                    and not getattr(arguments, "diagnostic_natural_primary", False)
                ):
                    stage_command += ("--fault-target", str(repetition))
                run_logged(
                    stage.identity,
                    stage_command,
                    attempt,
                    cwd=stage.cwd,
                    environment=environment,
                    timeout=stage.timeout,
                )
                if any(
                    not path.is_file() or not path.stat().st_size
                    for path in stage.artifacts
                ):
                    raise GateError(f"STAGE_ARTIFACT_MISSING:{stage.identity}")
            except (GateError, subprocess.TimeoutExpired) as error:
                code = (
                    str(error)
                    if isinstance(error, GateError)
                    else f"STAGE_TIMEOUT:{stage.identity}"
                )
            cleanup_preserved = docker_inventory() == baseline
            source_clean = not command(["git", "status", "--porcelain"]).stdout
            if not cleanup_preserved:
                code = "DOCKER_BASELINE_NOT_PRESERVED"
            if not source_clean:
                code = "SOURCE_TREE_CHANGED_BY_GATE"
            row = {
                "stage": stage.identity,
                "attempt": repetition,
                "status": "FAIL" if code else "PASS",
                "blocking_code": code,
                "cleanup_preserved": cleanup_preserved,
                "source_clean": source_clean,
                "started_at": started.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
            }
            results.append(row)
            write_json(
                output / "diagnostic-run.json",
                {
                    "schema_version": 1,
                    "profile": "full-local-diagnostic",
                    "source_commit": json.loads(bom_path.read_text())["source_commit"],
                    "bom_sha256": sha256(bom_path),
                    "results": results,
                },
            )
            print(json.dumps(row), flush=True)
            if not cleanup_preserved or not source_clean:
                return 2
    return 2 if any(row["status"] != "PASS" for row in results) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ui-url", default="https://127.0.0.1:18082")
    parser.add_argument("--viewer-user", required=True)
    parser.add_argument("--viewer-password-file", type=Path, required=True)
    parser.add_argument("--chromium", type=Path, required=True)
    parser.add_argument("--allow-local-certificate", action="store_true")
    parser.add_argument("--list-stages", action="store_true")
    parser.add_argument(
        "--diagnostic-stage", action="append", choices=sorted(FUNCTION_STAGES)
    )
    parser.add_argument(
        "--diagnostic-repetitions", type=int, choices=range(1, 4), default=1
    )
    parser.add_argument("--diagnostic-natural-primary", action="store_true")
    parser.add_argument("--diagnostic-bootstrap-member", type=int, choices=(1, 2, 3))
    arguments = parser.parse_args()
    if arguments.diagnostic_repetitions != 1 and not arguments.diagnostic_stage:
        parser.error("--diagnostic-repetitions requires --diagnostic-stage")
    if arguments.list_stages:
        print(json.dumps(FUNCTION_STAGES, indent=2, sort_keys=True))
        return 0
    started = datetime.now(UTC)
    output: Path | None = None
    try:
        output = ensure_external_output(arguments.output)
        status = command(["git", "status", "--porcelain"])
        if status.returncode or status.stdout:
            raise GateError("SOURCE_TREE_NOT_CLEAN")
        if not arguments.viewer_password_file.is_file():
            raise GateError("VIEWER_PASSWORD_FILE_MISSING")
        if not arguments.chromium.is_file():
            raise GateError("CHROMIUM_EXECUTABLE_MISSING")
        prepare_drivers(output)
        run_logged(
            "full-local-build",
            [sys.executable, "tools/full_local.py", "build"],
            output,
            timeout=7200,
        )
        run_logged(
            "full-local-preflight",
            [sys.executable, "tools/full_local.py", "preflight", "--require-images"],
            output,
        )
        bom_path = output / "release-bom.json"
        run_logged(
            "release-bom",
            [sys.executable, "tools/full_local.py", "bom", "--output", str(bom_path)],
            output,
        )
        if arguments.diagnostic_stage:
            return run_diagnostics(arguments, output, bom_path)
        baseline = docker_inventory()
        function_artifacts: dict[str, list[dict[str, str]]] = {}
        for stage in stage_plan(arguments, output):
            environment = dict(os.environ)
            environment.update(dict(stage.environment))
            log = run_logged(
                stage.identity,
                stage.command,
                output,
                cwd=stage.cwd,
                environment=environment,
                timeout=stage.timeout,
            )
            artifacts = list(stage.artifacts) or [log]
            if any(not path.is_file() or not path.stat().st_size for path in artifacts):
                raise GateError(f"STAGE_ARTIFACT_MISSING:{stage.identity}")
            rows = [
                {"path": str(path.relative_to(output)), "sha256": sha256(path)}
                for path in artifacts
            ]
            for function in stage.functions:
                function_artifacts[function] = rows
        final = docker_inventory()
        cleanup_preserved = final == baseline
        if not cleanup_preserved:
            raise GateError("DOCKER_BASELINE_NOT_PRESERVED")
        if command(["git", "status", "--porcelain"]).stdout:
            raise GateError("SOURCE_TREE_CHANGED_BY_GATE")
        bom = json.loads(bom_path.read_text(encoding="utf-8"))
        manifest = json.loads((ROOT / "full-local.json").read_text(encoding="utf-8"))
        required = manifest["acceptance"]["required_functions"]
        if sorted(function_artifacts) != sorted(required):
            raise GateError("FUNCTION_STAGE_SET_MISMATCH")
        evidence = {
            "schema_version": 1,
            "profile": "full-local",
            "release": manifest["release"],
            "source_commit": bom["source_commit"],
            "bom_sha256": sha256(bom_path),
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "clean_baseline": True,
            "cleanup_preserved": True,
            "functions": [
                {
                    "id": identity,
                    "status": "PASS",
                    "evidence": function_artifacts[identity],
                }
                for identity in required
            ],
            "result": "PASS",
        }
        evidence_path = output / "clean-room-evidence.json"
        write_json(evidence_path, evidence)
        verification = command(
            [
                sys.executable,
                "test_infrastructure/clean_room.py",
                "--evidence",
                str(evidence_path),
                "--bom",
                str(bom_path),
            ]
        )
        (output / "verification.json").write_text(verification.stdout, encoding="utf-8")
        if verification.returncode:
            raise GateError("EVIDENCE_VERIFICATION_FAILED")
        print(json.dumps({"status": "PASS", "evidence": str(evidence_path)}, indent=2))
        return 0
    except (
        GateError,
        OSError,
        KeyError,
        ValueError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ) as error:
        code = str(error) if isinstance(error, GateError) else type(error).__name__
        payload = {
            "schema_version": 1,
            "profile": "full-local",
            "status": "BLOCKED",
            "blocking_code": code,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
        }
        if output is not None:
            write_json(output / "blocked-run.json", payload)
        print(json.dumps(payload, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
