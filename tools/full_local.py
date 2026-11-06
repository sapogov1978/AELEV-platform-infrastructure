#!/usr/bin/env python3
"""Validate, preflight and inventory the complete clean-room local profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "full-local.json"
DIGEST_REFERENCE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


class FullLocalError(RuntimeError):
    """A bounded, secret-free validation failure."""


def display_path(path: Path, root: Path = ROOT) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def load_json(path: Path, root: Path = ROOT) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FullLocalError(f"JSON_OBJECT_REQUIRED:{display_path(path, root)}")
    return value


def read_env(path: Path, root: Path = ROOT) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or key in values:
            raise FullLocalError(f"ENV_INVALID:{display_path(path, root)}:{number}")
        values[key] = value
    return values


def command(
    arguments: list[str],
    *,
    cwd: Path = ROOT,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )


def stream_command(
    arguments: list[str],
    *,
    cwd: Path = ROOT,
    environment: dict[str, str] | None = None,
) -> None:
    """Run build/pull work with output inherited by the caller's evidence log."""
    subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        check=True,
        text=True,
    )


def validate_contract(root: Path = ROOT) -> tuple[dict[str, Any], list[str]]:
    manifest = load_json(root / "full-local.json", root)
    errors: list[str] = []
    release = load_json(root / "release.json", root)
    if manifest.get("schema_version") != 1:
        errors.append("MANIFEST_SCHEMA_UNSUPPORTED")
    if manifest.get("profile") != "full-local":
        errors.append("MANIFEST_PROFILE_INVALID")
    if manifest.get("release") != release.get("version"):
        errors.append("MANIFEST_RELEASE_MISMATCH")
    for section in ("root_compose", "compute_compose"):
        row = manifest.get(section)
        if not isinstance(row, dict):
            errors.append(f"MANIFEST_SECTION_MISSING:{section}")
            continue
        relative = row.get("file")
        if not isinstance(relative, str) or not (root / relative).is_file():
            errors.append(f"COMPOSE_FILE_MISSING:{section}")
        services = row.get("required_services")
        if (
            not isinstance(services, list)
            or not services
            or len(services) != len(set(services))
            or services != sorted(services)
        ):
            errors.append(f"SERVICE_SET_INVALID:{section}")
    environment_files = manifest.get("environment_files")
    if not isinstance(environment_files, list) or not environment_files:
        errors.append("ENVIRONMENT_FILE_SET_MISSING")
        environment_files = []
    for relative in environment_files:
        if not isinstance(relative, str) or not relative.endswith(".env"):
            errors.append("ENVIRONMENT_FILE_INVALID")
            continue
        example = (root / relative).with_name(".env.example")
        if not example.is_file():
            errors.append(f"ENVIRONMENT_TEMPLATE_MISSING:{relative}")
    for item in manifest.get("external_inputs", []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            errors.append("EXTERNAL_INPUT_INVALID")
            continue
        env_file = item.get("env_file")
        variable = item.get("variable")
        if env_file:
            example = (root / str(env_file)).with_name(".env.example")
            if not example.is_file() or variable not in read_env(example, root):
                errors.append(f"EXTERNAL_INPUT_UNDECLARED:{item['id']}")
    build_input_ids: list[str] = []
    for item in manifest.get("build_inputs", []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            errors.append("BUILD_INPUT_INVALID")
            continue
        build_input_ids.append(item["id"])
        reference = item.get("admitted_reference")
        consumers = item.get("consumers")
        if not isinstance(reference, str) or not DIGEST_REFERENCE.fullmatch(reference):
            errors.append(f"BUILD_INPUT_DIGEST_INVALID:{item['id']}")
            continue
        if not isinstance(consumers, list) or not consumers:
            errors.append(f"BUILD_INPUT_CONSUMERS_MISSING:{item['id']}")
            continue
        for relative in consumers:
            path = root / str(relative)
            if not path.is_file() or reference not in path.read_text(encoding="utf-8"):
                errors.append(f"BUILD_INPUT_NOT_ADMITTED:{item['id']}:{relative}")
    if len(build_input_ids) != len(set(build_input_ids)):
        errors.append("BUILD_INPUT_IDS_NOT_UNIQUE")
    acceptance = manifest.get("acceptance")
    if not isinstance(acceptance, dict):
        errors.append("ACCEPTANCE_MISSING")
    else:
        runner = acceptance.get("runner")
        verifier = acceptance.get("verifier")
        evidence = acceptance.get("evidence_schema")
        functions = acceptance.get("required_functions")
        if not isinstance(runner, str) or not (root / runner).is_file():
            errors.append("ACCEPTANCE_RUNNER_MISSING")
        if not isinstance(verifier, str) or not (root / verifier).is_file():
            errors.append("ACCEPTANCE_VERIFIER_MISSING")
        if not isinstance(evidence, str) or not (root / evidence).is_file():
            errors.append("ACCEPTANCE_SCHEMA_MISSING")
        if not isinstance(functions, list) or not functions:
            errors.append("ACCEPTANCE_FUNCTIONS_MISSING")
    return manifest, errors


def compose_prefix(manifest: dict[str, Any], root: Path = ROOT) -> list[str]:
    row = manifest["root_compose"]
    arguments = ["docker", "compose", "-f", str(root / row["file"])]
    for profile in row["profiles"]:
        arguments.extend(("--profile", profile))
    return arguments


def rendered_model(
    composition: str, manifest: dict[str, Any], root: Path = ROOT
) -> dict[str, Any]:
    if composition == "root":
        arguments = [*compose_prefix(manifest, root), "config", "--format", "json"]
    elif composition == "compute":
        compute = manifest["compute_compose"]
        arguments = [
            "docker",
            "compose",
            "--env-file",
            str(root / compute["env_file"]),
            "-f",
            str(root / compute["file"]),
            "config",
            "--format",
            "json",
        ]
    else:
        raise FullLocalError(f"COMPOSITION_UNKNOWN:{composition}")
    result = command(arguments, cwd=root)
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise FullLocalError(f"COMPOSE_MODEL_INVALID:{composition}")
    return value


def rendered_models(
    manifest: dict[str, Any], root: Path = ROOT
) -> dict[str, dict[str, Any]]:
    return {
        "root": rendered_model("root", manifest, root),
        "compute": rendered_model("compute", manifest, root),
    }


def _required_paths(model: dict[str, Any]) -> set[Path]:
    paths: set[Path] = set()
    for secret in model.get("secrets", {}).values():
        file = secret.get("file")
        if file:
            paths.add(Path(file))
    for service in model.get("services", {}).values():
        for mount in service.get("volumes", []):
            if (
                mount.get("type") == "bind"
                and mount.get("bind", {}).get("create_host_path") is False
            ):
                paths.add(Path(mount["source"]))
    return paths


def _image_inventory(models: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    images: dict[str, dict[str, Any]] = {}
    for composition, model in models.items():
        for service_name, service in model["services"].items():
            reference = service.get("image")
            if not reference:
                continue
            row = images.setdefault(
                reference,
                {"services": [], "buildable": bool(service.get("build"))},
            )
            row["services"].append(f"{composition}:{service_name}")
            row["buildable"] = row["buildable"] or bool(service.get("build"))
    return images


def preflight(
    root: Path = ROOT, *, require_images: bool = False
) -> tuple[dict[str, Any], int]:
    manifest, contract_errors = validate_contract(root)
    errors = list(contract_errors)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, blocking_code: str) -> None:
        checks.append(
            {
                "name": name,
                "status": "PASS" if ok else "BLOCKED",
                "detail": "OK" if ok else blocking_code,
            }
        )
        if not ok:
            errors.append(blocking_code)

    for executable in ("docker", "openssl"):
        check(
            f"tool:{executable}",
            shutil.which(executable) is not None,
            f"TOOL_UNAVAILABLE:{executable}",
        )
    for relative in manifest.get("environment_files", []):
        path = root / relative
        check(f"env:{relative}", path.is_file(), f"ENV_FILE_MISSING:{relative}")
    for item in manifest.get("external_inputs", []):
        if item.get("kind") == "operator-confirmation":
            value = os.environ.get(str(item["variable"]), "")
            ok = value == item.get("expected")
        else:
            env_path = root / str(item["env_file"])
            value = (
                read_env(env_path, root).get(str(item["variable"]), "")
                if env_path.is_file()
                else ""
            )
            ok = bool(DIGEST_REFERENCE.fullmatch(value))
        check(
            f"external:{item['id']}",
            ok,
            f"EXTERNAL_INPUT_NOT_ADMITTED:{item['id']}",
        )
    for item in manifest.get("build_inputs", []):
        reference = item.get("admitted_reference", "")
        consumers = item.get("consumers", [])
        ok = bool(DIGEST_REFERENCE.fullmatch(reference)) and all(
            (root / relative).is_file()
            and reference in (root / relative).read_text(encoding="utf-8")
            for relative in consumers
        )
        check(
            f"build-input:{item['id']}",
            ok,
            f"BUILD_INPUT_NOT_ADMITTED:{item['id']}",
        )
    models: dict[str, dict[str, Any]] = {}
    render_blockers = contract_errors + [
        code for code in errors if code.startswith("ENV_FILE_MISSING:")
    ]
    if not render_blockers:
        for composition in ("root", "compute"):
            try:
                models[composition] = rendered_model(composition, manifest, root)
            except (
                FullLocalError,
                subprocess.CalledProcessError,
                json.JSONDecodeError,
            ):
                errors.append(f"COMPOSE_RENDER_FAILED:{composition}")
    for composition, section in (
        ("root", manifest.get("root_compose", {})),
        ("compute", manifest.get("compute_compose", {})),
    ):
        if composition not in models:
            continue
        actual = sorted(models[composition].get("services", {}))
        expected = section.get("required_services", [])
        check(
            f"services:{composition}",
            actual == expected,
            f"SERVICE_SET_MISMATCH:{composition}",
        )
    missing_paths = sorted(
        str(path)
        for model in models.values()
        for path in _required_paths(model)
        if not path.exists()
    )
    if models:
        check(
            "owner-input-paths",
            not missing_paths,
            "OWNER_INPUT_PATHS_MISSING:" + str(len(missing_paths)),
        )
    images = _image_inventory(models) if models else {}
    locally_built = {
        row["image"]
        for row in manifest.get("local_builds", [])
        if isinstance(row, dict) and isinstance(row.get("image"), str)
    }
    mutable_pull = sorted(
        reference
        for reference, row in images.items()
        if not row["buildable"]
        and reference not in locally_built
        and not DIGEST_REFERENCE.fullmatch(reference)
    )
    if images:
        check(
            "immutable-pull-images",
            not mutable_pull,
            "MUTABLE_PULL_IMAGES:" + str(len(mutable_pull)),
        )
    missing_images: list[str] = []
    if require_images:
        for reference in images:
            result = subprocess.run(
                ["docker", "image", "inspect", reference],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode:
                missing_images.append(reference)
        check(
            "local-images",
            not missing_images,
            "LOCAL_IMAGES_MISSING:" + str(len(missing_images)),
        )
    result = {
        "schema_version": 1,
        "profile": "full-local",
        "release": manifest.get("release"),
        "status": "READY" if not errors else "BLOCKED",
        "checks": checks,
        "blocking_codes": sorted(set(errors)),
        "missing_paths": missing_paths,
        "mutable_pull_images": mutable_pull,
        "missing_images": missing_images,
    }
    return result, 0 if not errors else 2


def source_plan(root: Path = ROOT) -> dict[str, Any]:
    manifest, errors = validate_contract(root)
    if errors:
        raise FullLocalError(";".join(errors))
    return {
        "schema_version": 2,
        "release": manifest["release"],
        "profile": manifest["profile"],
        "owners": manifest["owners"],
        "root_services": manifest["root_compose"]["required_services"],
        "compute_services": manifest["compute_compose"]["required_services"],
        "external_inputs": [
            {
                "id": item["id"],
                "kind": item["kind"],
                "owner": item["owner"],
            }
            for item in manifest["external_inputs"]
        ],
        "build_inputs": [
            {
                "id": item["id"],
                "kind": item["kind"],
                "owner": item["owner"],
                "admitted_reference": item["admitted_reference"],
            }
            for item in manifest.get("build_inputs", [])
        ],
        "compose_models_included": ["root", "compute"],
        "translation_provider_implementation_included": False,
        "acceptance_functions": manifest["acceptance"]["required_functions"],
    }


def prepare_status_output_roots(
    manifest: dict[str, Any], root: Path = ROOT
) -> list[str]:
    prepared: list[str] = []
    for relative in manifest["environment_files"]:
        env_path = root / relative
        owner = env_path.parent.resolve()
        for key, value in read_env(env_path, root).items():
            if not key.endswith(
                ("_UI_SECRET_ROOT", "_OIDC_SECRET_ROOT", "_OPERATOR_SECRET_ROOT")
            ):
                continue
            path = (owner / value).resolve()
            if path != owner and owner not in path.parents:
                raise FullLocalError(f"OWNER_OUTPUT_PATH_INVALID:{relative}")
            path.mkdir(parents=True, exist_ok=True, mode=0o750)
            prepared.append(display_path(path, root))
    return sorted(set(prepared))


def adapt_created_env_identities(
    manifest: dict[str, Any], preexisting: set[str], root: Path = ROOT
) -> list[str]:
    """Bind only newly initialized local env files to the current host identity."""
    adapted: list[str] = []
    replacements = {
        "_SECRET_GID": str(os.getgid()),
        "_PROVISION_UID": str(os.getuid()),
    }
    for relative in manifest["environment_files"]:
        if relative in preexisting:
            continue
        path = root / relative
        rows: list[str] = []
        changed = False
        for row in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = row.partition("=")
            replacement = next(
                (
                    candidate
                    for suffix, candidate in replacements.items()
                    if key.endswith(suffix)
                ),
                None,
            )
            if separator and replacement is not None and value != replacement:
                row = f"{key}={replacement}"
                changed = True
            rows.append(row)
        if changed:
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            adapted.append(relative)
    return adapted


def prepare_owner_inputs(manifest: dict[str, Any], root: Path = ROOT) -> list[str]:
    prepared: list[str] = []
    observability = root / "observability"
    issuer = observability / "runtime/local-issuer"
    if shutil.which("poetry") is None:
        raise FullLocalError("TOOL_UNAVAILABLE:poetry")
    clean_environment = {
        key: value for key, value in os.environ.items() if not key.startswith("AELEV_")
    }
    for relative in manifest["owners"]:
        if not relative.startswith("observability/"):
            continue
        owner = root / relative
        command(
            ["poetry", "install", "--no-interaction", "--no-ansi"],
            cwd=owner,
            environment=clean_environment,
        )
        native = owner / "secrets/native"
        if not native.exists():
            command(
                [
                    "poetry",
                    "run",
                    "python",
                    "prepare.py",
                    "tls",
                    "--issuer-dir",
                    str(issuer.resolve()),
                ],
                cwd=owner,
                environment=clean_environment,
            )
        command(
            ["poetry", "run", "python", "prepare.py", "render"],
            cwd=owner,
            environment=clean_environment,
        )
        prepared.append(relative)
    egress = root / "public-egress-gateway"
    local_ca, gateway = egress / "secrets/local-ca", egress / "secrets/gateway"
    if local_ca.exists() != gateway.exists():
        raise FullLocalError("PUBLIC_EGRESS_LOCAL_PKI_PARTIAL")
    if not local_ca.exists():
        command(
            [
                sys.executable,
                "prepare.py",
                "init-local",
                "--root",
                "secrets",
                "--server-name",
                "public-egress-gateway",
            ],
            cwd=egress,
        )
    prepared.append("public-egress-gateway")
    command(
        [
            sys.executable,
            "prepare-local.py",
            "--identity-ca",
            str(root / "keycloak/secrets/https/ca.crt"),
        ],
        cwd=root / "translator-service",
    )
    prepared.append("translator-service")
    prepared.extend(prepare_status_output_roots(manifest, root))
    return prepared


def initialize_profile(root: Path = ROOT) -> dict[str, Any]:
    initial_manifest = load_json(root / "full-local.json", root)
    preexisting = {
        relative
        for relative in initial_manifest["environment_files"]
        if (root / relative).exists()
    }
    command([sys.executable, str(root / "bootstrap.py"), "init"], cwd=root)
    manifest, errors = validate_contract(root)
    if errors:
        raise FullLocalError(";".join(errors))
    adapted = adapt_created_env_identities(manifest, preexisting, root)
    prepared = prepare_owner_inputs(manifest, root)
    return {
        "schema_version": 1,
        "profile": manifest["profile"],
        "release": manifest["release"],
        "status": "INITIALIZED",
        "host_identity_envs": adapted,
        "prepared_owner_inputs": prepared,
        "containers_started": False,
    }


def build_service_sets(model: dict[str, Any]) -> tuple[list[str], list[str]]:
    source_builds = sorted(
        name
        for name, row in model["services"].items()
        if row.get("build") and not DIGEST_REFERENCE.fullmatch(row.get("image", ""))
    )
    pinned_pulls = sorted(
        name
        for name, row in model["services"].items()
        if row.get("build") and DIGEST_REFERENCE.fullmatch(row.get("image", ""))
    )
    return source_builds, pinned_pulls


def build_profile(root: Path = ROOT) -> dict[str, Any]:
    initialized = initialize_profile(root)
    manifest = load_json(root / "full-local.json", root)
    models = rendered_models(manifest, root)
    model = models["root"]
    services, pinned_pull_services = build_service_sets(model)
    if services:
        stream_command([*compose_prefix(manifest, root), "build", *services], cwd=root)
    local_images: list[str] = []
    for row in manifest.get("local_builds", []):
        arguments = [
            "docker",
            "build",
            "--target",
            str(row["target"]),
            "--tag",
            str(row["image"]),
            str(root / row["context"]),
        ]
        stream_command(arguments, cwd=root)
        local_images.append(str(row["image"]))
    exact_pull_images: list[str] = []
    for reference, row in sorted(_image_inventory(models).items()):
        present = subprocess.run(
            ["docker", "image", "inspect", reference],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not present.returncode:
            continue
        if reference in local_images or (
            row["buildable"] and not DIGEST_REFERENCE.fullmatch(reference)
        ):
            raise FullLocalError(f"LOCAL_BUILD_IMAGE_MISSING:{reference}")
        if not DIGEST_REFERENCE.fullmatch(reference):
            raise FullLocalError(f"MUTABLE_PULL_IMAGE_REFUSED:{reference}")
        stream_command(["docker", "pull", reference], cwd=root)
        exact_pull_images.append(reference)
    pinned_pull_images = sorted(
        {model["services"][name]["image"] for name in pinned_pull_services}
        | set(exact_pull_images)
    )
    return {
        **initialized,
        "status": "BUILT",
        "compose_services": services,
        "pinned_pull_services": pinned_pull_services,
        "pinned_pull_images": pinned_pull_images,
        "local_images": local_images,
        "containers_started": False,
    }


def build_bom(
    output: Path, root: Path = ROOT, *, allow_dirty: bool = False
) -> dict[str, Any]:
    result, code = preflight(root, require_images=True)
    if code:
        raise FullLocalError("FULL_LOCAL_PREFLIGHT_BLOCKED")
    status = command(["git", "status", "--porcelain"], cwd=root).stdout
    if status and not allow_dirty:
        raise FullLocalError("SOURCE_TREE_DIRTY")
    revision = command(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
    manifest = load_json(root / "full-local.json", root)
    models = rendered_models(manifest, root)
    images = _image_inventory(models)
    for reference, row in images.items():
        inspected = json.loads(
            command(["docker", "image", "inspect", reference], cwd=root).stdout
        )[0]
        row["image_id"] = inspected["Id"]
        row["repo_digests"] = sorted(inspected.get("RepoDigests") or [])
        row["services"] = sorted(row["services"])
    compose_digests = {
        name: "sha256:"
        + hashlib.sha256(
            json.dumps(model, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for name, model in models.items()
    }
    bom = {
        "schema_version": 1,
        "repository": "sapogov1978/AELEV-platform-infrastructure",
        "release": manifest["release"],
        "profile": "full-local",
        "source_commit": revision,
        "source_dirty": bool(status),
        "compose_digests": compose_digests,
        "images": dict(sorted(images.items())),
        "external_inputs": [
            row for row in result["checks"] if row["name"].startswith("external:")
        ],
        "build_inputs": [
            {
                "id": item["id"],
                "owner": item["owner"],
                "admitted_reference": item["admitted_reference"],
                "release_run": item["release_run"],
                "consumers": item["consumers"],
                "status": next(
                    row["status"]
                    for row in result["checks"]
                    if row["name"] == f"build-input:{item['id']}"
                ),
            }
            for item in manifest.get("build_inputs", [])
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(bom, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return bom


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command_name", required=True)
    subparsers.add_parser("validate")
    subparsers.add_parser("plan")
    subparsers.add_parser("init")
    subparsers.add_parser("build")
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--require-images", action="store_true")
    bom_parser = subparsers.add_parser("bom")
    bom_parser.add_argument("--output", required=True, type=Path)
    bom_parser.add_argument("--allow-dirty", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.command_name == "validate":
            _, errors = validate_contract()
            payload = {"status": "PASS" if not errors else "FAIL", "errors": errors}
            print(json.dumps(payload, indent=2))
            return int(bool(errors))
        if arguments.command_name == "plan":
            print(json.dumps(source_plan(), indent=2))
            return 0
        if arguments.command_name == "init":
            print(json.dumps(initialize_profile(), indent=2))
            return 0
        if arguments.command_name == "build":
            print(json.dumps(build_profile(), indent=2))
            return 0
        if arguments.command_name == "preflight":
            payload, code = preflight(require_images=arguments.require_images)
            print(json.dumps(payload, indent=2))
            return code
        bom = build_bom(arguments.output, allow_dirty=arguments.allow_dirty)
        print(json.dumps({"status": "PASS", "images": len(bom["images"])}, indent=2))
        return 0
    except (FullLocalError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print(json.dumps({"status": "FAIL", "code": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
