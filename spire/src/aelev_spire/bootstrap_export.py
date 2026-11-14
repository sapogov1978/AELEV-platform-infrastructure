"""Export an existing short-lived authorization, never issue or renew one."""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
import time
import uuid
from pathlib import Path
from typing import Any, cast

from .files import atomic, read, reserve
from .settings import OWNER

NAMES = ("plan.json", "bundle.crt", "join-token")


def expiry(value: Any) -> float:
    """Reject boolean, nonfinite and missing expiry without extending its value."""
    if type(value) not in (int, float):
        raise ValueError("SPIRE_OPERATOR_INITIAL_AUTHORIZATION_EXPIRED")
    result = float(value)
    if not math.isfinite(result) or time.time() >= result:
        raise ValueError("SPIRE_OPERATOR_INITIAL_AUTHORIZATION_EXPIRED")
    return result


def consume(root: Path, operation: str) -> dict[str, bytes]:
    """Validate a privately transferred package before the first agent install."""
    if root.is_symlink() or stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise ValueError("SPIRE_BOOTSTRAP_EXPORT_PERMISSIONS_INVALID")
    for name in (*NAMES, "receipt.json"):
        info = (root / name).lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ValueError("SPIRE_BOOTSTRAP_EXPORT_PERMISSIONS_INVALID")
    receipt = read(root / "receipt.json")
    files = material(root)
    expires = expiry(receipt.get("expires_at"))
    if (
        set(receipt)
        != {
            "owner",
            "schema_version",
            "kind",
            "operation_id",
            "node_id",
            "resource_owner",
            "request_digest",
            "expires_at",
            "files",
        }
        or receipt["owner"] != OWNER
        or type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["kind"] != "agent-bootstrap-export"
        or str(uuid.UUID(operation)) != operation
        or receipt["operation_id"] != operation
        or str(uuid.UUID(receipt["node_id"])) != receipt["node_id"]
        or not isinstance(receipt["resource_owner"], str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", receipt["resource_owner"])
        or not isinstance(receipt["request_digest"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", receipt["request_digest"])
        or time.time() >= expires
        or receipt["files"]
        != {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
    ):
        raise ValueError("SPIRE_BOOTSTRAP_EXPORT_PACKAGE_INVALID_OR_EXPIRED")
    raw: Any = json.loads(files["plan.json"])
    if not isinstance(raw, dict):
        raise ValueError("SPIRE_BOOTSTRAP_EXPORT_SOURCE_MISMATCH")
    value = cast(dict[str, Any], raw)
    if (
        value.get("node_id") != receipt["node_id"]
        or not isinstance(value.get("spiffe_id"), str)
        or not value["spiffe_id"].startswith(
            "spiffe://"
            + value.get("trust_domain", "")
            + "/"
            + receipt["resource_owner"]
            + "/"
        )
    ):
        raise ValueError("SPIRE_BOOTSTRAP_EXPORT_SOURCE_MISMATCH")
    return files


def plan(request: dict[str, Any]) -> dict[str, Any]:
    """Use the same exact public agent plan as initial authorization."""
    return {
        key: request[key]
        for key in (
            "owner",
            "schema_version",
            "environment",
            "trust_domain",
            "spiffe_id",
            "node_id",
            "server_address",
            "svid_ttl_seconds",
            "policy_mountpoints",
        )
        if key in request
    } | {"server_port": request.get("server_port", 8081)}


def material(root: Path) -> dict[str, bytes]:
    """Read only three bounded regular files; no state or private CA material."""
    if any(parent.is_symlink() for parent in (root, *root.parents)):
        raise ValueError("SPIRE_BOOTSTRAP_EXPORT_PATH_INVALID")
    result: dict[str, bytes] = {}
    for name in NAMES:
        path = root / name
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not 0 < info.st_size <= 65536
        ):
            raise ValueError("SPIRE_BOOTSTRAP_EXPORT_FILE_INVALID")
        result[name] = path.read_bytes()
    return result


def export(
    request: dict[str, Any],
    state: dict[str, Any],
    source: Path,
    target: Path,
    bundle: bytes,
) -> dict[str, Any]:
    """Called only under the operator lock after checking the native entry."""
    expires = expiry(state.get("token_expires_at"))
    files = material(source)
    if (
        read(source / "owner.json")
        != {
            "owner": OWNER,
            "role": "agent-bootstrap",
            "operation_id": request["operation_id"],
        }
        or json.loads(files["plan.json"]) != plan(request)
        or files["join-token"] != state["token"].encode()
        or files["bundle.crt"] != bundle
    ):
        raise ValueError("SPIRE_BOOTSTRAP_EXPORT_SOURCE_MISMATCH")
    receipt = {
        "owner": OWNER,
        "schema_version": 1,
        "kind": "agent-bootstrap-export",
        "operation_id": request["operation_id"],
        "node_id": request["node_id"],
        "resource_owner": request["resource_owner"],
        "request_digest": state["request_digest"],
        "expires_at": expires,
        "files": {
            name: hashlib.sha256(value).hexdigest() for name, value in files.items()
        },
    }
    reserve(
        target,
        {
            "owner": OWNER,
            "role": "bootstrap-export",
            "operation_id": request["operation_id"],
            "request_digest": state["request_digest"],
        },
    )
    allowed = {*NAMES, "owner.json", "receipt.json"}
    if any(path.name not in allowed for path in target.iterdir()):
        raise ValueError("SPIRE_BOOTSTRAP_EXPORT_DRIFT")
    if (target / "receipt.json").exists() and read(target / "receipt.json") != receipt:
        raise ValueError("SPIRE_BOOTSTRAP_EXPORT_DRIFT")
    for name, value in files.items():
        path = target / name
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or path.read_bytes() != value
            ):
                raise ValueError("SPIRE_BOOTSTRAP_EXPORT_DRIFT")
        else:
            atomic(path, value)
    # Commit last. A partial copy is not an export and can resume only exact bytes.
    atomic(target / "receipt.json", receipt)
    if time.time() >= expires:
        raise ValueError("SPIRE_OPERATOR_INITIAL_AUTHORIZATION_EXPIRED")
    return {
        key: receipt[key]
        for key in (
            "owner",
            "schema_version",
            "operation_id",
            "node_id",
            "resource_owner",
            "request_digest",
            "expires_at",
        )
    } | {"phase": "EXPORTED"}
