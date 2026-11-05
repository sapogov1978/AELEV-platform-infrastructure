"""Actual Grafana 12.1 application contracts; isolated loopback test transport."""

import base64
import http.client
import json
import os
import secrets
from pathlib import Path
from typing import Any

import pytest
from test_observation import plan
from test_real_spire import IMAGE, Sandbox, wait

from aelev_spire import observation_setup
from aelev_spire.files import atomic

GRAFANA = os.environ.get("AELEV_INFRASTRUCTURE_SPIRE_GRAFANA_TEST_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not IMAGE or not GRAFANA, reason="Explicit isolated images required"
)


def test_native_grafana_scoped_dashboard_rule_and_repeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential: tuple[bytes, bytes, bytes],
) -> None:
    sandbox = Sandbox()
    password = secrets.token_urlsafe(32)
    try:
        volume = sandbox.volume("grafana-initial")
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
            "from pathlib import Path; import sys; "
            "from aelev_spire.files import atomic; "
            "atomic(Path('/var/lib/spire/password'), sys.stdin.buffer.read())",
            payload=password.encode(),
        )
        container = sandbox.prefix + "-grafana"
        sandbox.docker(
            "run",
            "-d",
            "--name",
            container,
            "--label",
            "io.aelev.spire-test=" + sandbox.id,
            "--user",
            "10001:10001",
            "--memory",
            "384m",
            "--cpus",
            "1",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp:size=16m,mode=1777",
            "--tmpfs",
            "/var/lib/grafana:size=64m,uid=10001,gid=10001,mode=0700",
            "--tmpfs",
            "/var/log/grafana:size=8m,uid=10001,gid=10001,mode=0700",
            "-v",
            volume + ":/run/initial:ro",
            "-p",
            "127.0.0.1::3000",
            "-e",
            "GF_SECURITY_ADMIN_PASSWORD__FILE=/run/initial/password",
            "-e",
            "GF_ANALYTICS_CHECK_FOR_UPDATES=false",
            "-e",
            "GF_ANALYTICS_REPORTING_ENABLED=false",
            "-e",
            "GF_PLUGINS_PREINSTALL_DISABLED=true",
            GRAFANA,
        )
        record = json.loads(
            sandbox.docker(
                "inspect", "--format", "{{json .NetworkSettings.Ports}}", container
            )
        )
        port = int(record["3000/tcp"][0]["HostPort"])
        authorization = (
            "Basic " + base64.b64encode(("admin:" + password).encode()).decode()
        )

        def call(
            method: str,
            route: str,
            data: dict[str, Any] | None = None,
            auth: str = authorization,
        ) -> tuple[int, Any]:
            client = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                client.request(
                    method,
                    route,
                    json.dumps(data).encode() if data else None,
                    {"Authorization": auth, "Content-Type": "application/json"},
                )
                reply = client.getresponse()
                raw = reply.read(1048577)
                assert len(raw) < 1048577
                return reply.status, json.loads(raw) if raw else {}
            finally:
                client.close()

        def ready() -> bool:
            try:
                return (
                    call("GET", "/api/health")[0] == 200
                    and call("GET", "/api/folders?limit=1")[0] == 200
                )
            except (OSError, ValueError, http.client.HTTPException):
                return False

        wait(ready, "SPIRE_TEST_GRAFANA_NOT_READY", seconds=600)
        assert (
            call(
                "POST",
                "/api/folders",
                {"uid": "infrastructure-spire", "title": "SPIRE"},
            )[0]
            == 200
        )
        code, account = call(
            "POST",
            "/api/serviceaccounts",
            {"name": "spire-publisher", "role": "Viewer"},
        )
        assert code == 201
        assert (
            call(
                "POST",
                "/api/folders/infrastructure-spire/permissions",
                {"items": [{"userId": account["id"], "permission": 2}]},
            )[0]
            == 200
        )
        code, token = call(
            "POST",
            f"/api/serviceaccounts/{account['id']}/tokens",
            {"name": "test", "secondsToLive": 3600},
        )
        assert code == 200
        selected = plan()
        selected["grafana_url"] = "https://localhost:" + str(port)
        configuration, secret_root = tmp_path / "configuration", tmp_path / "secrets"
        for name, raw in observation_setup.documents(selected).items():
            atomic(configuration / name, raw)
        atomic(secret_root / "token", token["key"].encode())
        atomic(secret_root / "grafana-ca.crt", credential[2])
        code, rule_account = call(
            "POST",
            "/api/serviceaccounts",
            {"name": "spire-initial-rule-provision", "role": "Admin"},
        )
        assert code == 201
        code, rule_token = call(
            "POST",
            f"/api/serviceaccounts/{rule_account['id']}/tokens",
            {"name": "initial-provision", "secondsToLive": 600},
        )
        assert code == 200
        admin_root = tmp_path / "initial-alert-authority"
        atomic(admin_root / "token", rule_token["key"].encode())
        atomic(admin_root / "grafana-ca.crt", credential[2])
        assert (
            call(
                "POST",
                "/api/datasources",
                {
                    "name": "Isolated metrics",
                    "uid": "aelev-prometheus",
                    "type": "prometheus",
                    "access": "proxy",
                    "url": "https://projection.invalid",
                },
            )[0]
            == 200
        )

        # Only the test boundary adapts HTTPS to the isolated native HTTP
        # listener. Production code still creates/verifies its TLS context;
        # real TLS/mTLS transport has its separate loopback tests.
        original_http = http.client.HTTPConnection

        def local(
            host: str, target_port: int, *, timeout: int, context: Any
        ) -> http.client.HTTPConnection:
            assert host == "localhost" and target_port == port and context is not None
            return original_http("127.0.0.1", port, timeout=timeout)

        monkeypatch.setattr(observation_setup.http.client, "HTTPSConnection", local)
        observation_setup.dashboard(configuration, secret_root)
        observation_setup.dashboard(configuration, secret_root)
        observation_setup.dashboard(configuration, admin_root, alert_only=True)
        observation_setup.dashboard(configuration, admin_root, alert_only=True)
        code, rule = call(
            "GET", "/api/v1/provisioning/alert-rules/aelev-spire-readiness"
        )
        assert code == 200 and rule["folderUID"] == "infrastructure-spire"
        code, _ = call(
            "POST",
            "/api/serviceaccounts",
            {"name": "forbidden", "role": "Admin"},
            "Bearer " + token["key"],
        )
        assert code == 403
    except BaseException:
        if sandbox.containers:
            failed = sandbox.containers[-1]
            raw = sandbox.docker("logs", "--tail", "100", failed, check=False)
            atomic(
                tmp_path / "grafana-failure.log",
                raw.replace(password.encode(), b"<redacted>"),
            )
            status = sandbox.docker(
                "inspect",
                "--format",
                "{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}",
                failed,
                check=False,
            )
            print("SPIRE_TEST_GRAFANA_STATE " + status.decode().strip())
            print(
                "SPIRE_TEST_GRAFANA_DIAGNOSTIC " + str(tmp_path / "grafana-failure.log")
            )
        raise
    finally:
        sandbox.cleanup()
