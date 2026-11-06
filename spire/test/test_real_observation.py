"""Opt-in native Prometheus and durable sender/archive, isolated from the stand."""

import base64
import json
import os
from typing import Any

import pytest
from test_observation import plan
from test_real_spire import IMAGE, Sandbox, wait

from aelev_spire.observation_setup import SECRET_FILES, documents

SENDER = os.environ.get("AELEV_INFRASTRUCTURE_SPIRE_SENDER_TEST_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not IMAGE or not SENDER, reason="Explicit isolated images required"
)


def test_actual_collection_delivery_outage_and_recovery(
    credential: tuple[bytes, bytes, bytes],
) -> None:
    sandbox = Sandbox()
    cert, key, ca = credential
    try:
        sandbox.private_network()

        def deliver(volume: str, files: dict[str, bytes]) -> None:
            sandbox.docker(
                "run",
                "--rm",
                "-i",
                "--network",
                "none",
                "--entrypoint",
                "python",
                "-v",
                volume + ":/var/lib/spire",
                IMAGE,
                "-c",
                "import sys,json,base64; from pathlib import Path; "
                "from aelev_spire.files import atomic; "
                "[atomic(Path('/var/lib/spire')/n,base64.b64decode(v)) "
                "for n,v in json.load(sys.stdin).items()]",
                payload=json.dumps(
                    {n: base64.b64encode(v).decode() for n, v in files.items()}
                ).encode(),
            )

        config, secrets, public = (
            sandbox.volume("configuration"),
            sandbox.volume("secrets"),
            sandbox.volume("public", ephemeral=True),
        )
        selected = plan()
        selected["central_url"] = "https://central.example:4318"
        files = documents(selected)
        files.pop("observation.json")
        sender_config = json.loads(files["sender.json"])
        archive_config: dict[str, Any] = {
            **sender_config,
            "mode": "archive",
            "prometheus_command": [],
            "destinations": {
                kind: "https://projection.invalid" + route
                for kind, route in {
                    "logs": "/v1/logs",
                    "traces": "/v1/traces",
                    "metrics": "/api/v1/write",
                    "otlp_metrics": "/v1/metrics",
                }.items()
            },
        }
        files["archive.json"] = json.dumps(archive_config).encode()
        deliver(config, files)
        material = {
            name: key if name.endswith(".key") else ca if "ca.crt" in name else cert
            for role in ("observer", "sender")
            for name in SECRET_FILES[role]
        }
        deliver(secrets, material)

        native_configuration = sandbox.volume("native-configuration")
        sandbox.seed(
            native_configuration,
            "plan.json",
            {
                "owner": "infrastructure-spire",
                "schema_version": 1,
                "environment": "test",
                "trust_domain": "test.example",
                "svid_ttl_seconds": 20,
            },
        )
        native = sandbox.start(
            "native",
            "server",
            sandbox.volume("native-data"),
            native_configuration,
            [
                "-v",
                public + ":/run/observation",
                "-e",
                "AELEV_INFRASTRUCTURE_SPIRE_OBSERVATION_ROOT=/run/observation",
            ],
        )
        wait(
            lambda: sandbox.public_state(native, heartbeat=True).get("ready") is True,
            "SPIRE_TEST_OBSERVED_NATIVE_NOT_READY",
        )

        def start(name: str, image: str, extra: list[str], command: list[str]) -> str:
            container = sandbox.prefix + "-" + name
            sandbox.docker(
                "run",
                "-d",
                "--name",
                container,
                "--label",
                "io.aelev.spire-test=" + sandbox.id,
                "--network",
                str(sandbox.network),
                "--network-alias",
                name,
                "--memory",
                "384m",
                "--cpus",
                "0.5",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--tmpfs",
                "/tmp:size=32m,mode=1777",
                "--tmpfs",
                "/run/spire:size=2m,uid=10001,gid=10001,mode=0700",
                *extra,
                image,
                *command,
            )
            return container

        archive_state, sender_state, observer_state = (
            sandbox.volume("archive-data"),
            sandbox.volume("sender-data"),
            sandbox.volume("observer-data"),
        )
        for volume in (archive_state, sender_state):
            sandbox.docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "python",
                "-v",
                volume + ":/var/lib/spire",
                IMAGE,
                "-S",
                "-c",
                "from pathlib import Path; "
                "assert Path('/var/lib/spire').stat().st_uid==10001",
            )
        shared = [
            "--entrypoint",
            "python",
            "-v",
            config + ":/etc/aelev:ro",
            "-v",
            secrets + ":/run/secrets/telemetry:ro",
        ]
        archive = start(
            "central.example",
            SENDER,
            [*shared, "-v", archive_state + ":/var/lib/delivery"],
            ["/opt/aelev/delivery.py", "run", "--config", "/etc/aelev/archive.json"],
        )
        sender = start(
            "spire-telemetry-sender",
            SENDER,
            [*shared, "-v", sender_state + ":/var/lib/delivery"],
            ["/opt/aelev/delivery.py", "run", "--config", "/etc/aelev/sender.json"],
        )
        observer = start(
            "spire-observer",
            IMAGE,
            [
                "--entrypoint",
                "python",
                "-e",
                "AELEV_INFRASTRUCTURE_SPIRE_OTLP_ENDPOINT=https://spire-telemetry-sender:4318",
                "-e",
                "AELEV_INFRASTRUCTURE_SPIRE_UI_MODULE_ID="
                + plan()["presentation"]["module_id"],
                "-v",
                public + ":/run/observation:ro",
                "-v",
                secrets + ":/run/secrets/observation:ro",
                "-v",
                observer_state + ":/var/lib/observation",
            ],
            ["-m", "aelev_spire.observation", "serve"],
        )

        def counters(container: str) -> dict[str, int]:
            raw = sandbox.docker(
                "exec",
                container,
                "python",
                "-S",
                "-c",
                "import sqlite3,json; from pathlib import Path; "
                "p=Path('/var/lib/delivery/delivery.sqlite3'); "
                "db=sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True); "
                "rows=db.execute('SELECT kind,count(*) FROM records GROUP BY kind'); "
                "print(json.dumps(dict(rows)))",
                check=False,
            )
            try:
                value: dict[str, int] = json.loads(raw)
                return value
            except ValueError:
                return {}

        wait(
            lambda: {"metrics", "logs", "traces"} <= set(counters(archive)),
            "SPIRE_TEST_SIGNALS_NOT_ARCHIVED",
            seconds=120,
        )
        sandbox.docker(
            "exec",
            sender,
            "/bin/promtool",
            "check",
            "config",
            "/etc/aelev/prometheus.json",
        )
        sandbox.docker(
            "exec", sender, "/bin/promtool", "check", "rules", "/etc/aelev/rules.json"
        )
        previous_archive = counters(archive)
        sandbox.docker("stop", "--time", "10", archive)
        before = counters(sender)
        wait(
            lambda: counters(sender).get("logs", 0) > before.get("logs", 0),
            "SPIRE_TEST_OUTAGE_DID_NOT_PRESERVE_SAMPLE",
            seconds=60,
        )
        sandbox.docker("restart", "--time", "10", sender)
        sandbox.docker("start", archive)
        wait(
            lambda: counters(archive).get("logs", 0) > previous_archive.get("logs", 0),
            "SPIRE_TEST_BACKLOG_NOT_RESTORED",
            seconds=120,
        )
        assert (
            sandbox.docker(
                "inspect", "--format", "{{.State.Running}}", observer
            ).strip()
            == b"true"
        )
    finally:
        sandbox.cleanup()
