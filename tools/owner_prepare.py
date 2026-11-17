"""Create and verify one private owner workspace from pinned public sources."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from typing import Any, Iterator

from tools import node_execution as node
from tools import owner_build as build
from tools import owner_compose as owner

FIELDS = {
    "schema_version",
    "executor_root",
    "executor_files",
    "owner_root",
    "owner_files",
    "catalog",
    "role",
    "workspace",
    "env_file",
    "inputs",
}
RECEIPT = ".owner-workspace.json"


def definition(value: dict[str, Any]) -> tuple[Path, str, Path, dict[str, Any]]:
    node.require(
        set(value) == FIELDS
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1,
        "OWNER_PREPARE_REQUEST_INVALID",
    )
    executor, executor_files = owner.source_tree(
        value["executor_root"], value["executor_files"]
    )
    node.require(
        all(
            name in executor_files
            for name in (
                "tools/owner_prepare.py",
                "tools/owner_build.py",
                "tools/owner_compose.py",
                "tools/node_execution.py",
            )
        )
        and owner.digest(Path(__file__).read_bytes())
        == executor_files["tools/owner_prepare.py"],
        "OWNER_PREPARE_EXECUTOR_MISMATCH",
    )
    root, files = owner.source_tree(value["owner_root"], value["owner_files"])
    catalog_name = build.relative(value["catalog"])
    node.require(catalog_name in files, "OWNER_PREPARE_CATALOG_REQUIRED")
    catalog = node.read(root / catalog_name)
    roles = [
        row
        for row in catalog["roles"]
        if catalog["product"] + "/" + row["id"] == value["role"]
    ]
    node.require(len(roles) == 1, "OWNER_PREPARE_ROLE_INVALID")
    prefix = build.relative(roles[0]["owner"])
    node.require(
        all(
            PurePosixPath(name).is_relative_to(PurePosixPath(prefix))
            for name in roles[0]["recipes"]
        ),
        "OWNER_PREPARE_RECIPE_OWNER_MISMATCH",
    )
    workspace = node.absolute(value["workspace"])
    node.require(
        not workspace.is_relative_to(root)
        and not workspace.is_relative_to(executor)
        and not root.is_relative_to(workspace)
        and not executor.is_relative_to(workspace),
        "OWNER_PREPARE_WORKSPACE_SEPARATION_REQUIRED",
    )
    node.private(workspace.parent, directory=True)
    environment = node.absolute(value["env_file"])
    node.private(environment)
    body = environment.read_bytes()
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise node.ExecutionError("OWNER_PREPARE_ENVIRONMENT_INVALID") from error
    node.require(
        value["inputs"] == {str(environment): owner.digest(body)},
        "OWNER_PREPARE_ENVIRONMENT_BINDING_INVALID",
    )
    node.require(
        0 < len(body) <= 1024**2 and b"\0" not in body,
        "OWNER_PREPARE_ENVIRONMENT_INVALID",
    )
    records = {}
    for name, checksum in files.items():
        path = PurePosixPath(name)
        if path.is_relative_to(PurePosixPath(prefix)):
            relative = str(path.relative_to(PurePosixPath(prefix)))
            node.require(
                relative not in {".env", RECEIPT},
                "OWNER_PREPARE_PRIVATE_FILE_COLLISION",
            )
            records[relative] = {
                "sha256": checksum,
                "mode": 0o755 if (root / name).stat().st_mode & 0o111 else 0o644,
            }
    node.require(0 < len(records) <= 20000, "OWNER_PREPARE_PUBLIC_FILE_SET_INVALID")
    expected = {
        "schema_version": 1,
        "role": value["role"],
        "workspace": str(workspace),
        "public_files": records,
        "environment_sha256": owner.digest(body),
    }
    return root, prefix, workspace, expected


def verify(workspace: Path, expected: dict[str, Any]) -> dict[str, Any]:
    node.private(workspace, directory=True)
    receipt = node.absolute(str(workspace / RECEIPT))
    node.private(receipt)
    node.require(
        node.read(receipt) == expected, "OWNER_PREPARE_WORKSPACE_HANDOFF_REQUIRED"
    )
    for name, record in {
        **expected["public_files"],
        ".env": {"sha256": expected["environment_sha256"], "mode": 0o600},
    }.items():
        path = node.absolute(str(workspace / name))
        info = path.stat()
        node.require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == record["mode"]
            and owner.digest(path.read_bytes()) == record["sha256"],
            "OWNER_PREPARE_WORKSPACE_DRIFT",
        )
    return {
        "status": "OWNER_CONFIGURATION_PREPARED",
        "workspace_sha256": owner.digest(node.encode(expected)),
        "public_files_sha256": owner.digest(node.encode(expected["public_files"])),
        "environment_sha256": expected["environment_sha256"],
    }


def sync_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    directories = [root, *(p for p in root.rglob("*") if p.is_dir())]
    for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def workspace_lock(workspace: Path) -> Iterator[None]:
    name = ".owner-prepare-" + owner.digest(str(workspace).encode()) + ".lock"
    path = node.absolute(str(workspace.parent / name))
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        node.private(path)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def publish(
    value: dict[str, Any],
    root: Path,
    prefix: str,
    workspace: Path,
    expected: dict[str, Any],
) -> dict[str, Any]:
    if workspace.exists():
        return verify(workspace, expected)
    # Publication is create-only. A failed attempt remains private; no old
    # workspace, credentials, data, process or Docker reference is modified.
    pending = Path(tempfile.mkdtemp(prefix=".owner-prepare-", dir=workspace.parent))
    build.snapshot(root, value["owner_files"], prefix, pending)
    pending.chmod(0o700)
    body = Path(value["env_file"]).read_bytes()
    node.require(
        owner.digest(body) == expected["environment_sha256"],
        "OWNER_PREPARE_ENVIRONMENT_BINDING_INVALID",
    )
    with (pending / ".env").open("xb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    (pending / ".env").chmod(0o600)
    node.save(pending / RECEIPT, expected)
    sync_tree(pending)
    verify(pending, expected)
    node.require(
        not workspace.exists() and not workspace.is_symlink(),
        "OWNER_PREPARE_CONCURRENT_WORKSPACE_REQUIRED",
    )
    os.rename(pending, workspace)
    descriptor = os.open(workspace.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return verify(workspace, expected)


def execute(value: dict[str, Any], action: str) -> dict[str, Any]:
    root, prefix, workspace, expected = definition(value)
    if action == "status":
        node.require(workspace.exists(), "OWNER_PREPARE_WORKSPACE_NOT_FOUND")
        return verify(workspace, expected)
    node.require(action == "prepare", "OWNER_PREPARE_ACTION_INVALID")
    with workspace_lock(workspace):
        return publish(value, root, prefix, workspace, expected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "status"))
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--request-sha256", required=True)
    args = parser.parse_args()
    try:
        request = node.absolute(str(args.request))
        node.private(request)
        node.require(
            owner.digest(request.read_bytes()) == args.request_sha256,
            "OWNER_PREPARE_REQUEST_DRIFT",
        )
        result = execute(node.read(request), args.action)
    except node.ExecutionError as error:
        print(json.dumps({"status": "BLOCKED", "code": str(error)}))
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "code": "OWNER_PREPARE_INPUT_OR_ENVIRONMENT_INVALID",
                }
            )
        )
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
