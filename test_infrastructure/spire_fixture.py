"""Explicit fresh local/test SPIRE inputs and real certificate maintenance."""

from __future__ import annotations

import ast
import http.client
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from resources import Inventory, Owners

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import full_local, full_runtime, spire_local  # noqa: E402


class Fixture:
    """Own only new resources of one explicitly selected SPIRE installation."""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root.resolve()
        env = full_local.read_env(self.root / "spire/.env", self.root)
        self.project = env["AELEV_INFRASTRUCTURE_SPIRE_COMPOSE_PROJECT"]
        self.owners: Owners = frozenset({("infrastructure-spire", self.project)})
        self.directory = self.root / "runtime" / ("root-spire-" + uuid4().hex)
        self.source = self.directory / "local.json"
        self.module_id = str(uuid5(NAMESPACE_URL, "root-lifecycle/" + self.project))
        self.original_env = (self.root / ".env").read_bytes()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.failure = ""
        self.states: list[str] = []

    def preflight(self, before: Inventory) -> None:
        # This is a fresh-bootstrap test, never an adoption of an installed owner.
        core = full_local.read_env(self.root / ".env", self.root)
        if core.get("AELEV_INFRASTRUCTURE_COMPOSE_PROJECT") == self.project:
            raise RuntimeError("ROOT_SPIRE_COORDINATOR_CONFLICT")
        if not full_runtime.initial_binding_required(self.root):
            raise RuntimeError("ROOT_SPIRE_EXISTING_BINDING_REFUSED")
        for kind in ("containers", "networks", "volumes"):
            if any(
                row.get("project") == self.project
                or (row.get("owner"), row.get("installation")) in self.owners
                for row in before[kind].values()
            ):
                raise RuntimeError("ROOT_SPIRE_EXISTING_RESOURCES_REFUSED")

    def invoke(self, arguments: list[str], *, timeout: int = 120) -> str:
        diagnostic = self.directory / ("operation-" + uuid4().hex)
        try:
            result = subprocess.run(
                arguments, cwd=self.root, capture_output=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("ROOT_SPIRE_OPERATION_RESULT_UNKNOWN") from None
        spire_local.private(diagnostic.with_suffix(".stdout"), result.stdout)
        spire_local.private(diagnostic.with_suffix(".stderr"), result.stderr)
        if result.returncode:
            raise RuntimeError("ROOT_SPIRE_OPERATOR_FAILED")
        return result.stdout.decode()

    def prepare(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        platform = full_local.read_env(self.root / "platform-ui/.env", self.root)
        gateway = platform["AELEV_INFRASTRUCTURE_PLATFORM_UI_LOCAL_BRIDGE_BIND"]
        # Routes must be declared before build/BOM, never changed during a trial.
        for owner, key in (("delivery", "DELIVERY"), ("grafana", "GRAFANA")):
            path = self.root / "observability" / owner / ".env"
            name = "AELEV_INFRASTRUCTURE_" + key + "_BIND"
            values = full_local.read_env(path, self.root)
            address = ipaddress.ip_address(values[name])
            if address.version != 4 or not (
                str(address) == gateway or address.is_unspecified
            ):
                raise RuntimeError("ROOT_SPIRE_PUBLISHED_ROUTE_REQUIRED")

        telemetry = self.root / "observability/runtime/local-issuer/internal-ca.crt"
        telemetry_publisher = (
            self.root / "observability/runtime/local-issuer/publisher-ca.crt"
        )
        publisher = self.root / "runtime/root-spire-publisher-authority/publisher.crt"
        issued = {}
        for name, issuer in (
            ("central-write", telemetry_publisher),
            ("status-publisher", publisher),
        ):
            issued[name] = self.issue_client(name, issuer)
        delivery = full_local.read_env(
            self.root / "observability/delivery/.env", self.root
        )
        grafana = full_local.read_env(
            self.root / "observability/grafana/.env", self.root
        )
        cfg: dict[str, Any] = {
            "schema_version": 1,
            "environment": "test",
            "server_plan": {
                "owner": "infrastructure-spire",
                "schema_version": 1,
                "environment": "test",
                "trust_domain": "clean-room.test",
                "svid_ttl_seconds": 300,
                "public_source": {
                    "bind_address": "127.0.0.1",
                    "public_port": 18084,
                    "identity": "cn:AELEV SPIRE public-source",
                    "client_identities": [
                        "cn:AELEV SPIRE observer",
                        "cn:AELEV SPIRE status-query",
                    ],
                },
            },
            "observation_plan": {
                "owner": "infrastructure-spire",
                "schema_version": 1,
                "sender_image": self.image("aelev/spire-telemetry-sender:local"),
                "central_url": "https://delivery:"
                + delivery["AELEV_INFRASTRUCTURE_DELIVERY_PORT"],
                "grafana_url": "https://grafana:"
                + grafana["AELEV_INFRASTRUCTURE_GRAFANA_PORT"],
                "datasource_uid": "aelev-prometheus",
                "presentation": {
                    "url": platform["AELEV_INFRASTRUCTURE_PLATFORM_UI_UI_URL"],
                    "identity_url": platform[
                        "AELEV_INFRASTRUCTURE_PLATFORM_UI_UI_IDENTITY_URL"
                    ],
                    "realm": platform[
                        "AELEV_INFRASTRUCTURE_PLATFORM_UI_KEYCLOAK_REALM"
                    ],
                    "client_id": "spire-status",
                    "audience": "platform-ui",
                    "module_id": self.module_id,
                },
                "host_gateway_aliases": [
                    "delivery",
                    "grafana",
                    "host.docker.internal",
                    "keycloak",
                ],
                "network": {
                    "source_url": "https://spire-server:8084",
                    "source_identity": "cn:AELEV SPIRE public-source",
                    "observer_url": "https://spire-observer:8083",
                    "sender_otlp_url": "https://spire-telemetry-sender:4318",
                    "sender_health_url": "https://spire-telemetry-sender:13134",
                },
            },
            "issued": issued,
            "trust": {
                "central": str(telemetry),
                "shell": str(self.root / "platform-ui/secrets/issuer/transport.crt"),
                "identity": str(self.root / "keycloak/secrets/https/ca.crt"),
                "grafana": str(telemetry),
            },
            "status_admin": {
                "user": str(self.directory / "admin-user"),
                "password": str(self.root / "keycloak/secrets/admin-password"),
            },
        }
        identity = full_local.read_env(self.root / "keycloak/.env", self.root)
        spire_local.private(
            self.directory / "admin-user",
            (identity["AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_USER"] + "\n").encode(),
        )
        spire_local.save(self.source, cfg)

    def image(self, reference: str) -> str:
        return self.invoke(
            ["docker", "image", "inspect", "--format", "{{.Id}}", reference]
        ).strip()

    def issue_client(self, name: str, issuer: Path) -> dict[str, str]:
        """Issue a fresh 90-day owner leaf; never reuse another runtime identity."""
        key = self.directory / (name + ".key")
        request = self.directory / (name + ".csr")
        certificate = self.directory / (name + ".crt")
        extensions = self.directory / (name + ".extensions")
        if any(p.exists() for p in (key, request, certificate, extensions)):
            raise RuntimeError("ROOT_SPIRE_ISSUED_IDENTITY_EXISTS")
        spire_local.private(
            extensions,
            b"basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=clientAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n",
        )
        self.invoke(
            [
                "openssl",
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                "/CN=SPIRE " + name,
                "-keyout",
                str(key),
                "-out",
                str(request),
            ]
        )
        self.invoke(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(request),
                "-CA",
                str(issuer),
                "-CAkey",
                str(issuer.with_suffix(".key")),
                "-set_serial",
                str(secrets.randbits(128)),
                "-days",
                "90",
                "-sha256",
                "-extfile",
                str(extensions),
                "-out",
                str(certificate),
            ]
        )
        for path in (key, request, certificate):
            path.chmod(0o600)
        self.invoke(
            [
                "openssl",
                "verify",
                "-x509_strict",
                "-purpose",
                "sslclient",
                "-CAfile",
                str(issuer),
                str(certificate),
            ]
        )
        self.invoke(
            [
                "openssl",
                "x509",
                "-in",
                str(certificate),
                "-checkend",
                str(30 * 86400),
                "-noout",
            ]
        )
        return {
            "certificate": str(certificate),
            "key": str(key),
            "issuer_certificate": str(issuer),
        }

    def enroll(self) -> Path:
        material = self.root / "runtime/spire-local-input/material"
        requests = json.loads((material / "renewal-requests.json").read_bytes())
        cfg = json.loads(self.source.read_bytes())
        profiles = {}
        for name, entries in requests["profiles"].items():
            operator = self.directory / name.removesuffix(".json")
            operator.mkdir(mode=0o700)
            reports = operator / "reports"
            reports.mkdir(mode=0o700)
            approved = []
            for entry in entries:
                issuer = entry["issuer_scope"]
                issuer_key = (
                    material / "authorities" / (issuer + ".key")
                    if issuer in {"query", "server", "write"}
                    else Path(
                        cfg["issued"][entry["id"]]["issuer_certificate"]
                    ).with_suffix(".key")
                )
                approved.append(
                    {
                        key: entry[key]
                        for key in ("id", "certificate", "key", "issuer_certificate")
                    }
                    | {"issuer_key": str(issuer_key)}
                )
            approval = operator / "approval.json"
            spire_local.save(
                approval,
                {
                    "schema_version": 1,
                    "environment": "test",
                    "consumer_owner": "infrastructure-spire",
                    "openssl": str(
                        Path(shutil.which("openssl") or "missing-openssl").resolve()
                    ),
                    "report": str(reports / name),
                    "certificates": approved,
                },
            )
            state = operator / "state"
            profiles[name] = {"approval": str(approval), "state": str(state)}
            self.states.append(str(state))
        manifest = self.directory / "renewal.json"
        spire_local.save(
            manifest,
            {
                "schema_version": 1,
                "environment": "test",
                "maintenance": "operator",
                "helper_image": self.image(
                    "aelev/infrastructure-spire-certificate-delivery:local"
                ),
                "profiles": profiles,
            },
        )
        return manifest

    def maintain(self) -> None:
        def refresh() -> None:
            while not self.stop_event.wait(45):
                try:
                    for state in self.states:
                        if self.stop_event.is_set():
                            return
                        self.invoke(
                            [
                                sys.executable,
                                str(self.root / "observability/delivery/renewal.py"),
                                "once",
                                "--state",
                                state,
                            ],
                            timeout=60,
                        )
                except Exception:
                    # Unknown outcomes stop maintenance; they are never retried.
                    self.failure = "ROOT_SPIRE_MAINTENANCE_FAILED"
                    return

        self.thread = threading.Thread(
            target=refresh, name="root-spire-renewal", daemon=True
        )
        self.thread.start()

    def check(self) -> None:
        if self.failure:
            raise RuntimeError(self.failure)

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=70)
            if self.thread.is_alive():
                raise RuntimeError("ROOT_SPIRE_MAINTENANCE_STOP_UNKNOWN")

    def compose(self) -> list[str]:
        if full_runtime.initial_binding_required(self.root):
            return []  # A partial bootstrap has no export; exact owner labels remain.
        try:
            receipt = full_runtime.spire_binding(self.root)
        except full_local.FullLocalError:
            return []  # A damaged partial export cannot suppress scoped cleanup.
        if receipt["spire-server"]["compose_project"] != self.project:
            return []  # Never run Compose down against an unadmitted coordinator.
        return [
            "docker",
            "compose",
            "-p",
            receipt["spire-server"]["compose_project"],
            "-f",
            str(full_runtime.spire_projection(self.root)),
        ]

    def diagnostic(self, output: Path) -> None:
        """Publish only source-declared failure codes, never raw operator output."""
        from sender_diagnostic import read_status

        central: dict[str, Any] = {"code": "ROOT_CENTRAL_DELIVERY_STATUS_UNAVAILABLE"}
        try:
            owner = self.root / "observability/delivery"
            cfg = full_local.read_env(owner / ".env", self.root)
            secrets = owner / cfg["AELEV_INFRASTRUCTURE_DELIVERY_SECRET_ROOT"]
            central = {
                "code": "ROOT_CENTRAL_DELIVERY_STATUS",
                "status": read_status(
                    "https://localhost:"
                    + cfg["AELEV_INFRASTRUCTURE_DELIVERY_HEALTH_PORT"],
                    str(secrets / "ca.crt"),
                    str(secrets / "client.crt"),
                    str(secrets / "client.key"),
                ),
            }
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            http.client.HTTPException,
            full_local.FullLocalError,
        ):
            pass
        allowed: set[str] = set()
        for source in (self.root / "spire/src/aelev_spire").glob("*.py"):
            for node in ast.walk(ast.parse(source.read_text())):
                if (
                    isinstance(node, ast.Raise)
                    and isinstance(node.exc, ast.Call)
                    and node.exc.args
                    and isinstance(node.exc.args[0], ast.Constant)
                    and isinstance(node.exc.args[0].value, str)
                    and re.fullmatch(r"SPIRE_[A-Z0-9_]{1,120}", node.exc.args[0].value)
                ):
                    allowed.add(node.exc.args[0].value)
        operations = []
        allowed.update(
            re.findall(
                r"echo '(SPIRE_EXPORT_[A-Z0-9_]+)' >&2",
                (self.root / "spire/export.sh").read_text(),
            )
        )
        for source in (self.root / "runtime/spire-installed").glob(
            "bootstrap-*/*.stderr"
        ):
            if (
                source.name
                not in {"server.stderr", "observation.stderr", "export.stderr"}
                or source.is_symlink()
                or source.stat().st_size > 2 * 1024 * 1024
            ):
                continue
            found = re.findall(
                r'"code"\s*:\s*"(SPIRE_[A-Z0-9_]{1,120})"',
                source.read_text(errors="replace"),
            )
            found.extend(
                re.findall(
                    r"^(?:ValueError|RuntimeError): (SPIRE_[A-Z0-9_]{1,120})$",
                    source.read_text(errors="replace"),
                    re.MULTILINE,
                )
            )
            found.extend(
                re.findall(
                    r"^(SPIRE_EXPORT_[A-Z0-9_]{1,120})$",
                    source.read_text(errors="replace"),
                    re.MULTILINE,
                )
            )
            operations.append(
                {"phase": source.stem, "failure_codes": sorted(set(found) & allowed)}
            )
        roles = []
        sender = {"code": "ROOT_SPIRE_SENDER_STATUS_UNAVAILABLE"}
        for row in full_runtime.containers(self.project, self.root):
            labels = row["Config"].get("Labels") or {}
            name = labels.get("com.docker.compose.service")
            if name not in full_runtime.SPIRE_ROLES:
                continue
            state = row["State"]
            if (
                name == "spire-status-publisher"
                and state.get("Running") is True
                and labels.get("io.aelev.owner") == "infrastructure-spire"
                and labels.get("io.aelev.installation") == self.project
            ):
                sender = self.sender_diagnostic(row["Id"])
            roles.append(
                {
                    "service": name,
                    "status": state.get("Status"),
                    "running": state.get("Running"),
                    "exit_code": state.get("ExitCode"),
                    "health": state.get("Health", {}).get("Status"),
                }
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "code": "ROOT_SPIRE_FAILURE_CONTEXT",
                    "operations": operations,
                    "roles": roles,
                    "sender": sender,
                    "central_delivery": central,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    def sender_diagnostic(self, container: str) -> dict[str, Any]:
        from sender_diagnostic import sanitize

        unavailable = {"code": "ROOT_SPIRE_SENDER_STATUS_UNAVAILABLE"}
        try:
            script = self.root / "test_infrastructure/sender_diagnostic.py"
            result = subprocess.run(
                ["docker", "exec", "-i", container, "python", "-S", "-"],
                input=script.read_bytes(),
                capture_output=True,
                timeout=12,
            )
            if result.returncode or len(result.stdout) > 65536:
                return unavailable
            value = json.loads(result.stdout)
            if (
                not isinstance(value, dict)
                or value.get("code") != "ROOT_SPIRE_SENDER_STATUS"
            ):
                return unavailable
            return {"code": value["code"], "status": sanitize(value.get("status"))}
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return unavailable

    @staticmethod
    def replace(path: Path, expected: bytes, updated: bytes) -> None:
        if path.is_symlink() or path.read_bytes() != expected:
            raise RuntimeError("ROOT_SPIRE_ENV_CHANGED")
        with path.open("r+b") as stream:
            stream.write(updated)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())

    def restore_binding(self) -> None:
        path = self.root / ".env"
        current = path.read_bytes()
        if current != self.original_env:
            projection = str(self.root / "runtime/spire-installed") + "/"
            added = current[len(self.original_env) :]
            expected = (
                full_runtime.SPIRE_COMPOSITION_VARIABLE + "=" + projection
            ).encode()
            if (
                not current.startswith(self.original_env)
                or not added.startswith(expected)
                or len(added.splitlines()) != 1
            ):
                raise RuntimeError("ROOT_SPIRE_ENV_CHANGED")
            self.replace(path, current, self.original_env)
