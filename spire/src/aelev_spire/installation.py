"""Operator-only Docker installation; no daemon capability leaves this process."""

from __future__ import annotations

import argparse
import base64
import fcntl
import ipaddress
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from .files import atomic, read, reserve
from .operator import validate as validate_request
from .settings import environment, node_ttl, verify_authority


class Installer:
    def __init__(
        self,
        project: str,
        daemon: str,
        root: Path,
        image: str,
        *,
        update: bool = False,
        host_os: str = "Linux",
        compose_project: str | None = None,
    ) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", project):
            raise ValueError("SPIRE_INSTALL_PROJECT_INVALID")
        if (
            compose_project is not None
            and re.fullmatch(r"aelev_[a-z0-9_]{1,57}", compose_project) is None
        ):
            raise ValueError("SPIRE_INSTALL_COMPOSE_PROJECT_INVALID")
        if self.docker("info", "--format", "{{.ID}}").decode().strip() != daemon:
            raise ValueError("SPIRE_INSTALL_DAEMON_MISMATCH")
        if self.docker("info", "--format", "{{.OSType}}").decode().strip() != "linux":
            raise ValueError("SPIRE_INSTALL_LINUX_ENGINE_REQUIRED")
        self.project, self.root, self.image = project, root, image
        self.host_os = host_os
        self.update = update
        marker: dict[str, Any] = {
            "owner": "infrastructure-spire",
            "role": "installer",
            "project": project,
            "daemon_id": daemon,
        }
        existing = root / "owner.json"
        before = read(existing) if existing.exists() else {}
        for role in ("sender", "observer"):
            if (
                type(before.get(role + "_recovery_schema")) is int
                and before.get(role + "_recovery_schema") == 1
            ):
                marker[role + "_recovery_schema"] = 1
        reserve(root, marker)
        self.image_id = (
            self.docker("image", "inspect", image, "--format", "{{.Id}}")
            .decode()
            .strip()
        )
        logging.info('{"code":"SPIRE_INSTALL_CHECKING_RELEASE"}')
        # Both installer and runtime must contain the same selected provider code
        # and native artifact lock, not just a matching mutable tag.
        checker = (
            "from pathlib import Path; import hashlib; h=hashlib.sha256(); "
            "paths=sorted(Path('/opt/aelev/src').rglob('*.py')); "
            "[h.update(str(p.relative_to('/opt/aelev')).encode()+p.read_bytes()) "
            "for p in paths]; "
            "h.update(Path('/opt/aelev/artifacts.lock.json').read_bytes()); "
            "print(h.hexdigest())"
        )
        actual = (
            self.docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--memory",
                "128m",
                "--read-only",
                "--entrypoint",
                "python",
                image,
                "-S",
                "-c",
                checker,
            )
            .decode()
            .strip()
        )
        import hashlib

        own = hashlib.sha256()
        for path in sorted(Path("/opt/aelev/src").rglob("*.py")):
            own.update(str(path.relative_to("/opt/aelev")).encode() + path.read_bytes())
        own.update(Path("/opt/aelev/artifacts.lock.json").read_bytes())
        if actual != own.hexdigest():
            raise ValueError("SPIRE_INSTALL_IMAGE_RELEASE_MISMATCH")
        self.bind_compose_project(compose_project)

    @property
    def compose_project(self) -> str:
        """Physical coordinator; logical project still owns labels and volumes."""
        return getattr(self, "_compose_project", self.project)

    def bind_compose_project(self, preferred: str | None = None) -> None:
        """Pin the verified coordinator for this operator transaction only."""
        self._compose_project = self.select_compose_project(preferred)

    def select_compose_project(self, preferred: str | None = None) -> str:
        """Resolve exact owned runtime labels without adopting a mixed transfer."""
        ids = (
            self.docker(
                "ps",
                "--all",
                "--filter",
                "label=io.aelev.installation=" + self.project,
                "--filter",
                "label=com.docker.compose.service",
                "--format",
                "{{.ID}}",
            )
            .decode()
            .splitlines()
        )
        if not ids:
            return preferred or self.project
        if len(ids) > 32:
            raise ValueError("SPIRE_INSTALL_COORDINATOR_AMBIGUOUS")
        records = json.loads(self.docker("inspect", *ids))
        projects: set[str] = set()
        roles: set[str] = set()
        for record in records:
            labels = record["Config"].get("Labels", {})
            role = labels.get("com.docker.compose.service")
            if role not in {
                "spire-server",
                "spire-agent",
                "spire-observer",
                "spire-status-publisher",
                "spire-telemetry-sender",
            }:
                continue  # Administrative one-shots do not choose a coordinator.
            project = labels.get("com.docker.compose.project", "")
            if (
                labels.get("io.aelev.owner") != "infrastructure-spire"
                or labels.get("io.aelev.installation") != self.project
                or not isinstance(project, str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", project)
            ):
                raise ValueError("SPIRE_INSTALL_COORDINATOR_OWNER_MISMATCH")
            if role in roles:
                raise ValueError("SPIRE_INSTALL_COORDINATOR_AMBIGUOUS")
            roles.add(role)
            projects.add(project)
        if len(projects) > 1:
            raise ValueError("SPIRE_INSTALL_COORDINATOR_MIXED")
        return next(iter(projects), self.project)

    def docker(self, *arguments: str, payload: bytes | None = None) -> bytes:
        helper = None
        if arguments and arguments[0] == "run" and "--name" not in arguments:
            helper = self.project + "-task-" + uuid.uuid4().hex[:12]
            arguments = (
                "run",
                "--name",
                helper,
                "--label",
                "io.aelev.owner=infrastructure-spire",
                "--label",
                "io.aelev.installation=" + self.project,
                "--label",
                "io.aelev.role=operator-helper",
                *arguments[1:],
            )
        try:
            result = subprocess.run(
                ["docker", *arguments],
                input=payload,
                capture_output=True,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired:
            if helper is not None:
                try:
                    inspected = subprocess.run(
                        ["docker", "inspect", helper],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                    if inspected.returncode == 0:
                        record = json.loads(inspected.stdout)[0]
                        labels = record["Config"]["Labels"]
                        if (
                            labels.get("io.aelev.owner") != "infrastructure-spire"
                            or labels.get("io.aelev.installation") != self.project
                            or labels.get("io.aelev.role") != "operator-helper"
                            or record["Name"] != "/" + helper
                        ):
                            raise ValueError("SPIRE_INSTALL_HELPER_OWNER_MISMATCH")
                        removed = subprocess.run(
                            ["docker", "rm", "--force", helper],
                            capture_output=True,
                            timeout=15,
                            check=False,
                        )
                        if removed.returncode:
                            raise ValueError("SPIRE_INSTALL_HELPER_STATE_UNKNOWN")
                except (OSError, subprocess.TimeoutExpired, KeyError, TypeError):
                    raise ValueError("SPIRE_INSTALL_HELPER_STATE_UNKNOWN") from None
            raise ValueError("SPIRE_INSTALL_HELPER_TIMEOUT") from None
        if result.returncode or len(result.stdout) > 4 * 1024**2:
            if (
                b"all predefined address pools have been fully subnetted"
                in result.stderr
            ):
                raise ValueError("SPIRE_INSTALL_NETWORK_ADDRESS_POOL_EXHAUSTED")
            for line in result.stdout.decode(errors="replace").splitlines():
                try:
                    value = json.loads(line).get("code")
                except (ValueError, AttributeError):
                    continue
                if isinstance(value, str) and re.fullmatch(
                    r"SPIRE_[A-Z0-9_]{1,120}", value
                ):
                    raise ValueError(value)
            raise ValueError("SPIRE_INSTALL_DOCKER_OPERATION_FAILED")
        return result.stdout

    def authority_binding(self, source: str) -> str:
        if self.host_os != "Windows":
            if not Path(source).is_absolute():
                raise ValueError("SPIRE_INSTALL_EXTERNAL_AUTHORITY_REQUIRED")
            return source
        if not PureWindowsPath(source).is_absolute():
            raise ValueError("SPIRE_INSTALL_EXTERNAL_AUTHORITY_REQUIRED")
        # Docker Desktop's host-path translation is owned by Docker. Read the
        # actual engine path of this installer's already-mounted input, instead
        # of guessing /host_mnt or treating C:\\ as a Linux absolute path.
        current = Path("/etc/hostname").read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", current):
            raise ValueError("SPIRE_INSTALL_SELF_IDENTITY_REQUIRED")
        record = json.loads(self.docker("inspect", current))[0]
        labels = record["Config"]["Labels"]
        if (
            labels.get("io.aelev.owner") != "infrastructure-spire"
            or labels.get("io.aelev.installation") != self.project
            or labels.get("io.aelev.role") != "operator-installer"
        ):
            raise ValueError("SPIRE_INSTALL_SELF_IDENTITY_REQUIRED")
        mounts = [
            m
            for m in record["Mounts"]
            if m.get("Destination") == "/run/authority-source"
            and m.get("Type") == "bind"
            and m.get("RW") is False
        ]
        if len(mounts) != 1 or not Path(mounts[0]["Source"]).is_absolute():
            raise ValueError("SPIRE_INSTALL_AUTHORITY_MOUNT_REQUIRED")
        return str(mounts[0]["Source"])

    def bootstrap(self, volume: str, operation: str) -> dict[str, bytes]:
        logging.info('{"code":"SPIRE_INSTALL_READING_BOOTSTRAP"}')
        if str(uuid.UUID(operation)) != operation:
            raise ValueError("SPIRE_INSTALL_OPERATION_INVALID")
        record = json.loads(self.docker("volume", "inspect", volume))[0]
        labels = record.get("Labels", {})
        if (
            labels.get("io.aelev.owner") != "infrastructure-spire"
            or labels.get("io.aelev.role") != "bootstrap-output"
            or not re.fullmatch(
                r"[a-z][a-z0-9_-]{2,63}", labels.get("io.aelev.installation", "")
            )
        ):
            raise ValueError("SPIRE_INSTALL_FOREIGN_VOLUME_REFUSED")
        helper = (
            "import base64,json,sys; from pathlib import Path; "
            "root=Path('/bootstrap') / sys.argv[1]; "
            "names=('plan.json','bundle.crt','join-token'); "
            "assert all(not (root/n).is_symlink() and "
            "(root/n).stat().st_size<=65536 for n in names); "
            "print(json.dumps({n:base64.b64encode((root/n).read_bytes()).decode() "
            "for n in names}))"
        )
        raw = self.docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--memory",
            "128m",
            "--entrypoint",
            "python",
            "-v",
            volume + ":/bootstrap:ro",
            self.image,
            "-S",
            "-c",
            helper,
            operation,
        )
        result: dict[str, str] = json.loads(raw)
        return {
            name: base64.b64decode(value, validate=True)
            for name, value in result.items()
        }

    def volume(self, name: str, *, ephemeral: bool = False) -> str:
        full = self.project + "_" + name
        names = (
            self.docker("volume", "ls", "--format", "{{.Name}}").decode().splitlines()
        )
        labels = {
            "io.aelev.owner": "infrastructure-spire",
            "io.aelev.installation": self.project,
            "io.aelev.role": name,
        }
        if full in names:
            existing = json.loads(self.docker("volume", "inspect", full))[0]
            if any(
                existing.get("Labels", {}).get(key) != value
                for key, value in labels.items()
            ):
                raise ValueError("SPIRE_INSTALL_FOREIGN_VOLUME_REFUSED")
            if ephemeral and existing.get("Options", {}) != {
                "type": "tmpfs",
                "device": "tmpfs",
                "o": "size=2m,uid=10001,gid=10001,mode=0700",
            }:
                raise ValueError("SPIRE_INSTALL_EPHEMERAL_VOLUME_REQUIRED")
        else:
            persistent = {
                "configuration",
                "server",
                "agent",
                "operator",
                "bootstrap-output",
                "observation",
                "status",
                "telemetry",
                "observation-configuration",
                "observer-secrets",
                "sender-secrets",
                "status-secrets",
                "status-admin",
                "grafana-secrets",
                "grafana-admin",
                "certificate-renewal-status",
            }
            if (
                (self.root / "deployment.json").exists()
                and name in persistent
                and (
                    name
                    in {
                        "configuration",
                        "server",
                        "agent",
                        "operator",
                        "bootstrap-output",
                    }
                    or (self.root / "observation-deployment.json").exists()
                )
            ):
                raise ValueError("SPIRE_INSTALL_STATE_RESTORE_REQUIRED")
            command = ["volume", "create"]
            for key, value in labels.items():
                command += ["--label", key + "=" + value]
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
            self.docker(*command, full)
        return full

    def seed(self, volume: str, role: str, files: dict[str, bytes]) -> None:
        logging.info('{"code":"SPIRE_INSTALL_SEEDING_CONFIGURATION"}')
        payload = {
            "marker": {
                "owner": "infrastructure-spire",
                "role": role,
                "project": self.project,
            },
            "files": {
                name: base64.b64encode(raw).decode() for name, raw in files.items()
            },
        }
        self.docker(
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--read-only",
            "--memory",
            "128m",
            "--pids-limit",
            "64",
            "--entrypoint",
            "python",
            "-v",
            volume + ":/var/lib/spire",
            self.image,
            "-m",
            "aelev_spire.configuration",
            payload=json.dumps(payload).encode(),
        )

    def operate(self, action: str, request: dict[str, Any]) -> dict[str, Any]:
        validate_request(request)
        deployment = read(self.root / "deployment.json")
        if deployment["role"] != "server" or any(
            deployment["plan"].get(key) != request.get(key)
            for key in ("environment", "trust_domain")
        ):
            raise ValueError("SPIRE_INSTALL_OPERATOR_SCOPE_MISMATCH")
        if deployment["image_id"] != self.image_id:
            raise ValueError("SPIRE_INSTALL_OPERATOR_RELEASE_MISMATCH")
        request_volume = self.volume("request-" + request["operation_id"])
        self.seed(
            request_volume,
            "operator-request",
            {
                "request.json": json.dumps(request, sort_keys=True).encode(),
            },
        )
        export_arguments: list[str] = []
        export_volume = None
        if action == "export-bootstrap":
            export_volume = self.volume("bootstrap-export-" + request["operation_id"])
            export_arguments = ["-v", export_volume + ":/run/export"]
        response = self.docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--memory",
            "128m",
            "--pids-limit",
            "64",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "-v",
            self.volume("admin", ephemeral=True) + ":/run/spire/admin",
            "-v",
            self.volume("operator") + ":/var/lib/spire",
            "-v",
            request_volume + ":/run/request:ro",
            "-v",
            self.volume("bootstrap-output")
            + ":/run/svid"
            + (":ro" if action == "export-bootstrap" else ""),
            *export_arguments,
            self.image,
            action,
            "--plan",
            "/run/request/request.json",
            "--output",
            "/run/svid/" + request["operation_id"],
            *(["--export-path", "/run/export/package"] if export_volume else []),
        )
        result: dict[str, Any] = json.loads(response)
        logging.info(
            json.dumps(
                {
                    "owner": "infrastructure-spire",
                    "action": action,
                    "operation_id": request["operation_id"],
                    "bootstrap_volume": self.project + "_bootstrap-output",
                    "bootstrap_directory": request["operation_id"],
                    "phase": result["phase"],
                }
            )
        )
        if export_volume:
            result |= {"export_volume": export_volume, "export_directory": "package"}
        return result

    def resolve(self, file: str, values: dict[str, str]) -> dict[str, Any]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("AELEV_", "COMPOSE_"))
        }
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                os.devnull,
                "--profile",
                "*",
                "-f",
                "/opt/aelev/" + file,
                "config",
                "--format",
                "json",
                "--no-env-resolution",
            ],
            env={**env, **values},
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise ValueError("SPIRE_INSTALL_COMPOSE_INVALID")
        return json.loads(result.stdout)

    def apply(
        self,
        role: str,
        plan: dict[str, Any],
        files: dict[str, bytes],
        authority_source: str | None,
        access_network: str | None = None,
        container_name: str | None = None,
    ) -> None:
        mode, _, _ = environment(plan)
        if (
            plan.get("owner") != "infrastructure-spire"
            or plan.get("schema_version") != 1
        ):
            raise ValueError("SPIRE_INSTALL_PLAN_OWNER_INVALID")
        if container_name is not None and (
            role != "agent"
            or re.fullmatch(r"aelev_[a-z0-9_]{1,248}", container_name) is None
        ):
            raise ValueError("SPIRE_INSTALL_CONTAINER_NAME_INVALID")
        bind_address = "127.0.0.1"
        if role == "server":
            node_ttl(plan)
            port = plan.get("public_port", 18081)
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("SPIRE_INSTALL_PORT_INVALID")
            address = plan.get("bind_address", "127.0.0.1")
            try:
                if not isinstance(address, str):
                    raise ValueError("invalid bind address")
                bind = ipaddress.ip_address(address)
                bind_address = str(bind) if bind.version == 4 else "[" + str(bind) + "]"
            except ValueError:
                raise ValueError("SPIRE_INSTALL_BIND_ADDRESS_INVALID") from None
        if mode != "production":
            authority_source = None
        if mode == "production" and "@sha256:" not in self.image:
            raise ValueError("SPIRE_INSTALL_PRODUCTION_IMAGE_DIGEST_REQUIRED")
        path = self.root / "deployment.json"
        expected = {
            "role": role,
            "plan": plan,
            "image": self.image,
            "image_id": self.image_id,
            "authority_source": authority_source,
            "access_network": access_network,
        }
        if container_name is not None:
            expected["container_name"] = container_name
        if path.exists() and read(path) != expected:
            previous = read(path)
            name_only_upgrade = (
                container_name is not None
                and "container_name" not in previous
                and previous
                == {
                    key: value
                    for key, value in expected.items()
                    if key != "container_name"
                }
            )
            if any(
                previous.get(key) != expected[key]
                for key in ("role", "plan", "authority_source", "access_network")
            ) or not (self.update or name_only_upgrade):
                raise ValueError("SPIRE_INSTALL_EXISTING_DEPLOYMENT_PRESERVED")
            if not name_only_upgrade:
                atomic(self.root / "previous-deployment.json", previous)
        configuration = self.volume("configuration")
        state = self.volume("server" if role == "server" else "agent")
        if role == "server":
            authority_binding = None
            if mode == "production":
                if not authority_source:
                    raise ValueError("SPIRE_INSTALL_EXTERNAL_AUTHORITY_REQUIRED")
                authority_binding = self.authority_binding(authority_source)
                verify_authority(Path("/run/authority-source"), production=True)
            if files:
                self.seed(configuration, "server-configuration", files)
            values = {
                "AELEV_INFRASTRUCTURE_SPIRE_COMPOSE_PROJECT": self.project,
                "AELEV_INFRASTRUCTURE_SPIRE_IMAGE": self.image,
                "AELEV_INFRASTRUCTURE_SPIRE_SERVER_VOLUME": state,
                "AELEV_INFRASTRUCTURE_SPIRE_CONFIGURATION_VOLUME": configuration,
                "AELEV_INFRASTRUCTURE_SPIRE_ADMIN_VOLUME": self.volume(
                    "admin", ephemeral=True
                ),
                "AELEV_INFRASTRUCTURE_SPIRE_OPERATOR_VOLUME": self.volume("operator"),
                "AELEV_INFRASTRUCTURE_SPIRE_BOOTSTRAP_VOLUME": self.volume(
                    "bootstrap-output"
                ),
                "AELEV_INFRASTRUCTURE_SPIRE_ACCESS_NETWORK": self.project + "-access",
                "AELEV_INFRASTRUCTURE_SPIRE_OBSERVATION_VOLUME": self.volume(
                    "observation-source", ephemeral=True
                ),
                "AELEV_INFRASTRUCTURE_SPIRE_BIND_ADDRESS": bind_address,
                "AELEV_INFRASTRUCTURE_SPIRE_PORT": str(plan.get("public_port", 18081)),
            }
            model = self.resolve("compose.yml", values)
            target = "spire-server"
            if mode == "production":
                model["services"][target]["command"] = [
                    "server",
                    "--bootstrap",
                    "/run/authority",
                ]
                model["services"][target]["volumes"].append(
                    {
                        "type": "bind",
                        "source": authority_binding,
                        "target": "/run/authority",
                        "read_only": True,
                        "bind": {"create_host_path": False},
                    }
                )
        else:
            if files:
                self.seed(configuration, "agent-bootstrap", files)
            values = {
                "AELEV_INFRASTRUCTURE_SPIRE_AGENT_PROJECT": self.project,
                "AELEV_INFRASTRUCTURE_SPIRE_IMAGE": self.image,
                "AELEV_INFRASTRUCTURE_SPIRE_AGENT_VOLUME": state,
                "AELEV_INFRASTRUCTURE_SPIRE_AGENT_BOOTSTRAP_VOLUME": configuration,
                "AELEV_INFRASTRUCTURE_SPIRE_SVID_VOLUME": self.volume(
                    "svid", ephemeral=True
                ),
            }
            model = self.resolve("compose.agent.yml", values)
            target = "spire-agent"
            if container_name is not None:
                model["services"][target]["container_name"] = container_name
        if access_network:
            if role != "agent":
                raise ValueError("SPIRE_INSTALL_AGENT_NETWORK_REQUIRED")
            network = json.loads(self.docker("network", "inspect", access_network))[0]
            labels = network.get("Labels", {})
            if (
                labels.get("io.aelev.owner") != "infrastructure-spire"
                or labels.get("io.aelev.role") != "access"
            ):
                raise ValueError("SPIRE_INSTALL_FOREIGN_NETWORK_REFUSED")
            model["networks"] = {
                "spire-access": {"name": access_network, "external": True}
            }
            model["services"][target]["networks"] = {"spire-access": {}}
        else:
            for definition in model.get("networks", {}).values():
                definition.setdefault("labels", {}).update(
                    {
                        "io.aelev.owner": "infrastructure-spire",
                        "io.aelev.installation": self.project,
                        "io.aelev.role": "access",
                    }
                )
        for key, definition in model["volumes"].items():
            # All volumes have just passed exact ownership checks above.
            if "name" not in definition:
                raise ValueError("SPIRE_INSTALL_VOLUME_NAME_REQUIRED")
            model["volumes"][key] = {"name": definition["name"], "external": True}
        for service in model["services"].values():
            service.setdefault("labels", {}).update(
                {
                    "io.aelev.owner": "infrastructure-spire",
                    "io.aelev.installation": self.project,
                }
            )
        ids = (
            self.docker(
                "ps",
                "--all",
                "--filter",
                "label=com.docker.compose.project=" + self.compose_project,
                "--filter",
                "label=io.aelev.installation=" + self.project,
                "--format",
                "{{.ID}}",
            )
            .decode()
            .splitlines()
        )
        for container in ids:
            labels = json.loads(
                self.docker("inspect", "--format", "{{json .Config.Labels}}", container)
            )
            if (
                labels.get("io.aelev.installation") != self.project
                or labels.get("io.aelev.owner") != "infrastructure-spire"
            ):
                raise ValueError("SPIRE_INSTALL_FOREIGN_CONTAINER_REFUSED")
        atomic(path, expected)
        compose = self.root / "compose.json"
        atomic(compose, model)
        logging.info('{"code":"SPIRE_INSTALL_STARTING_OWN_ROLE"}')
        self.docker(
            "compose",
            "--env-file",
            os.devnull,
            "-p",
            self.compose_project,
            "-f",
            str(compose),
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
            target,
        )
        deadline = time.monotonic() + 120
        logging.info('{"code":"SPIRE_INSTALL_WAITING_READINESS"}')
        while time.monotonic() < deadline:
            container = (
                self.docker(
                    "compose",
                    "-p",
                    self.compose_project,
                    "-f",
                    str(compose),
                    "ps",
                    "-q",
                    target,
                )
                .decode()
                .strip()
            )
            if container:
                runtime_state = (
                    self.docker(
                        "inspect",
                        "--format",
                        "{{.State.Status}}",
                        container,
                    )
                    .decode()
                    .strip()
                )
                if runtime_state != "running":
                    time.sleep(2)
                    continue
                raw = self.docker(
                    "exec",
                    container,
                    "python",
                    "-S",
                    "-c",
                    "from pathlib import Path; "
                    "p=Path('/run/spire/heartbeat.json'); "
                    "print(p.read_text() if p.is_file() else '{}')",
                )
                status = json.loads(raw)
                age = (
                    datetime.now(UTC)
                    - datetime.fromisoformat(
                        status.get("updated_at", "1970-01-01T00:00:00+00:00")
                    )
                ).total_seconds()
                if (
                    status.get("ready") is True
                    and status.get("owner") == "infrastructure-spire"
                    and 0 <= age < 15
                ):
                    health = (
                        self.docker(
                            "inspect",
                            "--format",
                            "{{.State.Health.Status}}",
                            container,
                        )
                        .decode()
                        .strip()
                    )
                    if health != "healthy":
                        time.sleep(2)
                        continue
                    atomic(
                        self.root / "result.json",
                        {
                            "owner": "infrastructure-spire",
                            "project": self.project,
                            "role": role,
                            "ready": True,
                            "checked_at": time.time(),
                        },
                    )
                    return
            time.sleep(2)
        raise ValueError("SPIRE_INSTALL_READINESS_DEADLINE")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "role",
        choices=(
            "server",
            "agent",
            "authorize",
            "revoke",
            "inspect",
            "observation",
            "export-bootstrap",
        ),
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--daemon-id", required=True)
    parser.add_argument(
        "--host-os", choices=("Darwin", "Linux", "Windows"), required=True
    )
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--authority-source")
    parser.add_argument("--access-network")
    parser.add_argument("--compose-project")
    parser.add_argument("--container-name")
    parser.add_argument("--bootstrap-volume")
    parser.add_argument("--bootstrap-operation")
    parser.add_argument("--exported-bootstrap", action="store_true")
    parser.add_argument("--environment", choices=("local", "test", "production"))
    parser.add_argument("--trust-domain")
    parser.add_argument("--bind-address", default="127.0.0.1")
    parser.add_argument("--public-port", type=int, default=18081)
    parser.add_argument("--svid-ttl", type=int, default=300)
    parser.add_argument("--agent-ttl", type=int)
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--root", type=Path, default=Path("/installation"))
    parser.add_argument("--sender-image")
    parser.add_argument("--central-url")
    parser.add_argument("--ui-url")
    parser.add_argument("--identity-url")
    parser.add_argument("--realm")
    parser.add_argument("--publisher-client-id")
    parser.add_argument("--module-id")
    parser.add_argument("--grafana-url")
    parser.add_argument("--datasource-uid")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        installer = Installer(
            arguments.project,
            arguments.daemon_id,
            arguments.root,
            arguments.runtime_image,
            update=arguments.update,
            host_os=arguments.host_os,
            compose_project=arguments.compose_project,
        )
        lock_path = arguments.root / "installation.lock"
        if lock_path.is_symlink():
            raise ValueError("SPIRE_INSTALL_LOCK_INVALID")
        lock = lock_path.open("a+b")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if arguments.role == "observation":
            from .observation_setup import install

            source = Path("/run/input/observation.json")
            existing = arguments.root / "observation-deployment.json"
            if source.is_file():
                observation_plan = read(source)
            elif arguments.sender_image:
                observation_plan = {
                    "owner": "infrastructure-spire",
                    "schema_version": 1,
                    "sender_image": arguments.sender_image,
                    "central_url": arguments.central_url,
                    "grafana_url": arguments.grafana_url,
                    "datasource_uid": arguments.datasource_uid,
                    "presentation": {
                        "url": arguments.ui_url,
                        "identity_url": arguments.identity_url,
                        "realm": arguments.realm,
                        "client_id": arguments.publisher_client_id,
                        "audience": "aelev-platform-ui",
                        "module_id": arguments.module_id
                        or str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL, "aelev/spire/" + arguments.project
                            )
                        ),
                    },
                }
            elif existing.is_file():
                observation_plan = read(existing)
            else:
                raise ValueError("SPIRE_OBSERVATION_INITIAL_INPUT_REQUIRED")
            install(installer, observation_plan, Path("/run/input"))
            return
        if arguments.role in {"authorize", "revoke", "inspect", "export-bootstrap"}:
            result = installer.operate(
                arguments.role, read(Path("/run/input/request.json"))
            )
            if arguments.role == "export-bootstrap":
                sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
            return
        plan_path = Path("/run/input/plan.json")
        if arguments.exported_bootstrap:
            if (
                arguments.role != "agent"
                or not arguments.bootstrap_operation
                or arguments.bootstrap_volume
            ):
                raise ValueError("SPIRE_INSTALL_BOOTSTRAP_SCOPE_INVALID")
            from .bootstrap_export import consume

            files = consume(Path("/run/input"), arguments.bootstrap_operation)
            installer.apply(
                "agent",
                json.loads(files["plan.json"]),
                files,
                None,
                arguments.access_network,
                arguments.container_name,
            )
            return
        if arguments.bootstrap_volume:
            if arguments.role != "agent" or not arguments.bootstrap_operation:
                raise ValueError("SPIRE_INSTALL_BOOTSTRAP_SCOPE_INVALID")
            files = installer.bootstrap(
                arguments.bootstrap_volume, arguments.bootstrap_operation
            )
            plan = json.loads(files["plan.json"])
        elif plan_path.exists():
            plan = read(plan_path)
            files = {"plan.json": plan_path.read_bytes()}
            if arguments.role == "agent":
                for name in ("bundle.crt", "join-token"):
                    path = Path("/run/input") / name
                    if path.is_symlink() or path.stat().st_size > 65536:
                        raise ValueError("SPIRE_INSTALL_INITIAL_INPUT_INVALID")
                    files[name] = path.read_bytes()
        elif (arguments.root / "deployment.json").is_file():
            previous = read(arguments.root / "deployment.json")
            plan = previous["plan"]
            files = {}
            if arguments.authority_source is None:
                arguments.authority_source = previous["authority_source"]
            if arguments.access_network is None:
                arguments.access_network = previous.get("access_network")
        elif (
            arguments.role == "server"
            and arguments.environment
            and arguments.trust_domain
        ):
            plan = {
                "owner": "infrastructure-spire",
                "schema_version": 1,
                "environment": arguments.environment,
                "trust_domain": arguments.trust_domain,
                "svid_ttl_seconds": arguments.svid_ttl,
                "bind_address": arguments.bind_address,
                "public_port": arguments.public_port,
            }
            if arguments.agent_ttl is not None:
                plan["agent_ttl_seconds"] = arguments.agent_ttl
            files = {"plan.json": json.dumps(plan, sort_keys=True).encode()}
        else:
            raise ValueError("SPIRE_INSTALL_INITIAL_PLAN_REQUIRED")
        installer.apply(
            arguments.role,
            plan,
            files,
            arguments.authority_source,
            arguments.access_network,
            arguments.container_name,
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        code = str(error)
        logging.error(
            json.dumps(
                {
                    "code": (
                        code
                        if re.fullmatch(r"SPIRE_[A-Z0-9_]{1,120}", code)
                        else "SPIRE_INSTALL_FAILED"
                    )
                }
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
