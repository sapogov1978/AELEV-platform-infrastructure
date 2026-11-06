"""Audited, exact registration intents; retries never resurrect deleted entries."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import subprocess
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from .files import atomic, read, reserve, writer
from .journal import append
from .settings import AGENT, OWNER, SERVER, SERVER_SOCKET, domain, environment, identity


class EntryNotFound(ValueError):
    pass


def native(*arguments: str) -> bytes:
    result = subprocess.run(
        [SERVER, *arguments, "-socketPath", SERVER_SOCKET],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode and arguments[:2] in {("entry", "show"), ("entry", "delete")}:
        try:
            error = json.loads(result.stdout).get("error")
        except (ValueError, AttributeError):
            error = None
        if isinstance(error, str) and re.search(
            r"(?:^|: )rpc error: code = NotFound desc = ", error
        ):
            raise EntryNotFound("SPIRE_OPERATOR_ENTRY_NOT_FOUND")
    if result.returncode or len(result.stdout) > 1024**2:
        raise ValueError("SPIRE_OPERATOR_NATIVE_REQUEST_FAILED")
    return result.stdout


def entries(entry_id: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(
            native("entry", "show", "-entryID", entry_id, "-output", "json")
        )
    except EntryNotFound:
        return []
    result: Any = (
        cast(dict[str, Any], value).get("entries") if isinstance(value, dict) else None
    )
    if not isinstance(result, list) or any(
        not isinstance(entry, dict) for entry in cast(list[Any], result)
    ):
        raise ValueError("SPIRE_OPERATOR_ENTRY_RESPONSE_INVALID")
    return cast(list[dict[str, Any]], result)


def validate(request: dict[str, Any]) -> tuple[str, str, int]:
    mode, trust, ttl = environment(request)
    owner = request.get("resource_owner")
    if (
        request.get("owner") != OWNER
        or request.get("schema_version") != 1
        or not isinstance(owner, str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", owner)
    ):
        raise ValueError("SPIRE_OPERATOR_RESOURCE_OWNER_INVALID")
    for field in ("operation_id", "node_id"):
        if str(uuid.UUID(request[field])) != request[field]:
            raise ValueError("SPIRE_OPERATOR_ID_INVALID")
    workload = identity(request.get("spiffe_id"), trust)
    if not workload.startswith("spiffe://" + trust + "/" + owner + "/"):
        raise ValueError("SPIRE_OPERATOR_SCOPE_INVALID")
    names = request.get("dns_names", [])
    if (
        not isinstance(names, list)
        or len(cast(list[Any], names)) > 4
        or any(
            not isinstance(name, str) or domain(name) != name
            for name in cast(list[Any], names)
        )
    ):
        raise ValueError("SPIRE_OPERATOR_DNS_INVALID")
    if not isinstance(request.get("actor"), str) or not re.fullmatch(
        r"[A-Za-z0-9_.:@-]{1,128}", request["actor"]
    ):
        raise ValueError("SPIRE_OPERATOR_ACTOR_REQUIRED")
    if not isinstance(request.get("server_address"), str) or not re.fullmatch(
        r"[A-Za-z0-9.:-]{1,253}", request["server_address"]
    ):
        raise ValueError("SPIRE_OPERATOR_SERVER_REQUIRED")
    port = request.get("server_port", 8081)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("SPIRE_OPERATOR_SERVER_REQUIRED")
    allowed = {
        "owner",
        "schema_version",
        "resource_owner",
        "operation_id",
        "node_id",
        "spiffe_id",
        "dns_names",
        "actor",
        "server_address",
        "server_port",
        "environment",
        "trust_domain",
        "svid_ttl_seconds",
    }
    if set(request) - allowed:
        raise ValueError("SPIRE_OPERATOR_UNDECLARED_INPUT")
    return mode, trust, ttl


def check_entry(
    entry: dict[str, Any], request: dict[str, Any], parent: str, ttl: int
) -> None:
    # Exact SPIRE 1.15 CLI proto-JSON (API SDK Entry, not legacy common.Entry).
    def spiffe(value: Any) -> str:
        if not isinstance(value, dict):
            raise ValueError("SPIRE_OPERATOR_ENTRY_IDENTITY_INVALID")
        value = cast(dict[str, Any], value)
        if set(value) != {"trust_domain", "path"}:
            raise ValueError("SPIRE_OPERATOR_ENTRY_IDENTITY_INVALID")
        return identity(
            "spiffe://" + value["trust_domain"] + value["path"], request["trust_domain"]
        )

    selectors = sorted(
        (item["type"], item["value"]) for item in entry.get("selectors", [])
    )
    expected = [("unix", "path:" + AGENT), ("unix", "uid:10001")]
    if (
        spiffe(entry.get("spiffe_id")) != request["spiffe_id"]
        or spiffe(entry.get("parent_id")) != parent
        or entry.get("id") != request["operation_id"]
        or selectors != expected
        or entry.get("admin", False)
        or entry.get("downstream", False)
        or entry.get("x509_svid_ttl") != ttl
        or entry.get("federates_with", [])
        or entry.get("store_svid", False)
        or sorted(entry.get("dns_names", [])) != sorted(request.get("dns_names", []))
    ):
        raise ValueError("SPIRE_OPERATOR_EXISTING_SCOPE_MISMATCH")


def operate(
    action: str,
    request_path: Path,
    root: Path,
    output: Path,
    *,
    export_path: Path | None = None,
) -> dict[str, Any]:
    if action not in {"authorize", "revoke", "inspect", "export-bootstrap"}:
        raise ValueError("SPIRE_OPERATOR_ACTION_INVALID")
    request = read(request_path)
    mode, trust, ttl = validate(request)
    deployment = read(Path(SERVER_SOCKET).parent / "deployment.json")
    if deployment != {"owner": OWNER, "environment": mode, "trust_domain": trust}:
        raise ValueError("SPIRE_OPERATOR_SERVER_SCOPE_MISMATCH")
    reserve(
        root,
        {
            "owner": OWNER,
            "role": "operator",
            "environment": mode,
            "trust_domain": trust,
        },
    )
    lock_path = root / "operator.lock"
    if lock_path.is_symlink():
        raise ValueError("SPIRE_OPERATOR_LOCK_INVALID")
    with writer(root), lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return locked(action, request, root, output, ttl, export_path=export_path)


def locked(
    action: str,
    request: dict[str, Any],
    root: Path,
    output: Path,
    ttl: int,
    *,
    export_path: Path | None = None,
) -> dict[str, Any]:
    operation = request["operation_id"]
    state: dict[str, Any]
    path = root / "operations" / (operation + ".json")
    digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    if path.exists():
        state = read(path)
        if state.get("request_digest") != digest:
            raise ValueError("SPIRE_OPERATOR_INTENT_COLLISION")
    else:
        if action != "authorize":
            raise ValueError("SPIRE_OPERATOR_INTENT_NOT_FOUND")
        state = {
            "owner": OWNER,
            "request_digest": digest,
            "request": request,
            "phase": "INTENT",
            "created_at": time.time(),
        }
        atomic(path, state)
    parent = f"spiffe://{request['trust_domain']}/node/{request['resource_owner']}/{request['node_id']}"

    def save(phase: str, **extra: Any) -> None:
        state.update(phase=phase, updated_at=time.time(), **extra)
        atomic(path, state)
        append(
            root,
            {
                "owner": OWNER,
                "operation_id": operation,
                "resource_owner": request["resource_owner"],
                "actor": request["actor"],
                "phase": phase,
                "request_digest": digest,
                "recorded_at": time.time(),
            },
        )

    if action == "inspect":
        return {key: state[key] for key in ("owner", "phase", "request_digest")}
    if state["phase"] == "REVOKED":
        if action == "revoke":
            return {"owner": OWNER, "phase": "REVOKED"}
        raise ValueError("SPIRE_OPERATOR_REVOKED")
    if action == "export-bootstrap":
        # Export is read-only with respect to registration. In particular it must
        # never fall through into token issue, entry creation or revoke recovery.
        if state["phase"] != "DONE" or export_path is None:
            raise ValueError("SPIRE_BOOTSTRAP_EXPORT_AUTHORIZATION_NOT_COMPLETE")
        found = entries(operation)
        if len(found) != 1:
            raise ValueError("SPIRE_OPERATOR_REVOKED")
        check_entry(found[0], request, parent, ttl)
        from .bootstrap_export import export

        return export(
            request,
            state,
            output,
            export_path,
            native("bundle", "show", "-format", "pem"),
        )
    if action == "revoke" or state["phase"] == "REVOKING":
        save("REVOKING")
        if entries(operation):
            with suppress(EntryNotFound):
                native("entry", "delete", "-entryID", operation, "-output", "json")
        save("REVOKED")
        if action != "revoke":
            raise ValueError("SPIRE_OPERATOR_REVOKED")
        return {"owner": OWNER, "phase": "REVOKED"}
    if state["phase"] == "INTENT":
        # Persist before crossing the native boundary. An ambiguous token issue
        # is never retried with a fresh right on restart.
        save("ISSUING_TOKEN")
        token = json.loads(
            native(
                "token",
                "generate",
                "-spiffeID",
                parent,
                "-ttl",
                "600",
                "-output",
                "json",
            )
        )
        value = token.get("value")
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9-]{32,128}", value):
            raise ValueError("SPIRE_OPERATOR_TOKEN_RESPONSE_INVALID")
        save("TOKEN_ISSUED", token=value, token_expires_at=time.time() + 600)
    if state["phase"] == "ISSUING_TOKEN":
        raise ValueError("SPIRE_OPERATOR_AMBIGUOUS_TOKEN_ISSUE")
    if state["phase"] == "TOKEN_ISSUED":
        save("CREATING_ENTRY")
        arguments = [
            "entry",
            "create",
            "-entryID",
            operation,
            "-parentID",
            parent,
            "-spiffeID",
            request["spiffe_id"],
            "-selector",
            "unix:uid:10001",
            "-selector",
            "unix:path:" + AGENT,
            "-x509SVIDTTL",
            str(ttl),
            "-output",
            "json",
        ]
        for name in request.get("dns_names", []):
            arguments += ["-dns", name]
        native(*arguments)
        save("ENTRY_CREATED")
    found = entries(operation)
    if len(found) != 1:
        if state["phase"] == "DONE":
            save("REVOKED", reason="SPIRE_REGISTRATION_REMOVED")
            raise ValueError("SPIRE_OPERATOR_REVOKED")
        raise ValueError("SPIRE_OPERATOR_AMBIGUOUS_ENTRY_CREATION")
    check_entry(found[0], request, parent, ttl)
    if time.time() >= state["token_expires_at"] and not (
        state["phase"] == "DONE" and (output / "plan.json").is_file()
    ):
        raise ValueError("SPIRE_OPERATOR_INITIAL_AUTHORIZATION_EXPIRED")
    reserve(
        output, {"owner": OWNER, "role": "agent-bootstrap", "operation_id": operation}
    )
    bundle = native("bundle", "show", "-format", "pem")
    atomic(output / "bundle.crt", bundle, 0o644)
    atomic(output / "join-token", state["token"].encode())
    atomic(
        output / "plan.json",
        {
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
            )
            if key in request
        }
        | {"server_port": request.get("server_port", 8081)},
    )
    save("DONE")
    return {"owner": OWNER, "phase": "DONE", "request_digest": digest}
