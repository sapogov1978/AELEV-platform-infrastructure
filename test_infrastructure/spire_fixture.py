"""Explicit fresh local/test SPIRE inputs and real certificate maintenance."""

from __future__ import annotations

import json
import os
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
            if values[name] not in {gateway, "0.0.0.0"}:
                raise RuntimeError("ROOT_SPIRE_PUBLISHED_ROUTE_REQUIRED")

        telemetry = self.root / "observability/runtime/local-issuer/internal-ca.crt"
        publisher = self.root / "platform-ui/secrets/issuer/publisher.crt"
        issued = {}
        for name, issuer in (
            ("central-write", telemetry),
            ("status-publisher", publisher),
        ):
            self.invoke(
                [
                    sys.executable,
                    str(
                        self.root
                        / "platform-ui/client/src/aelev_platform_ui_client/prepare.py"
                    ),
                    "--root",
                    str(self.directory),
                    "--name",
                    name,
                    "--ca-cert",
                    str(issuer),
                    "--ca-key",
                    str(issuer.with_suffix(".key")),
                ]
            )
            issued[name] = {
                "certificate": str(self.directory / (name + ".crt")),
                "key": str(self.directory / (name + ".key")),
                "issuer_certificate": str(issuer),
            }
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
