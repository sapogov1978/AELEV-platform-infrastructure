"""Opt-in Mac Docker Desktop installation, repeat and revocation lifecycle."""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import platform
import re
import socket
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from test_real_spire import IMAGE, Sandbox

INSTALLER = os.environ.get("AELEV_INFRASTRUCTURE_SPIRE_INSTALLER_TEST_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not IMAGE or not INSTALLER or platform.system() != "Darwin",
    reason="Explicit isolated images and Mac Docker Desktop required",
)


@pytest.mark.parametrize(
    ("shared_coordinator", "exported"), [(False, False), (True, False), (False, True)]
)
def test_shell_install_repeat_register_and_revoke(
    tmp_path: Path, shared_coordinator: bool, exported: bool
) -> None:
    sandbox = Sandbox()
    projects = [sandbox.prefix + "-server", sandbox.prefix + "-agent"]
    source = Path(__file__).resolve().parents[1]

    def install(
        project: str,
        role: str,
        *arguments: str,
        request: bool = False,
        initial: Path | None = None,
        expected_refusal: bool = False,
    ) -> int:
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "HOME",
                "TMPDIR",
                "DOCKER_HOST",
                "DOCKER_CONTEXT",
                "DOCKER_CONFIG",
            }
        }
        for key in (
            "AELEV_INFRASTRUCTURE_SPIRE_INITIAL_INPUT",
            "AELEV_INFRASTRUCTURE_SPIRE_AUTHORITY_SOURCE",
        ):
            env.pop(key, None)
        if request:
            env["AELEV_INFRASTRUCTURE_SPIRE_INITIAL_INPUT"] = str(initial or tmp_path)
        env["DRP_SPIRE_DOCKER_SOCKET_APPROVAL"] = "operator-root-equivalent-v1"
        result = subprocess.run(
            [
                "sh",
                str(source / "install.sh"),
                role,
                project,
                IMAGE,
                INSTALLER,
                *arguments,
            ],
            env=env,
            capture_output=True,
            timeout=240,
            check=False,
        )
        if result.returncode and role != "authorize" and not expected_refusal:
            codes = re.findall(
                r'"code"\s*:\s*"(SPIRE_[A-Z0-9_]+)"',
                result.stderr.decode(errors="replace"),
            )
            raise AssertionError(
                "SPIRE_INSTALLATION_TEST_FAILED " + " ".join(codes)[-300:]
            )
        return result.returncode

    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    try:
        for project in projects:
            assert not sandbox.docker(
                "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project
            ).strip()
            assert not sandbox.docker(
                "volume",
                "ls",
                "-q",
                "--filter",
                "label=io.aelev.installation=" + project,
            ).strip()
        # Do not consume Docker's exhausted default pools or remove other networks.
        network_ids = sandbox.docker("network", "ls", "-q").decode().splitlines()
        network_rows: list[dict[str, Any]] = json.loads(
            sandbox.docker("network", "inspect", *network_ids)
        )
        used: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for network in network_rows:
            ipam: dict[str, Any] = network.get("IPAM") or {}
            configs: list[dict[str, Any]] = ipam.get("Config") or []
            for config in configs:
                if config.get("Subnet"):
                    used.append(ipaddress.ip_network(config["Subnet"], strict=False))
        subnet = next(
            candidate
            for candidate in ipaddress.ip_network("10.254.0.0/16").subnets(
                new_prefix=24
            )
            if not any(candidate.overlaps(existing) for existing in used)
        )
        sandbox.docker(
            "network",
            "create",
            "--subnet",
            str(subnet),
            "--label",
            "io.aelev.owner=infrastructure-spire",
            "--label",
            "io.aelev.installation=" + projects[0],
            "--label",
            "io.aelev.role=access",
            "--label",
            "com.docker.compose.project=" + projects[0],
            "--label",
            "com.docker.compose.network=spire-access",
            projects[0] + "-access",
        )
        assert (
            install(
                projects[0],
                "server",
                "--environment",
                "test",
                "--trust-domain",
                "test.example",
                "--public-port",
                str(port),
                "--svid-ttl",
                "20",
            )
            == 0
        )
        coordinator = projects[0]
        if shared_coordinator:
            # Migrate only this disposable server; keep logical state and CA.
            model = json.loads(
                sandbox.docker(
                    "run",
                    "--rm",
                    "--user",
                    "0:0",
                    "--read-only",
                    "--network",
                    "none",
                    "--memory",
                    "128m",
                    "--entrypoint",
                    "python",
                    "--volume",
                    projects[0] + "_installer:/state:ro",
                    IMAGE,
                    "-S",
                    "-c",
                    "from pathlib import Path; "
                    "print(Path('/state/compose.json').read_text())",
                )
            )
            coordinator = sandbox.prefix + "-infrastructure"
            original = (
                sandbox.docker(
                    "ps",
                    "-aq",
                    "--no-trunc",
                    "--filter",
                    "label=io.aelev.installation=" + projects[0],
                    "--filter",
                    "label=com.docker.compose.service=spire-server",
                )
                .decode()
                .strip()
            )
            assert len(original) == 64
            sandbox.docker("stop", original)
            sandbox.docker("rm", original)
            for definition in model["networks"].values():
                name = definition["name"]
                definition.clear()
                definition.update({"name": name, "external": True})
            sandbox.docker(
                "compose",
                "-p",
                coordinator,
                "-f",
                "-",
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "spire-server",
                payload=json.dumps(model).encode(),
            )
            # A sibling sharing the physical project is not owned by SPIRE.
            sandbox.docker(
                "run",
                "--detach",
                "--name",
                sandbox.prefix + "-foreign",
                "--network",
                "none",
                "--memory",
                "32m",
                "--entrypoint",
                "python",
                "--label",
                "io.aelev.spire-test=" + sandbox.id,
                "--label",
                "com.docker.compose.project=" + coordinator,
                "--label",
                "com.docker.compose.service=fixture-foreign",
                IMAGE,
                "-S",
                "-c",
                "import time; time.sleep(600)",
            )
        container = (
            sandbox.docker(
                "ps",
                "-q",
                "--filter",
                "label=io.aelev.installation=" + projects[0],
                "--filter",
                "label=com.docker.compose.service=spire-server",
            )
            .decode()
            .strip()
        )
        assert install(projects[0], "server") == 0
        assert (
            sandbox.docker(
                "ps",
                "-q",
                "--filter",
                "label=io.aelev.installation=" + projects[0],
                "--filter",
                "label=com.docker.compose.service=spire-server",
            )
            .decode()
            .strip()
            == container
        )
        assert (
            sandbox.docker(
                "inspect",
                "--format",
                '{{index .Config.Labels "com.docker.compose.project"}}',
                container,
            )
            .decode()
            .strip()
            == coordinator
        )
        operation = str(uuid.uuid4())
        value = {
            "owner": "infrastructure-spire",
            "schema_version": 1,
            "environment": "test",
            "trust_domain": "test.example",
            "svid_ttl_seconds": 20,
            "resource_owner": "fixture-owner",
            "operation_id": operation,
            "node_id": str(uuid.uuid4()),
            "spiffe_id": "spiffe://test.example/fixture-owner/component/install-test",
            "dns_names": ["localhost"],
            "actor": "isolated-test",
            "server_address": "spire-server",
            "server_port": 8081,
        }
        (tmp_path / "request.json").write_text(json.dumps(value))
        assert install(projects[0], "authorize", request=True) == 0
        assert install(projects[0], "authorize", request=True) == 0
        if exported:
            assert install(projects[0], "export-bootstrap", request=True) == 0

            def packet() -> bytes:
                return sandbox.docker(
                    "run",
                    "--rm",
                    "--user",
                    "0:0",
                    "--network",
                    "none",
                    "--read-only",
                    "--memory",
                    "128m",
                    "--entrypoint",
                    "python",
                    "--volume",
                    projects[0] + "_bootstrap-export-" + operation + ":/export:ro",
                    IMAGE,
                    "-S",
                    "-c",
                    "import base64,json; from pathlib import Path; "
                    "r=Path('/export/package'); "
                    "print(json.dumps({n:base64.b64encode((r/n).read_bytes()).decode() "
                    "for n in ('plan.json','bundle.crt','join-token',"
                    "'receipt.json')}))",
                )

            first = packet()
            assert install(projects[0], "export-bootstrap", request=True) == 0
            assert packet() == first
            initial = tmp_path / "transferred-package"
            initial.mkdir(mode=0o700)
            for name, content in json.loads(first).items():
                (initial / name).write_bytes(base64.b64decode(content, validate=True))
                (initial / name).chmod(0o600)
            assert (
                install(
                    projects[1],
                    "agent",
                    "--exported-bootstrap",
                    "--bootstrap-operation",
                    operation,
                    "--access-network",
                    projects[0] + "-access",
                    request=True,
                    initial=initial,
                )
                == 0
            )
        else:
            assert (
                install(
                    projects[1],
                    "agent",
                    "--bootstrap-volume",
                    projects[0] + "_bootstrap-output",
                    "--bootstrap-operation",
                    operation,
                    "--access-network",
                    projects[0] + "-access",
                )
                == 0
            )
        assert install(projects[1], "agent") == 0
        assert install(projects[0], "revoke", request=True) == 0
        assert install(projects[0], "authorize", request=True) != 0
        if exported:
            assert (
                install(
                    projects[0], "export-bootstrap", request=True, expected_refusal=True
                )
                != 0
            )
    finally:
        if shared_coordinator:
            foreign = (
                sandbox.docker(
                    "ps", "-aq", "--filter", "name=^/" + sandbox.prefix + "-foreign$"
                )
                .decode()
                .strip()
            )
            if foreign:
                assert (
                    sandbox.docker(
                        "inspect",
                        "--format",
                        '{{index .Config.Labels "io.aelev.spire-test"}}',
                        foreign,
                    )
                    .decode()
                    .strip()
                    == sandbox.id
                )
                sandbox.docker("rm", "--force", foreign)
        # Only the two unique installations created here. Never global down/prune.
        for project in reversed(projects):
            ids = (
                sandbox.docker(
                    "ps", "-aq", "--filter", "label=io.aelev.installation=" + project
                )
                .decode()
                .splitlines()
            )
            for identifier in ids:
                owner = (
                    sandbox.docker(
                        "inspect",
                        "--format",
                        '{{index .Config.Labels "io.aelev.owner"}}',
                        identifier,
                    )
                    .decode()
                    .strip()
                )
                assert owner == "infrastructure-spire"
                sandbox.docker("rm", "--force", identifier)
            volumes = (
                sandbox.docker(
                    "volume",
                    "ls",
                    "-q",
                    "--filter",
                    "label=io.aelev.installation=" + project,
                )
                .decode()
                .splitlines()
            )
            for name in volumes:
                owner = (
                    sandbox.docker(
                        "volume",
                        "inspect",
                        "--format",
                        '{{index .Labels "io.aelev.owner"}}',
                        name,
                    )
                    .decode()
                    .strip()
                )
                assert owner == "infrastructure-spire"
                sandbox.docker("volume", "rm", name)
            networks = (
                sandbox.docker(
                    "network",
                    "ls",
                    "-q",
                    "--filter",
                    "label=com.docker.compose.project=" + project,
                )
                .decode()
                .splitlines()
            )
            for identifier in networks:
                sandbox.docker("network", "rm", identifier)
