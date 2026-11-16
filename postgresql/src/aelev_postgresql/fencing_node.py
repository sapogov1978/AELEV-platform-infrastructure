"""One-shot node operation; canonical intent remains beside its Docker Engine.

Invoke only through the operator-owned forced SSH command. Requests cannot
select a socket, image, environment, state directory, command or DCS endpoint.
"""

from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from aelev_postgresql import fencing_intent
from aelev_postgresql.configuration import setting
from aelev_postgresql.fencing import Claim, Docker, FencingError, NODES
from aelev_postgresql.fencing_return import Returns

LIMIT = 4096
COMMAND = "aelev-postgresql-fencing-node-v1"


def binding() -> dict[str, Any]:
    """Bind one explicitly allowed node, cluster and daemon, not an SSH caller."""
    node = setting("FENCING_DOCKER_LOCAL_NODES")
    daemon = setting("FENCING_DOCKER_DAEMON_ID")
    if node not in NODES or not re.fullmatch(r"[A-Za-z0-9:_-]{8,128}", daemon):
        raise FencingError("Invalid node operation binding", "NODE_BINDING_INVALID")
    return {
        "schema": 1,
        "cluster": setting("CLUSTER"),
        "node": node,
        "daemon_id": daemon,
    }


@contextmanager
def exclusive() -> Iterator[None]:
    """Serialize all callers and replicas at the canonical target ledger."""
    root = fencing_intent.ROOT
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_mode & 0o077:
        raise FencingError("Private node ledger required", "NODE_STATE_INVALID")
    descriptor = os.open(
        root / "node-operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def check_daemon(backend: Docker, expected: dict[str, Any]) -> None:
    if backend.call("GET", "/info").get("ID") != expected["daemon_id"]:
        raise FencingError("Docker daemon changed", "NODE_DAEMON_CHANGED")


def initialize(backend: Docker) -> None:
    """Explicit fresh installation only; never adopt or reset an old budget."""
    expected = binding()
    root = fencing_intent.ROOT
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_symlink() and not any(root.iterdir()):
        # Explicit initialization may prepare a still-empty Docker volume.
        root.chmod(0o700)
    with exclusive():
        check_daemon(backend, expected)
        marker = fencing_intent.ROOT / "node-binding.json"
        if marker.exists():
            verify_binding(expected)
            return
        if set(p.name for p in fencing_intent.ROOT.iterdir()) != {
            "node-operation.lock"
        }:
            raise FencingError(
                "Existing fencing history requires handoff",
                "NODE_HISTORY_HANDOFF_REQUIRED",
            )
        descriptor = os.open(
            marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(expected))
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(fencing_intent.ROOT, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def verify_binding(expected: dict[str, Any]) -> None:
    try:
        descriptor = os.open(
            fencing_intent.ROOT / "node-binding.json", os.O_RDONLY | os.O_NOFOLLOW
        )
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT or json.loads(raw) != expected:
            raise ValueError("Binding mismatch")
    except (OSError, ValueError) as error:
        raise FencingError(
            "Node ledger is not admitted", "NODE_STATE_NOT_ADMITTED"
        ) from error


def validate(request: Any, expected: dict[str, Any]) -> None:
    base = {*expected, "action", "request_id"}
    if not isinstance(request, dict) or request.get("action") not in {
        "status",
        "fence",
        "return-once",
    }:
        raise FencingError("Invalid node request", "NODE_REQUEST_INVALID")
    keys = base | ({"claim"} if request["action"] == "fence" else set())
    if set(request) != keys or any(
        type(request[k]) is not type(v) or request[k] != v for k, v in expected.items()
    ):
        raise FencingError("Node request binding mismatch", "NODE_REQUEST_INVALID")
    if (
        not isinstance(request["request_id"], str)
        or str(uuid.UUID(request["request_id"])) != request["request_id"]
    ):
        raise FencingError("Invalid request identity", "NODE_REQUEST_INVALID")
    if request["action"] == "fence":
        claim = request["claim"]
        if (
            not isinstance(claim, dict)
            or set(claim) != {"candidate", "lease", "revision"}
            or claim["candidate"] not in NODES - {expected["node"]}
            or any(
                not isinstance(claim[k], str)
                or not re.fullmatch(r"[1-9][0-9]{0,19}", claim[k])
                for k in ("lease", "revision")
            )
        ):
            raise FencingError("Invalid native claim", "NODE_REQUEST_INVALID")


def execute(request: Any, backend: Docker) -> dict[str, Any]:
    """Re-use existing lease/termination/return guards inside the target node."""
    expected = binding()
    validate(request, expected)
    with exclusive():
        verify_binding(expected)
        check_daemon(backend, expected)

        def record(
            phase: str,
            identities: list[str],
            reason_code: str | None = None,
            **extra: Any,
        ) -> None:
            row = {
                **request,
                "phase": phase,
                "identities": identities,
                "recorded_at": time.time(),
                **extra,
            }
            if reason_code is not None:
                row["reason_code"] = reason_code
            descriptor = os.open(
                fencing_intent.ROOT / "node-audit.jsonl",
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "w") as stream:
                stream.write(json.dumps(row) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            directory = os.open(fencing_intent.ROOT, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

        result: dict[str, Any] = {"request": request, "ok": True}
        if request["action"] == "fence":
            claim = Claim(**request["claim"])
            claim.verify()
            try:
                result["identities"] = backend.fence(expected["node"], claim, record)
                claim.verify()
                record("confirmed", result["identities"])
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                http.client.HTTPException,
            ) as error:
                record(
                    "failed", [], getattr(error, "code", "NODE_OPERATION_UNCONFIRMED")
                )
                raise
        elif request["action"] == "return-once":

            def return_record(
                node: str,
                intent: dict[str, Any],
                phase: str,
                identities: list[str],
                reason_code: str | None,
            ) -> None:
                record(
                    phase,
                    identities,
                    reason_code,
                    start_attempts=intent["start_attempts"],
                )

            worker = Returns(
                backend, {node: threading.Lock() for node in NODES}, return_record
            )
            worker.once()
            result["dispatched"] = worker.dispatched
        # Status and return receipts do not attest to PostgreSQL/primary health.
        store = fencing_intent.IntentStore(expected["node"])
        with store.locked():
            result["intent"] = store.load()
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["initialize", "execute"])
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.operation == "initialize":
            initialize(Docker())
            return
        if os.environ.get("SSH_ORIGINAL_COMMAND") != COMMAND:
            raise FencingError("Forced node command required", "NODE_COMMAND_DENIED")
        raw = sys.stdin.buffer.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise FencingError("Node request is too large", "NODE_REQUEST_INVALID")
        result = execute(json.loads(raw), Docker())
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        http.client.HTTPException,
    ) as error:
        result = {
            "ok": False,
            "reason_code": getattr(error, "code", "NODE_OPERATION_UNCONFIRMED"),
        }
    print(json.dumps(result))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
