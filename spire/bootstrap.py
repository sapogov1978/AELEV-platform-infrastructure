#!/usr/bin/env python3
"""Install this owner from its private initial bundle using public entrypoints."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
APPROVAL = "DRP_SPIRE_DOCKER_SOCKET_APPROVAL"
VALUE = "operator-root-equivalent-v1"
ROLE_INPUTS: dict[str, list[str]] = json.loads(
    (ROOT / "src/aelev_spire/observation-inputs.json").read_text()
)


class BootstrapError(RuntimeError):
    """Safe owner code; captured native diagnostics remain private."""


def private_text(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(value)


def validate_inputs(source: Path) -> None:
    """Check delivery boundaries; the native installer validates plans and PKI."""
    try:
        paths = [source, *source.rglob("*")]
        if len(paths) > 128 or not source.is_dir():
            raise ValueError
        for path in paths:
            mode = path.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError
            if os.name != "nt" and mode & 0o077:
                raise ValueError
            if stat.S_ISREG(mode) and path.stat().st_size > 65536:
                raise ValueError
        for name in ("plan.json", "observation.json"):
            value = json.loads((source / name).read_text())
            if (
                value.get("owner") != "infrastructure-spire"
                or value.get("schema_version") != 1
            ):
                raise ValueError
        plan = json.loads((source / "plan.json").read_text())
        if plan.get("environment") not in {"local", "test", "production"}:
            raise ValueError
        for name, files in ROLE_INPUTS.items():
            directory = source / name
            if not directory.is_dir() or any(
                not (directory / file).is_file() for file in files
            ):
                raise ValueError
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise BootstrapError("SPIRE_BOOTSTRAP_INITIAL_BUNDLE_INVALID") from error


def invoke(
    arguments: list[str],
    environment: dict[str, str],
    timeout: int,
    diagnostic: Path | None = None,
) -> str:
    try:
        result = subprocess.run(
            arguments,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        if diagnostic is not None:
            private_text(
                diagnostic.with_suffix(".unknown"), "SPIRE_BOOTSTRAP_RESULT_UNKNOWN\n"
            )
        raise BootstrapError("SPIRE_BOOTSTRAP_RESULT_UNKNOWN") from error
    if diagnostic is not None:
        private_text(diagnostic.with_suffix(".stdout"), result.stdout)
        private_text(diagnostic.with_suffix(".stderr"), result.stderr)
    if result.returncode:
        raise BootstrapError("SPIRE_BOOTSTRAP_NATIVE_OPERATION_FAILED")
    return result.stdout


def entrypoint(name: str, system: str) -> list[str]:
    if system in {"Darwin", "Linux"}:
        return ["sh", str(ROOT / (name + ".sh"))]
    if system == "Windows":
        return [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / (name + ".ps1")),
        ]
    raise BootstrapError("SPIRE_BOOTSTRAP_HOST_UNSUPPORTED")


def install(
    project: str,
    runtime_image: str,
    installer_image: str,
    source: Path,
    output: Path,
) -> dict[str, Any]:
    if os.environ.get(APPROVAL) != VALUE:
        raise BootstrapError("SPIRE_BOOTSTRAP_DOCKER_SOCKET_APPROVAL_REQUIRED")
    if re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", project) is None:
        raise BootstrapError("SPIRE_BOOTSTRAP_PROJECT_INVALID")
    validate_inputs(source)
    if (
        source.resolve() == output.resolve()
        or source.resolve() in output.resolve().parents
    ):
        raise BootstrapError("SPIRE_BOOTSTRAP_OUTPUT_BINDING_INVALID")
    environment = dict(os.environ)
    environment["AELEV_INFRASTRUCTURE_SPIRE_INITIAL_INPUT"] = str(source.resolve())
    system = platform.system()
    if output.is_symlink():
        raise BootstrapError("SPIRE_BOOTSTRAP_OUTPUT_BINDING_INVALID")
    binding = output / ".env"
    if binding.exists():
        lines = binding.read_text().splitlines()
        if (
            binding.is_symlink()
            or len(lines) != 2
            or lines[0] != "# AELEV SPIRE installed composition v1"
            or re.fullmatch(
                r"AELEV_INFRASTRUCTURE_SPIRE_INSTALLED_COMPOSE=\./runtime/installed-composition/[a-f0-9]{64}/compose\.json",
                lines[1],
            )
            is None
        ):
            raise BootstrapError("SPIRE_BOOTSTRAP_OUTPUT_BINDING_INVALID")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    diagnostic = output / ("bootstrap-" + uuid4().hex)
    diagnostic.mkdir(mode=0o700)
    # Resolve the caller's selected release once; subsequent steps use immutable IDs.
    images = [
        invoke(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            environment,
            30,
        ).strip()
        for image in (runtime_image, installer_image)
    ]
    if any(re.fullmatch(r"sha256:[a-f0-9]{64}", image) is None for image in images):
        raise BootstrapError("SPIRE_BOOTSTRAP_IMAGE_ID_REQUIRED")
    runtime, installer = images
    for role in ("server", "observation"):
        invoke(
            [
                *entrypoint("install", system),
                role,
                project,
                runtime,
                installer,
                "--native-restart-policy",
                "no",
            ],
            environment,
            600,
            diagnostic / role,
        )
    arguments = entrypoint("export", system)
    if system == "Windows":
        arguments += [
            "-Project",
            project,
            "-ExportOperatorImage",
            installer,
            "-OutputRoot",
            str(output.resolve()),
        ]
    else:
        arguments += [project, installer, str(output.resolve())]
    invoke(arguments, environment, 180, diagnostic / "export")
    try:
        lines = (output / ".env").read_text().splitlines()
        if len(lines) != 2 or lines[0] != "# AELEV SPIRE installed composition v1":
            raise ValueError
        match = re.fullmatch(
            r"AELEV_INFRASTRUCTURE_SPIRE_INSTALLED_COMPOSE="
            r"(\./runtime/installed-composition/([a-f0-9]{64})/compose\.json)",
            lines[1],
        )
        if match is None:
            raise ValueError
        projection = output / match[1]
        receipt = json.loads((projection.parent.parent / "receipt.json").read_text())
        if receipt["generation"] != match[2] or not projection.is_file():
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise BootstrapError("SPIRE_BOOTSTRAP_PUBLIC_EXPORT_INVALID") from error
    return {
        "status": "SPIRE_RUNTIME_INSTALLED",
        "project": project,
        "composition": str(projection.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("validate", "up"))
    parser.add_argument("--initial-input", required=True, type=Path)
    parser.add_argument("--project")
    parser.add_argument("--runtime-image")
    parser.add_argument("--installer-image")
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "validate":
            validate_inputs(args.initial_input)
            print(
                json.dumps(
                    {
                        "status": "INITIAL_BUNDLE_LAYOUT_VALID",
                        "native_pki_validation_required": True,
                    }
                )
            )
        else:
            if not all(
                (
                    args.project,
                    args.runtime_image,
                    args.installer_image,
                    args.output_root,
                )
            ):
                parser.error(
                    "up requires project, runtime-image, installer-image "
                    "and output-root"
                )
            print(
                json.dumps(
                    install(
                        args.project,
                        args.runtime_image,
                        args.installer_image,
                        args.initial_input,
                        args.output_root,
                    )
                )
            )
    except (BootstrapError, OSError) as error:
        code = (
            str(error)
            if isinstance(error, BootstrapError)
            else "SPIRE_BOOTSTRAP_HOST_OPERATION_FAILED"
        )
        print(code, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
