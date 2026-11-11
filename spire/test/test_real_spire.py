"""Opt-in real SPIRE lifecycle on isolated Docker resources, never the stand."""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from typing import Any, cast

import pytest

IMAGE = os.environ.get("AELEV_INFRASTRUCTURE_SPIRE_TEST_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="Explicit isolated SPIRE image required"
)


class Sandbox:
    def __init__(self) -> None:
        self.id = str(uuid.uuid4())
        self.prefix = "spire-test-" + self.id[:12]
        self.containers: list[str] = []
        self.volumes: list[str] = []
        self.network: str | None = None

    def private_network(self) -> str:
        if self.network is not None:
            raise RuntimeError("SPIRE_TEST_NETWORK_ALREADY_CREATED")
        identifiers = self.docker("network", "ls", "-q").decode().splitlines()
        rows: list[dict[str, Any]] = (
            json.loads(self.docker("network", "inspect", *identifiers))
            if identifiers
            else []
        )
        used: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for row in rows:
            ipam = cast(dict[str, Any], row.get("IPAM") or {})
            configurations = cast(list[dict[str, Any]], ipam.get("Config") or [])
            for configuration in configurations:
                subnet = configuration.get("Subnet")
                if subnet:
                    used.append(ipaddress.ip_network(subnet, strict=False))
        candidate = next(
            (
                subnet
                for parent in ("10.254.0.0/16", "10.253.0.0/16")
                for subnet in ipaddress.ip_network(parent).subnets(new_prefix=24)
                if not any(subnet.overlaps(existing) for existing in used)
            ),
            None,
        )
        if candidate is None:
            raise RuntimeError("SPIRE_TEST_PRIVATE_SUBNET_UNAVAILABLE")
        self.network = self.prefix + "-private"
        self.docker(
            "network",
            "create",
            "--internal",
            "--subnet",
            str(candidate),
            "--label",
            "io.aelev.spire-test=" + self.id,
            self.network,
        )
        return self.network

    def docker(
        self, *arguments: str, payload: bytes | None = None, check: bool = True
    ) -> bytes:
        helper: str | None = None
        if arguments and arguments[0] == "run":
            if "--name" in arguments:
                helper = arguments[arguments.index("--name") + 1]
            else:
                helper = self.prefix + "-helper-" + uuid.uuid4().hex[:8]
                arguments = (
                    "run",
                    "--name",
                    helper,
                    "--label",
                    "io.aelev.spire-test=" + self.id,
                    *arguments[1:],
                )
            if not helper.startswith(self.prefix + "-"):
                raise ValueError("SPIRE_TEST_FOREIGN_CONTAINER_REFUSED")
            self.containers.append(helper)
        result = subprocess.run(
            ["docker", *arguments],
            input=payload,
            capture_output=True,
            timeout=180,
            check=False,
        )
        if helper is not None and result.returncode == 0 and "--rm" in arguments:
            self.containers.remove(helper)
        if check and result.returncode:
            # Native command output can contain initial credentials. Only safe
            # wrapper codes may be attached to a test failure.
            safe = result.stdout.decode(errors="replace").strip()
            if not safe.startswith('{"code": "SPIRE_'):
                safe = "SPIRE_TEST_DOCKER_COMMAND_FAILED"
            raise RuntimeError(safe[:200])
        return result.stdout

    def volume(self, name: str, *, ephemeral: bool = False) -> str:
        name = self.prefix + "-" + name
        found = self.docker("volume", "ls", "--format", "{{.Name}}")
        if name in found.decode().splitlines():
            raise RuntimeError("SPIRE_TEST_EXISTING_VOLUME_REFUSED")
        command = ["volume", "create", "--label", "io.aelev.spire-test=" + self.id]
        if ephemeral:
            command += [
                "--driver",
                "local",
                "--opt",
                "type=tmpfs",
                "--opt",
                "device=tmpfs",
                "--opt",
                "o=size=2m,uid=10001,gid=10001,mode=0700",
            ]
        self.docker(*command, name)
        self.volumes.append(name)
        return name

    def seed(self, volume: str, name: str, value: dict[str, Any]) -> None:
        self.docker(
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--memory",
            "128m",
            "--entrypoint",
            "python",
            "-v",
            volume + ":/var/lib/spire",
            IMAGE,
            "-c",
            "import sys,json; from pathlib import Path; "
            "from aelev_spire.files import atomic; "
            "atomic(Path('/var/lib/spire') / sys.argv[1], json.load(sys.stdin))",
            name,
            payload=json.dumps(value).encode(),
        )

    def start(
        self, name: str, role: str, state: str, bootstrap: str, extra: list[str]
    ) -> str:
        name = self.prefix + "-" + name
        self.docker(
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            "io.aelev.spire-test=" + self.id,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "384m",
            "--pids-limit",
            "128",
            "--cpus",
            "0.5",
            "--network",
            str(self.network),
            "--tmpfs",
            "/run/spire:size=8m,uid=10001,gid=10001,mode=0700",
            "--tmpfs",
            "/tmp:size=16m,mode=1777",
            "-v",
            state + ":/var/lib/spire",
            "-v",
            bootstrap + ":/run/bootstrap:ro",
            *extra,
            IMAGE,
            role,
        )
        return name

    def public_state(
        self, container: str, *, heartbeat: bool = False
    ) -> dict[str, Any]:
        path = "/run/spire/heartbeat.json" if heartbeat else "/run/svid/current.json"
        raw = self.docker(
            "exec",
            container,
            "python",
            "-S",
            "-c",
            "import sys,json; from pathlib import Path; p=Path(sys.argv[1]); "
            "print(p.read_text() if p.is_file() else '{}')",
            path,
            check=False,
        )
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}

    def cleanup(self) -> None:
        for name in reversed(self.containers):
            label = (
                self.docker(
                    "inspect",
                    "--format",
                    '{{index .Config.Labels "io.aelev.spire-test"}}',
                    name,
                    check=False,
                )
                .decode()
                .strip()
            )
            if label == self.id:
                self.docker("rm", "--force", name)
        for name in reversed(self.volumes):
            label = (
                self.docker(
                    "volume",
                    "inspect",
                    "--format",
                    '{{index .Labels "io.aelev.spire-test"}}',
                    name,
                    check=False,
                )
                .decode()
                .strip()
            )
            if label == self.id:
                self.docker("volume", "rm", name)
        if self.network is not None:
            label = (
                self.docker(
                    "network",
                    "inspect",
                    "--format",
                    '{{index .Labels "io.aelev.spire-test"}}',
                    self.network,
                    check=False,
                )
                .decode()
                .strip()
            )
            if label == self.id:
                self.docker("network", "rm", self.network)


def wait(predicate: Callable[[], bool], code: str, seconds: int = 90) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(2)
    raise AssertionError(code)


def test_real_issue_rotation_revoke_and_node_restart() -> None:
    sandbox = Sandbox()
    try:
        sandbox.private_network()
        configuration = sandbox.volume("configuration")
        admin = sandbox.volume("admin", ephemeral=True)
        operator_state = sandbox.volume("operator")
        sandbox.seed(
            configuration,
            "plan.json",
            {
                "owner": "infrastructure-spire",
                "schema_version": 1,
                "environment": "test",
                "trust_domain": "test.example",
                "svid_ttl_seconds": 20,
                "agent_ttl_seconds": 3600,
            },
        )
        server_state = sandbox.volume("server")
        server = sandbox.start(
            "server",
            "server",
            server_state,
            configuration,
            ["--network-alias", "spire-server", "-v", admin + ":/run/spire/admin"],
        )
        wait(
            lambda: sandbox.public_state(server, heartbeat=True).get("ready") is True,
            "SPIRE_TEST_SERVER_NOT_READY",
        )
        agents: list[str] = []
        requests: list[str] = []
        outputs: list[str] = []
        agent_states: list[str] = []

        def operate(
            action: str, request_volume: str, output_volume: str, *, check: bool = True
        ) -> bytes:
            return sandbox.docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--memory",
                "128m",
                "--pids-limit",
                "64",
                "-v",
                admin + ":/run/spire/admin",
                "-v",
                operator_state + ":/var/lib/spire",
                "-v",
                request_volume + ":/run/request:ro",
                "-v",
                output_volume + ":/run/svid",
                IMAGE,
                action,
                "--plan",
                "/run/request/request.json",
                check=check,
            )

        for index in range(2):
            request_volume = sandbox.volume("request-" + str(index))
            output_volume = sandbox.volume("bootstrap-" + str(index))
            request = {
                "owner": "infrastructure-spire",
                "schema_version": 1,
                "environment": "test",
                "trust_domain": "test.example",
                "svid_ttl_seconds": 20,
                "resource_owner": "fixture-owner",
                "operation_id": str(uuid.uuid4()),
                "node_id": str(uuid.uuid4()),
                "spiffe_id": f"spiffe://test.example/fixture-owner/component/instance-{index}",
                "dns_names": ["localhost"],
                "actor": "isolated-test",
                "server_address": "spire-server",
            }
            sandbox.seed(request_volume, "request.json", request)
            operate("authorize", request_volume, output_volume)
            operate("authorize", request_volume, output_volume)
            svid = sandbox.volume("svid-" + str(index), ephemeral=True)
            agent_state = sandbox.volume("agent-" + str(index))
            agent_states.append(agent_state)
            agent = sandbox.start(
                "agent-" + str(index),
                "agent",
                agent_state,
                output_volume,
                ["-v", svid + ":/run/svid"],
            )
            agents.append(agent)
            requests.append(request_volume)
            outputs.append(output_volume)
        for agent in agents:
            wait(
                lambda agent=agent: sandbox.public_state(agent).get("state") == "READY",
                "SPIRE_TEST_SVID_NOT_READY",
            )
        first = [sandbox.public_state(agent) for agent in agents]
        assert first[0]["spiffe_id"] != first[1]["spiffe_id"]
        wait(
            lambda: all(
                sandbox.public_state(agent).get("serial")
                not in {None, original["serial"]}
                for agent, original in zip(agents, first, strict=True)
            ),
            "SPIRE_TEST_SVID_DID_NOT_ROTATE",
        )
        operate("revoke", requests[0], outputs[0])
        wait(
            lambda: sandbox.public_state(agents[0]).get("state") == "UNAVAILABLE",
            "SPIRE_TEST_REVOKE_NOT_PROPAGATED",
        )
        rejected = operate("authorize", requests[0], outputs[0], check=False)
        assert b"SPIRE_OPERATOR_REVOKED" in rejected
        assert sandbox.public_state(agents[1])["state"] == "READY"
        sandbox.docker("restart", "--time", "10", agents[1])
        wait(
            lambda: sandbox.public_state(agents[1]).get("state") == "READY",
            "SPIRE_TEST_NODE_STATE_NOT_RECOVERED",
        )
        assert sandbox.public_state(agents[1])["spiffe_id"] == first[1]["spiffe_id"]
        # Offline encrypted full-state recovery. No running writer is copied,
        # and both native registration state and the operator's terminal
        # revocation are restored, not recreated by authorizing again.
        for container in [*agents, server]:
            sandbox.docker("stop", "--time", "10", container)
        backup = sandbox.volume("backup")
        sandbox.docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            "-v",
            backup + ":/var/lib/spire",
            IMAGE,
            "-c",
            "from pathlib import Path; import os; "
            "from aelev_spire.files import atomic; "
            "atomic(Path('/var/lib/spire/backup.key'), os.urandom(32))",
        )
        sandbox.docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--memory",
            "128m",
            "--entrypoint",
            "python",
            "-v",
            server_state + ":/recovery/server",
            "-v",
            operator_state + ":/recovery/operator",
            "-v",
            agent_states[1] + ":/recovery/agent",
            "-v",
            backup + ":/var/lib/spire",
            IMAGE,
            "-m",
            "aelev_spire.backup",
            "create",
            "--role",
            "server",
            "--role",
            "operator",
            "--role",
            "agent",
            "--archive",
            "/var/lib/spire/snapshot.enc",
            "--key",
            "/var/lib/spire/backup.key",
        )
        restored = {
            role: sandbox.volume("restored-" + role)
            for role in ("server", "operator", "agent")
        }
        mounts = [
            argument
            for role, volume in restored.items()
            for argument in ("-v", volume + ":/recovery/" + role)
        ]
        sandbox.docker(
            "run",
            "--rm",
            "--user",
            "0:0",
            "--network",
            "none",
            "--entrypoint",
            "python",
            *mounts,
            IMAGE,
            "-S",
            "-c",
            "import os; from pathlib import Path; "
            "[os.chown(p,10001,10001) for p in Path('/recovery').iterdir()]",
        )
        sandbox.docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--memory",
            "128m",
            "--entrypoint",
            "python",
            *mounts,
            "-v",
            backup + ":/var/lib/spire:ro",
            IMAGE,
            "-m",
            "aelev_spire.backup",
            "restore",
            "--role",
            "server",
            "--role",
            "operator",
            "--role",
            "agent",
            "--archive",
            "/var/lib/spire/snapshot.enc",
            "--key",
            "/var/lib/spire/backup.key",
            "--trusted-receipt",
            "/var/lib/spire/snapshot.enc.receipt.json",
        )
        server = sandbox.start(
            "restored-server",
            "server",
            restored["server"],
            configuration,
            ["--network-alias", "spire-server", "-v", admin + ":/run/spire/admin"],
        )
        wait(
            lambda: sandbox.public_state(server, heartbeat=True).get("ready") is True,
            "SPIRE_TEST_RESTORED_SERVER_NOT_READY",
        )
        operator_state = restored["operator"]
        assert b"SPIRE_OPERATOR_REVOKED" in operate(
            "authorize", requests[0], outputs[0], check=False
        )
        restored_agent = sandbox.start(
            "restored-agent",
            "agent",
            restored["agent"],
            outputs[1],
            ["-v", sandbox.volume("restored-svid", ephemeral=True) + ":/run/svid"],
        )
        wait(
            lambda: sandbox.public_state(restored_agent).get("state") == "READY",
            "SPIRE_TEST_RESTORED_NODE_NOT_READY",
        )
        assert (
            sandbox.public_state(restored_agent)["spiffe_id"] == first[1]["spiffe_id"]
        )
    finally:
        sandbox.cleanup()
