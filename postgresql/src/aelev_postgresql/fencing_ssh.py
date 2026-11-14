"""SSH transport to a fixed, operator-owned PostgreSQL node operation."""

from __future__ import annotations

import json
import logging
import os
import re
import selectors
import subprocess
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from aelev_postgresql.configuration import setting
from aelev_postgresql.fencing import Claim, FencingError, NODES, timeout
from aelev_postgresql.fencing_node import COMMAND


def exchange(command: list[str], body: bytes) -> bytes:
    """Bound time and both output streams even for an uncooperative SSH peer."""
    if len(body) > 4096:
        raise FencingError("Oversized SSH request", "SSH_OPERATION_UNCONFIRMED")
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "LANG": "C"},
    )
    try:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise FencingError("SSH pipes unavailable", "SSH_OPERATION_UNCONFIRMED")
        process.stdin.write(body)
        process.stdin.close()
        outputs = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + timeout()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FencingError(
                        "Remote operation timed out", "SSH_OPERATION_UNCONFIRMED"
                    )
                for key, _ in selector.select(remaining):
                    data = os.read(key.fd, 4096)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    outputs[key.data].extend(data)
                    if len(outputs[key.data]) > 16384:
                        raise FencingError(
                            "Remote output exceeded bound", "SSH_OPERATION_UNCONFIRMED"
                        )
        if process.wait(timeout=max(0.001, deadline - time.monotonic())):
            raise FencingError("Remote operation failed", "SSH_OPERATION_UNCONFIRMED")
        return bytes(outputs["stdout"])
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FencingError(
            "Remote operation not confirmed", "SSH_OPERATION_UNCONFIRMED"
        ) from error
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()


def private_file(value: Any, *, secret: bool) -> str:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise FencingError("Absolute SSH input required", "SSH_BINDING_INVALID")
    path = Path(value)
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_mode & (0o077 if secret else 0o022)
    ):
        raise FencingError("Protected SSH input required", "SSH_BINDING_INVALID")
    return value


class SSHDocker:
    """No local socket or intent store; every replica uses target-owned state."""

    def __init__(self) -> None:
        filename = private_file(setting("FENCING_DOCKER_SSH_BINDINGS"), secret=True)
        raw = Path(filename).read_bytes()
        if len(raw) > 16384:
            raise FencingError("Oversized SSH configuration", "SSH_BINDING_INVALID")
        value = json.loads(raw)
        if not isinstance(value, dict) or not value or set(value) - NODES:
            raise FencingError("Invalid SSH nodes", "SSH_BINDING_INVALID")
        for binding in value.values():
            if (
                not isinstance(binding, dict)
                or set(binding)
                != {
                    "host",
                    "port",
                    "user",
                    "identity_file",
                    "known_hosts_file",
                    "daemon_id",
                }
                or not isinstance(binding["host"], str)
                or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", binding["host"]
                )
                or not isinstance(binding["user"], str)
                or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,63}", binding["user"])
                or type(binding["port"]) is not int
                or not 1 <= binding["port"] <= 65535
                or not isinstance(binding["daemon_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9:_-]{8,128}", binding["daemon_id"])
            ):
                raise FencingError("Invalid SSH destination", "SSH_BINDING_INVALID")
            private_file(binding["identity_file"], secret=True)
            private_file(binding["known_hosts_file"], secret=False)
        self.bindings: dict[str, Any] = value

    def call(
        self, node: str, action: str, claim: Claim | None = None
    ) -> dict[str, Any]:
        if node not in self.bindings:
            raise FencingError("Unconfigured remote node", "SSH_NODE_NOT_CONFIGURED")
        binding = self.bindings[node]
        request: dict[str, Any] = {
            "schema": 1,
            "cluster": setting("CLUSTER"),
            "node": node,
            "daemon_id": binding["daemon_id"],
            "action": action,
            "request_id": str(uuid.uuid4()),
        }
        if claim is not None:
            request["claim"] = asdict(claim)
        options = [
            "BatchMode=yes",
            "IdentitiesOnly=yes",
            "StrictHostKeyChecking=yes",
            "GlobalKnownHostsFile=/dev/null",
            "IdentityAgent=none",
            "ForwardAgent=no",
            "ForwardX11=no",
            "ClearAllForwardings=yes",
            "PermitLocalCommand=no",
            "RequestTTY=no",
            "PasswordAuthentication=no",
            "KbdInteractiveAuthentication=no",
            "PreferredAuthentications=publickey",
            "ControlMaster=no",
            "ControlPath=none",
            "ProxyCommand=none",
            "ProxyJump=none",
            "ConnectionAttempts=1",
            "ConnectTimeout=5",
            f"UserKnownHostsFile={binding['known_hosts_file']}",
        ]
        command = ["/usr/bin/ssh", "-F", "/dev/null", "-T"]
        for option in options:
            command.extend(["-o", option])
        command.extend(
            [
                "-i",
                binding["identity_file"],
                "-p",
                str(binding["port"]),
                "-l",
                binding["user"],
                "--",
                binding["host"],
                COMMAND,
            ]
        )
        # Never forward SSH stderr, host paths, key contents or untrusted text.
        response = json.loads(exchange(command, json.dumps(request).encode()))
        if (
            not isinstance(response, dict)
            or response.get("ok") is not True
            or response.get("request") != request
        ):
            raise FencingError("Remote receipt mismatch", "SSH_RECEIPT_INVALID")
        return response

    def fence(self, node: str, claim: Claim, record: Any) -> list[str]:
        claim.verify()
        response = self.call(node, "fence", claim)
        identities = response.get("identities")
        if (
            not isinstance(identities, list)
            or len(identities) != 1
            or not isinstance(identities[0], str)
            or not re.fullmatch(r"[a-f0-9]{64}", identities[0])
        ):
            raise FencingError("Remote termination unconfirmed", "SSH_RECEIPT_INVALID")
        claim.verify()
        record("remote_confirmed", identities)
        return identities


class SSHReturns:
    """Invoke existing target return guards; no replica-local retry budget."""

    def __init__(self, backend: SSHDocker) -> None:
        self.backend = backend
        self.stop = threading.Event()
        self.verified: dict[str, float] = {}
        self.lock = threading.Lock()

    def healthy(self) -> bool:
        """A live thread alone does not prove reachable, admitted node operations."""
        freshness = max(15.0, len(self.backend.bindings) * timeout() + 10)
        now = time.monotonic()
        with self.lock:
            return all(
                now - self.verified.get(node, float("-inf")) <= freshness
                for node in self.backend.bindings
            )

    def once(self) -> None:
        for node in sorted(self.backend.bindings):
            try:
                response = self.backend.call(node, "return-once")
                if type(response.get("dispatched")) is not bool:
                    raise FencingError("Invalid return receipt", "SSH_RECEIPT_INVALID")
                with self.lock:
                    self.verified[node] = time.monotonic()
                if response["dispatched"]:
                    return
            except (OSError, ValueError, KeyError, TypeError):
                with self.lock:
                    self.verified.pop(node, None)
                logging.error(
                    "fencing_return node=%s reason_code=SSH_RETURN_UNCONFIRMED", node
                )

    def run(self) -> None:
        while not self.stop.is_set():
            self.once()
            self.stop.wait(5)
