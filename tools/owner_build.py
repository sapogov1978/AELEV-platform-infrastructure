"""Build one non-opaque owner image from a manifest-only source snapshot."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from typing import Any

from tools import node_execution as node
from tools import owner_compose as owner

FIELDS = {
    "schema_version",
    "executor_root",
    "executor_files",
    "owner_root",
    "owner_files",
    "catalog",
    "role",
    "context",
    "recipe",
    "source_sha256",
    "target",
    "platform",
    "build_arguments",
    "inputs",
}
MAX_CONTEXT_BYTES = 256 * 1024**2


def relative(value: Any) -> str:
    node.require(
        isinstance(value, str)
        and bool(value)
        and "\\" not in value
        and not PurePosixPath(value).is_absolute()
        and ".." not in PurePosixPath(value).parts
        and str(PurePosixPath(value)) == value,
        "OWNER_BUILD_PATH_INVALID",
    )
    return str(value)


def definition(value: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    node.require(
        set(value) == FIELDS
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["inputs"] == {},
        "OWNER_BUILD_REQUEST_INVALID",
    )
    executor, files = owner.source_tree(value["executor_root"], value["executor_files"])
    node.require(
        all(
            name in files
            for name in (
                "tools/owner_build.py",
                "tools/owner_compose.py",
                "tools/node_execution.py",
            )
        )
        and owner.digest(Path(__file__).read_bytes()) == files["tools/owner_build.py"],
        "OWNER_BUILD_EXECUTOR_MISMATCH",
    )
    root, files = owner.source_tree(value["owner_root"], value["owner_files"])
    catalog_name = relative(value["catalog"])
    node.require(catalog_name in files, "OWNER_BUILD_CATALOG_REQUIRED")
    catalog = node.read(root / catalog_name)
    roles = [
        r
        for r in catalog["roles"]
        if catalog["product"] + "/" + r["id"] == value["role"]
    ]
    node.require(len(roles) == 1, "OWNER_BUILD_ROLE_INVALID")
    role = roles[0]
    node.require(role.get("opaque") is False, "OWNER_BUILD_OPAQUE_FORBIDDEN")
    context, recipe, owner_path = (
        relative(value["context"]),
        relative(value["recipe"]),
        relative(role["owner"]),
    )
    node.require(
        PurePosixPath(context).is_relative_to(PurePosixPath(owner_path))
        and PurePosixPath(recipe).is_relative_to(PurePosixPath(context))
        and recipe in files
        and value["source_sha256"] == "sha256:" + files[recipe],
        "OWNER_BUILD_SOURCE_BINDING_INVALID",
    )
    node.require(
        isinstance(value["target"], str)
        and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}", value["target"])
        is not None
        and value["platform"] in {"linux/amd64", "linux/arm64"},
        "OWNER_BUILD_TARGET_OR_PLATFORM_INVALID",
    )
    arguments = value["build_arguments"]
    node.require(
        isinstance(arguments, dict) and len(arguments) <= 64,
        "OWNER_BUILD_ARGUMENTS_INVALID",
    )
    declared = set(
        re.findall(
            r"^\s*ARG\s+([a-zA-Z_][a-zA-Z0-9_]*)",
            (root / recipe).read_text(),
            re.MULTILINE | re.IGNORECASE,
        )
    )
    for name, body in arguments.items():
        node.require(
            isinstance(name, str)
            and name in declared
            and re.search(
                r"(?:PASSWORD|SECRET|TOKEN|API_KEY|PRIVATE_KEY)", name, re.IGNORECASE
            )
            is None
            and isinstance(body, str)
            and len(body) <= 4096
            and not any(c in body for c in "\0\r\n")
            and re.search(r"://[^/]*@", body) is None,
            "OWNER_BUILD_ARGUMENTS_INVALID",
        )
    node.require(
        not executor.is_relative_to(root / context),
        "OWNER_BUILD_CONTEXT_SEPARATION_REQUIRED",
    )
    return root, files


def snapshot(root: Path, files: dict[str, Any], context: str, output: Path) -> str:
    records: dict[str, Any] = {}
    total = 0
    prefix = PurePosixPath(context)
    # The operation ancestor stays 0700. Public source directories must remain
    # traversable after Docker COPY into an unprivileged runtime image.
    output.chmod(0o755)
    for name, expected in sorted(files.items()):
        source_name = PurePosixPath(name)
        if not source_name.is_relative_to(prefix):
            continue
        source = node.absolute(str(root / name))
        data = source.read_bytes()
        total += len(data)
        node.require(
            len(data) <= 32 * 1024**2
            and total <= MAX_CONTEXT_BYTES
            and len(records) < 20000,
            "OWNER_BUILD_CONTEXT_TOO_LARGE",
        )
        node.require(owner.digest(data) == expected, "OWNER_BUILD_SOURCE_DRIFT")
        relative_name = str(source_name.relative_to(prefix))
        destination = output / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        for parent in destination.parents:
            if parent == output:
                break
            parent.chmod(0o755)
        mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
        with destination.open("xb") as stream:
            stream.write(data)
        destination.chmod(mode)
        records[relative_name] = {"sha256": expected, "mode": mode}
    node.require(bool(records), "OWNER_BUILD_CONTEXT_EMPTY")
    return owner.digest(node.encode({"files": records, "directory_mode": 0o755}))


def docker(arguments: list[str]) -> bytes:
    result = subprocess.run(
        ["docker", *arguments],
        env=owner.environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    node.require(result.returncode == 0, "OWNER_BUILD_DOCKER_OBSERVATION_FAILED")
    return result.stdout


def execute(value: dict[str, Any], directory: Path) -> dict[str, Any]:
    root, files = definition(value)
    node.private(node.absolute(str(directory)), directory=True)
    node.require(
        not directory.is_relative_to(root)
        and not directory.is_relative_to(Path(value["executor_root"])),
        "OWNER_BUILD_PRIVATE_OUTPUT_REQUIRED",
    )
    native = (
        docker(["info", "--format", "{{.OSType}}/{{.Architecture}}"]).decode().strip()
    )
    native = {"linux/aarch64": "linux/arm64", "linux/x86_64": "linux/amd64"}.get(
        native, native
    )
    node.require(native == value["platform"], "OWNER_BUILD_NATIVE_PLATFORM_REQUIRED")
    iidfile, metadata = directory / "build-image-id", directory / "build-metadata.json"
    node.require(
        not any(p.exists() or p.is_symlink() for p in (iidfile, metadata)),
        "OWNER_BUILD_OUTPUT_INSPECTION_REQUIRED",
    )
    with tempfile.TemporaryDirectory(
        prefix="build-source-", dir=directory
    ) as temporary:
        context = Path(temporary)
        context_hash = snapshot(root, files, value["context"], context)
        command = [
            "docker",
            "buildx",
            "build",
            "--load",
            "--progress",
            "plain",
            "--file",
            str(context / PurePosixPath(value["recipe"]).relative_to(value["context"])),
            "--target",
            value["target"],
            "--platform",
            value["platform"],
            "--iidfile",
            str(iidfile),
            "--metadata-file",
            str(metadata),
        ]
        for name, body in sorted(value["build_arguments"].items()):
            command += ["--build-arg", name + "=" + body]
        command.append(str(context))
        environment = owner.environment()
        environment["BUILDX_METADATA_PROVENANCE"] = "max"
        try:
            built = subprocess.run(
                command,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=sys.stderr,
                stderr=sys.stderr,
                timeout=6600,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "RESULT_UNKNOWN",
                "code": "OWNER_BUILD_RESULT_UNKNOWN_RECONCILIATION_REQUIRED",
            }
        if built.returncode:
            return {
                "status": "BLOCKED",
                "code": "OWNER_BUILD_COMMAND_FAILED",
                "owner_exit_code": built.returncode,
            }
    node.require(
        iidfile.is_file()
        and metadata.is_file()
        and not iidfile.is_symlink()
        and not metadata.is_symlink()
        and iidfile.stat().st_size <= 256
        and metadata.stat().st_size <= node.LIMIT,
        "OWNER_BUILD_OUTPUT_INVALID",
    )
    for path in (iidfile, metadata):
        path.chmod(0o600)
    identity = iidfile.read_text().strip()
    node.require(
        re.fullmatch(r"sha256:[a-f0-9]{64}", identity) is not None,
        "OWNER_BUILD_OUTPUT_INVALID",
    )
    projection = (
        '{"id":{{json .Id}},"os":{{json .Os}},"architecture":{{json .Architecture}}}'
    )
    image = json.loads(docker(["image", "inspect", "--format", projection, identity]))
    node.require(
        isinstance(image, dict)
        and set(image) == {"id", "os", "architecture"}
        and isinstance(image["id"], str)
        and re.fullmatch(r"sha256:[a-f0-9]{64}", image["id"]) is not None
        and image["os"] + "/" + image["architecture"] == value["platform"],
        "OWNER_BUILD_IMAGE_PLATFORM_MISMATCH",
    )
    parameters = {
        k: value[k]
        for k in (
            "role",
            "context",
            "recipe",
            "source_sha256",
            "target",
            "platform",
            "build_arguments",
        )
    }
    parameters["owner_manifest_sha256"] = owner.digest(
        node.encode(value["owner_files"])
    )
    return {
        "status": "OWNER_IMAGE_BUILT",
        "native_image_id": image["id"],
        "build_output_id": identity,
        "context_sha256": context_hash,
        "parameters_sha256": owner.digest(node.encode(parameters)),
        "platform": value["platform"],
        "build_metadata_sha256": owner.digest(metadata.read_bytes()),
        "owner_readiness_verified": False,
        "image_admitted": False,
        "queue_resume_issued": False,
        "physical_distribution_verified": False,
    }


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build",))
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--request-sha256", required=True)
    arguments = parser.parse_args()
    try:
        request = node.absolute(str(arguments.request))
        node.private(request)
        data = request.read_bytes()
        node.require(
            len(data) <= node.LIMIT and owner.digest(data) == arguments.request_sha256,
            "OWNER_BUILD_REQUEST_DRIFT",
        )

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            parsed: dict[str, Any] = {}
            for key, body in pairs:
                node.require(key not in parsed, "OWNER_BUILD_DUPLICATE_JSON_KEY")
                parsed[key] = body
            return parsed

        value = json.loads(data, object_pairs_hook=unique)
        node.require(isinstance(value, dict), "OWNER_BUILD_REQUEST_INVALID")
        result = execute(
            value, node.absolute(os.environ.get("AELEV_NODE_OPERATION_DIRECTORY", ""))
        )
    except node.ExecutionError as error:
        result = {"status": "BLOCKED", "code": str(error)}
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        subprocess.TimeoutExpired,
    ):
        result = {"status": "BLOCKED", "code": "OWNER_BUILD_INPUT_OR_RESULT_INVALID"}
    print(json.dumps(result, sort_keys=True))
    return (
        0
        if result["status"] == "OWNER_IMAGE_BUILT"
        else 3 if result["status"] == "RESULT_UNKNOWN" else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
