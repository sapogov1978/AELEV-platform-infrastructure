"""Reproduce the owner's example using real isolated products, never the live archive."""

from __future__ import annotations

import argparse
import base64
import importlib.util
from importlib import import_module
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "owner-metrics"
PROMETHEUS = "prom/prometheus:v3.5.0@sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996"
GRAFANA = "grafana/grafana:12.1.0@sha256:6ac590e7cabc2fbe8d7b8fc1ce9c9f0582177b334e0df9c927ebd9670469440f"
PYTHON = "python:3.13.7-alpine3.22@sha256:9ba6d8cbebf0fb6546ae71f2a1c14f6ffd2fdab83af7fa5669734ef30ad48844"
EXPRESSION = 'aelev_demo_ticks_total{owner_id="owner-demo"}'
LABEL = "com.docker.compose.project"


def command(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120)
    if result.returncode:
        # Do not include arbitrary subprocess output, private configs or credentials.
        raise RuntimeError(f"command {args[0]} failed with exit {result.returncode}")
    return result.stdout.strip()


def inventory() -> dict[str, set[str]]:
    return {
        "containers": set(command("docker", "ps", "-aq").split()),
        "volumes": set(command("docker", "volume", "ls", "-q").split()),
        "networks": set(command("docker", "network", "ls", "-q").split()),
        "images": set(command("docker", "image", "ls", "-q", "--no-trunc").split()),
        "running": set(command("docker", "ps", "-q").split()),
    }


def eventually(check: Callable[[], Any], seconds: int = 90) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (OSError, RuntimeError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("bounded readiness/verification deadline exceeded")


def private(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    path.write_text(body)
    path.chmod(0o640)


def certificates(root: Path) -> None:
    """Local test issuers only; no machine trust-store modification."""
    issuer = root / "issuer"
    issuer.mkdir(mode=0o700)
    for name in ("server", "local-write", "central-write", "query"):
        command(
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-sha256",
            "-days",
            "2",
            "-subj",
            f"/CN=isolated-{name}",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout",
            str(issuer / (name + ".key")),
            "-out",
            str(issuer / (name + ".crt")),
        )
    roles = {
        "exporter": [("exporter", "server", "serverAuth", "DNS:exporter")],
        "sender": [
            ("sender", "server", "serverAuth", "DNS:localhost,DNS:sender"),
            ("local-write", "local-write", "clientAuth", ""),
            ("central-write", "central-write", "clientAuth", ""),
            ("local-query", "query", "clientAuth", ""),
        ],
        "center": [
            (
                "archive",
                "server",
                "serverAuth",
                "DNS:localhost,DNS:telemetry.example.invalid",
            ),
            ("metrics", "server", "serverAuth", "DNS:localhost,DNS:metrics"),
            ("projection", "query", "clientAuth", ""),
        ],
        "grafana": [
            ("grafana", "server", "serverAuth", "DNS:localhost,DNS:grafana"),
            ("query", "query", "clientAuth", ""),
        ],
    }
    for role, entries in roles.items():
        target = root / "secrets" / role
        target.mkdir(parents=True, mode=0o750)
        for name, ca, purpose, san in entries:
            extension = issuer / "extension.cnf"
            extension.write_text(
                f"basicConstraints=CA:FALSE\nextendedKeyUsage={purpose}\n"
                + (f"subjectAltName={san}\n" if san else "")
            )
            command(
                "openssl",
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                f"/CN={name}",
                "-keyout",
                str(target / (name + ".key")),
                "-out",
                str(issuer / "request.csr"),
            )
            command(
                "openssl",
                "x509",
                "-req",
                "-sha256",
                "-days",
                "2",
                "-in",
                str(issuer / "request.csr"),
                "-CA",
                str(issuer / (ca + ".crt")),
                "-CAkey",
                str(issuer / (ca + ".key")),
                "-CAcreateserial",
                "-extfile",
                str(extension),
                "-out",
                str(target / (name + ".crt")),
            )
        for name, source in (
            ("server-ca", "server"),
            ("central-ca", "server"),
            ("local-write-ca", "local-write"),
            ("central-write-ca", "central-write"),
            ("local-query-ca", "query"),
        ):
            shutil.copyfile(issuer / (source + ".crt"), target / (name + ".crt"))
        for path in target.iterdir():
            path.chmod(0o640)
    for path in issuer.iterdir():
        path.chmod(0o600)


def center_files(root: Path, project: str, sender: str) -> Path:
    private(
        root / "metrics.yml",
        "global:\n  scrape_interval: 2s\nstorage:\n  tsdb:\n    out_of_order_time_window: 1h\nscrape_configs: []\n",
    )
    private(
        root / "metrics-web.yml",
        "tls_server_config:\n  cert_file: /secrets/metrics.crt\n  key_file: /secrets/metrics.key\n  client_auth_type: RequireAndVerifyClientCert\n  client_ca_file: /secrets/local-query-ca.crt\n  min_version: TLS12\n",
    )
    archive = json.loads((EXAMPLE / "sender.json").read_text())
    archive.update(
        mode="archive",
        prometheus_command=[],
        server_cert="/secrets/archive.crt",
        server_key="/secrets/archive.key",
        client_ca="/secrets/central-write-ca.crt",
        ca="/secrets/server-ca.crt",
        client_cert="/secrets/projection.crt",
        client_key="/secrets/projection.key",
        health_ca="/secrets/local-query-ca.crt",
        health_cert="/secrets/projection.crt",
        health_key="/secrets/projection.key",
        destinations={
            "logs": "https://metrics:9090/v1/logs",
            "traces": "https://metrics:9090/v1/traces",
            "metrics": "https://metrics:9090/api/v1/write",
            "otlp_metrics": "https://metrics:9090/api/v1/otlp/v1/metrics",
        },
    )
    private(root / "archive.json", json.dumps(archive))
    secrets = root / "secrets" / "grafana"
    private(secrets / "admin-password", uuid4().hex + uuid4().hex)
    private(secrets / "encryption-key", uuid4().hex + uuid4().hex)
    source = {
        "apiVersion": 1,
        "datasources": [
            {
                "name": "Prometheus",
                "type": "prometheus",
                "uid": "aelev-prometheus",
                "url": "https://metrics:9090",
                "access": "proxy",
                "isDefault": True,
                "jsonData": {
                    "tlsAuth": True,
                    "tlsAuthWithCACert": True,
                    "tlsSkipVerify": False,
                },
                "secureJsonData": {
                    "tlsCACert": (secrets / "server-ca.crt").read_text(),
                    "tlsClientCert": (secrets / "query.crt").read_text(),
                    "tlsClientKey": (secrets / "query.key").read_text(),
                },
            }
        ],
    }
    private(root / "datasources" / "source.yaml", json.dumps(source))
    gid = str(os.getgid())
    hardening: dict[str, Any] = {
        "read_only": True,
        "group_add": [gid],
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "tmpfs": ["/tmp:size=16m,mode=1777"],
        "mem_limit": "256m",
    }
    data = {
        "name": project,
        "services": {
            "init": {
                "image": sender,
                "profiles": ["provision"],
                "network_mode": "none",
                "user": "0:0",
                "entrypoint": ["python", "-c"],
                "command": [
                    "import os; [(os.chown(p,u,u),os.chmod(p,0o700)) for p,u in [('/archive',10001),('/metrics',65534),('/grafana',472)]]"
                ],
                "volumes": ["archive:/archive", "metrics:/metrics", "grafana:/grafana"],
            },
            "archive": {
                **hardening,
                "image": sender,
                "user": "10001:10001",
                "command": [
                    "python",
                    "/opt/aelev/delivery.py",
                    "run",
                    "--config",
                    "/etc/aelev/delivery.json",
                ],
                "volumes": [
                    f"{root / 'archive.json'}:/etc/aelev/delivery.json:ro",
                    f"{root / 'secrets/center'}:/secrets:ro",
                    "archive:/var/lib/delivery",
                ],
                "ports": ["127.0.0.1::13134"],
            },
            "metrics": {
                **hardening,
                "image": PROMETHEUS,
                "user": "65534:65534",
                "command": [
                    "--config.file=/etc/aelev/prometheus.yml",
                    "--web.config.file=/etc/aelev/web.yml",
                    "--web.enable-remote-write-receiver",
                    "--storage.tsdb.retention.time=4h",
                ],
                "volumes": [
                    f"{root / 'metrics.yml'}:/etc/aelev/prometheus.yml:ro",
                    f"{root / 'metrics-web.yml'}:/etc/aelev/web.yml:ro",
                    f"{root / 'secrets/center'}:/secrets:ro",
                    "metrics:/prometheus",
                ],
            },
            "grafana": {
                **hardening,
                "image": GRAFANA,
                "user": "472:472",
                "mem_limit": "512m",
                "environment": {
                    "GF_SERVER_PROTOCOL": "https",
                    "GF_SERVER_CERT_FILE": "/secrets/grafana.crt",
                    "GF_SERVER_CERT_KEY": "/secrets/grafana.key",
                    "GF_SECURITY_ADMIN_USER": "onboarding-admin",
                    "GF_SECURITY_ADMIN_PASSWORD__FILE": "/secrets/admin-password",
                    "GF_SECURITY_SECRET_KEY__FILE": "/secrets/encryption-key",
                    "GF_USERS_ALLOW_SIGN_UP": "false",
                    "GF_AUTH_ANONYMOUS_ENABLED": "false",
                    "GF_ANALYTICS_REPORTING_ENABLED": "false",
                    "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
                    "GF_SMTP_ENABLED": "false",
                    "GF_PLUGINS_PREINSTALL_DISABLED": "true",
                },
                "volumes": [
                    f"{secrets}:/secrets:ro",
                    f"{root / 'datasources'}:/etc/grafana/provisioning/datasources:ro",
                    "grafana:/var/lib/grafana",
                ],
                "ports": ["127.0.0.1::3000"],
            },
        },
        "volumes": {"archive": {}, "metrics": {}, "grafana": {}},
    }
    path = root / "center.json"
    private(path, json.dumps(data))
    return path


def main() -> None:
    # Dynamic loading must not leave an ignored cache in the example's source tree.
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sender-image", required=True, help="existing immutable sha256 image ID"
    )
    parser.add_argument("--chromium", type=Path, required=True)
    parser.add_argument(
        "--report", type=Path, default=ROOT / "test/onboarding-result.md"
    )
    args = parser.parse_args()
    sender = command(
        "docker", "image", "inspect", "--format", "{{.Id}}", args.sender_image
    )
    if args.sender_image != sender:
        raise ValueError(
            "resolve the supplied sender image to its exact sha256 ID first"
        )
    for image in (PROMETHEUS, GRAFANA, PYTHON):
        command("docker", "image", "inspect", image)
    command("openssl", "version")
    # Browser is operator tooling, not an extra dependency of the native service.
    sync_playwright = import_module("playwright.sync_api").sync_playwright

    spec = importlib.util.spec_from_file_location(
        "onboarding_grafana_api", EXAMPLE / "grafana_api.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("example client unavailable")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    baseline = inventory()
    run_id = "aelev-grafana-onboarding-" + uuid4().hex[:12]
    checks: list[tuple[str, str]] = []
    failure: str | None = None
    cleanup_ok = False
    started = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix=run_id + "-") as temporary:
        root = Path(temporary)
        owner = root / "owner"
        shutil.copytree(EXAMPLE, owner)
        center = root / "center"
        center.mkdir()
        certificates(root)
        source_project, center_project = run_id + "-owner", run_id + "-center"
        env = "\n".join(
            f"AELEV_DEMO_TELEMETRY_{key}={value}"
            for key, value in {
                "COMPOSE_PROJECT": source_project,
                "PYTHON_IMAGE": PYTHON,
                "SENDER_IMAGE": sender,
                "SECRET_ROOT": root / "secrets",
                "SECRET_GID": os.getgid(),
            }.items()
        )
        private(owner / ".env", env + "\n")
        center_compose = center_files(root, center_project, sender)

        def compose(which: str, *arguments: str) -> str:
            if which == "owner":
                return command(
                    "docker", "compose", "--env-file", ".env", *arguments, cwd=owner
                )
            return command(
                "docker", "compose", "-f", str(center_compose), *arguments, cwd=root
            )

        def container(which: str, service: str) -> str:
            return compose(which, "ps", "-q", "--all", service)

        def port(which: str, service: str, target: int) -> int:
            return int(compose(which, "port", service, str(target)).rsplit(":", 1)[1])

        ca = root / "secrets/sender/server-ca.crt"
        query_context = ssl.create_default_context(cafile=str(ca))
        # TLS 1.3 failed in the operator's OpenSSL 3.6.5 driver in this setup.
        # Test-driver connections stay verified and use the permitted TLS 1.2;
        # deployed Python 3.13 clients and product configs remain unchanged.
        query_context.maximum_version = ssl.TLSVersion.TLSv1_2
        query_context.load_cert_chain(
            root / "secrets/sender/local-query.crt",
            root / "secrets/sender/local-query.key",
        )

        def get_json(url: str, context: ssl.SSLContext = query_context) -> Any:
            with urlopen(url, context=context, timeout=5) as response:
                return json.load(response)

        def journal(which: str, service: str) -> dict[str, Any]:
            result: dict[str, Any] = get_json(
                f"https://localhost:{port(which, service, 13134)}/status"
            )
            return result

        def raw_points(url: str, ending: float) -> list[list[Any]]:
            params = urlencode({"query": EXPRESSION + "[20s]", "time": f"{ending:.3f}"})
            result = get_json(url + "/api/v1/query?" + params)["data"]["result"]
            return result[0]["values"] if result else []

        try:
            compose("owner", "config", "--quiet")
            compose("center", "config", "--quiet")
            compose("owner", "--profile", "provision", "run", "--rm", "sender-init")
            compose("center", "--profile", "provision", "run", "--rm", "init")
            compose(
                "owner",
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "/bin/promtool",
                "sender",
                "check",
                "config",
                "/etc/aelev/prometheus.yml",
            )
            checks.append(
                (
                    "Конфигурация",
                    "Оба Compose разрешены; promtool проверил неизменённый пример владельца.",
                )
            )
            compose("center", "up", "-d", "--no-build", "--pull", "never")
            compose("owner", "up", "-d", "--no-build", "--pull", "never")
            # Test-only connection of its own archive, not a required WAN bridge.
            network = source_project + "_default"
            archive_id = container("center", "archive")
            command(
                "docker",
                "network",
                "connect",
                "--alias",
                "telemetry.example.invalid",
                network,
                archive_id,
            )
            grafana_url = f"https://localhost:{port('center', 'grafana', 3000)}"
            basic = base64.b64encode(
                (
                    "onboarding-admin:"
                    + (root / "secrets/grafana/admin-password").read_text().strip()
                ).encode()
            ).decode()
            admin = api.Client(grafana_url, ca, "Basic " + basic)
            admin.context.maximum_version = ssl.TLSVersion.TLSv1_2
            eventually(lambda: admin.call("GET", "/api/health").get("database") == "ok")
            eventually(lambda: journal("owner", "sender")["worker_alive"] == 1)
            try:
                anonymous_context = ssl.create_default_context(cafile=str(ca))
                anonymous_context.maximum_version = ssl.TLSVersion.TLSv1_2
                get_json(
                    f"https://localhost:{port('owner', 'sender', 13134)}/status",
                    anonymous_context,
                )
            except URLError as error:
                if not isinstance(
                    error.reason, ssl.SSLError
                ) or error.reason.reason not in {
                    "SSLV3_ALERT_HANDSHAKE_FAILURE",
                    "TLSV13_ALERT_CERTIFICATE_REQUIRED",
                }:
                    raise
                checks.append(
                    (
                        "mTLS",
                        "Диагностический вход отверг соединение без клиентского сертификата; CA/SAN проверяются.",
                    )
                )
            else:
                raise RuntimeError("diagnostic endpoint accepted a missing certificate")
            admin.call(
                "POST",
                "/api/folders",
                {"uid": "owner-demo", "title": "Учебный владелец"},
            )
            admin.call(
                "POST",
                "/api/folders",
                {"uid": "other-demo", "title": "Другая учебная папка"},
            )
            account = admin.call(
                "POST",
                "/api/serviceaccounts",
                {"name": "owner-demo-publisher", "role": "Viewer"},
            )
            admin.call(
                "POST",
                "/api/folders/owner-demo/permissions",
                {"items": [{"userId": account["id"], "permission": 2}]},
            )
            token = admin.call(
                "POST",
                f"/api/serviceaccounts/{account['id']}/tokens",
                {"name": "onboarding", "secondsToLive": 3600},
            )
            token_file = root / "publisher" / "token"
            private(token_file, token["key"] + "\n")
            token_file.chmod(0o600)
            shutil.copyfile(ca, token_file.parent / "ca.crt")
            publisher = api.Client(
                grafana_url, ca, "Bearer " + api.read_secret(token_file)
            )
            publisher.context.maximum_version = ssl.TLSVersion.TLSv1_2
            dashboard = json.loads((owner / "dashboard.json").read_text())

            def cli(action: str, *arguments: str) -> str:
                return command(
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    center_project + "_default",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "-e",
                    "PYTHONDONTWRITEBYTECODE=1",
                    "-v",
                    f"{owner / 'grafana_api.py'}:/example/grafana_api.py:ro",
                    "-v",
                    f"{owner / 'dashboard.json'}:/example/dashboard.json:ro",
                    "-v",
                    f"{token_file.parent}:/secrets:ro",
                    PYTHON,
                    "python",
                    "/example/grafana_api.py",
                    action,
                    "--url",
                    "https://grafana:3000",
                    "--ca",
                    "/secrets/ca.crt",
                    "--token-file",
                    "/secrets/token",
                    *arguments,
                )

            first = json.loads(
                cli(
                    "publish",
                    "--folder",
                    "owner-demo",
                    "--dashboard",
                    "/example/dashboard.json",
                )
            )
            second = json.loads(
                cli(
                    "publish",
                    "--folder",
                    "owner-demo",
                    "--dashboard",
                    "/example/dashboard.json",
                )
            )
            inspected = json.loads(cli("inspect", "--uid", "owner-demo-metrics"))
            if inspected["folderUid"] != "owner-demo":
                raise RuntimeError("CLI did not confirm the owner folder")
            if first["id"] != second["id"] or first["uid"] != second["uid"]:
                raise RuntimeError("repeat publication created another resource")
            search = publisher.call(
                "GET", "/api/search?type=dash-db&dashboardUIDs=owner-demo-metrics"
            )
            if len(search) != 1:
                raise RuntimeError("dashboard is missing or duplicated")
            checks.append(
                (
                    "Публикация",
                    f"Viewer + Edit своей папки: повтор дал один UID и ID; версии {first['version']} → {second['version']}.",
                )
            )
            for path, payload in (
                (
                    "/api/dashboards/db",
                    {
                        "dashboard": {**dashboard, "uid": "other-demo-metrics"},
                        "folderUid": "other-demo",
                    },
                ),
                ("/api/serviceaccounts", {"name": "forbidden", "role": "Admin"}),
            ):
                try:
                    publisher.call("POST", path, payload)
                except api.ApiError as error:
                    if error.status != 403:
                        raise
                else:
                    raise RuntimeError("publisher escaped its assigned edit scope")
            checks.append(
                (
                    "Права",
                    "Издателю запрещены запись в чужую папку и создание административной identity: HTTP 403.",
                )
            )
            source_url = f"https://localhost:{port('owner', 'sender', 9090)}"
            eventually(lambda: len(raw_points(source_url, time.time())) >= 6)
            proxy = "/api/datasources/proxy/uid/aelev-prometheus/api/v1/query?"

            def central_points(ending: float) -> list[list[Any]]:
                params = urlencode(
                    {"query": EXPRESSION + "[20s]", "time": f"{ending:.3f}"}
                )
                result = publisher.call("GET", proxy + params)["data"]["result"]
                return result[0]["values"] if result else []

            eventually(lambda: len(central_points(time.time())) >= 4)
            cli(
                "query",
                "--datasource",
                "aelev-prometheus",
                "--expression",
                EXPRESSION,
                "--start-ms",
                str(int((time.time() - 30) * 1000)),
                "--end-ms",
                str(int(time.time() * 1000)),
            )
            checks.append(
                (
                    "Сквозная доставка",
                    "Настоящий экспортёр → Prometheus 3.5.0 → sender → archive → Prometheus 3.5.0 → запрос Grafana 12.1.0.",
                )
            )
            compose("center", "stop", "archive")
            outage_start = time.time()
            eventually(lambda: journal("owner", "sender")["pending"] >= 2)
            # Time window long enough for multiple real scrapes and remote-write batches.
            eventually(lambda: time.time() - outage_start >= 22, seconds=35)
            ending = time.time()
            expected = raw_points(source_url, ending)
            if len(expected) < 8 or central_points(ending):
                raise RuntimeError(
                    "outage did not create an actual missing central history window"
                )
            pending_before = journal("owner", "sender")["pending"]
            compose("owner", "restart", "sender")
            eventually(lambda: journal("owner", "sender")["pending"] >= pending_before)
            compose("center", "start", "archive")
            restored = eventually(
                lambda: central_points(ending) == expected and central_points(ending)
            )
            checks.append(
                (
                    "Восстановление истории",
                    f"При отключённом архиве накоплено {pending_before} пакетов; после рестарта sender и возврата архива совпали все {len(restored)} исходных пар timestamp/value фиксированного окна. Отсутствующая история не заменена текущим временем.",
                )
            )
            eventually(lambda: journal("owner", "sender")["pending"] == 0)
            retained = journal("center", "archive")["records"]
            if retained < 1:
                raise RuntimeError("archive did not retain accepted original packets")
            checks.append(
                (
                    "Квитанции и архив",
                    f"Исходная задолженность завершилась; временный центральный архив сохранил {retained} исходных пакетов после применения.",
                )
            )
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=str(args.chromium), headless=True
                )
                # Explicit exception for this ephemeral self-signed test PKI only.
                context = browser.new_context(
                    ignore_https_errors=True, viewport={"width": 1440, "height": 900}
                )
                page = context.new_page()
                page.goto(grafana_url + "/login")
                page.locator('input[name="user"]').fill("onboarding-admin")
                page.locator('input[name="password"]').fill(
                    (root / "secrets/grafana/admin-password").read_text().strip()
                )
                page.get_by_role("button", name="Log in", exact=True).click()
                page.wait_for_url(lambda url: "/login" not in url, timeout=30000)
                queries: list[int] = []
                page.on(
                    "response",
                    lambda response: (
                        queries.append(response.status)
                        if "/api/ds/query" in response.url
                        else None
                    ),
                )
                with page.expect_response(
                    lambda response: "/api/ds/query" in response.url
                ) as incoming:
                    page.goto(grafana_url + second["url"])
                if incoming.value.status != 200:
                    raise RuntimeError("browser datasource query failed")
                frames = incoming.value.json()["results"]["A"]["frames"]
                if not frames or not frames[0]["data"]["values"][0]:
                    raise RuntimeError("browser query returned no data points")
                page.get_by_text("Счётчик учебного экспортёра", exact=True).wait_for()
                if page.get_by_text("No data", exact=True).count():
                    raise RuntimeError("browser panel rendered without data")
                page.screenshot(
                    path=str(args.report.with_suffix(".png")), full_page=True
                )
                browser.close()
            checks.append(
                (
                    "Браузер",
                    "Настоящая панель открыта в Chromium; запрос данных вернул 200, No data отсутствует; снимок сохранён.",
                )
            )
            cli("delete", "--uid", "owner-demo-metrics")
            admin.call(
                "DELETE", f"/api/serviceaccounts/{account['id']}/tokens/{token['id']}"
            )
            try:
                publisher.call("GET", "/api/health")
                publisher.call("GET", "/api/search")
            except api.ApiError as error:
                if error.status != 401:
                    raise
            else:
                raise RuntimeError("revoked token is still authorized")
            checks.append(
                (
                    "Отзыв",
                    "Панель удалена отдельной операцией; отозванный токен больше не даёт доступ: HTTP 401.",
                )
            )
        except (Exception, KeyboardInterrupt) as error:
            failure = type(error).__name__ + ": " + str(error)
        finally:
            # These UUID projects did not exist before the test. Inspect exact ownership.
            for which, project in (
                ("center", center_project),
                ("owner", source_project),
            ):
                try:
                    ids = command(
                        "docker", "ps", "-aq", "--filter", f"label={LABEL}={project}"
                    ).split()
                    if baseline["containers"].intersection(ids):
                        raise RuntimeError("cleanup target existed before the test")
                    for item in ids:
                        actual = command(
                            "docker",
                            "inspect",
                            "--format",
                            '{{index .Config.Labels "com.docker.compose.project"}}',
                            item,
                        )
                        if actual != project:
                            raise RuntimeError("cleanup ownership mismatch")
                    compose(which, "down", "--volumes", "--remove-orphans")
                except Exception as error:
                    failure = (
                        failure or ""
                    ) + f"; cleanup {which}: {type(error).__name__}"
            remaining = inventory()
            cleanup_ok = remaining == baseline
            if not cleanup_ok:
                failure = (
                    failure or ""
                ) + "; Docker inventory/running baseline differs"
            else:
                checks.append(
                    (
                        "Очистка",
                        f"Исходные {len(baseline['containers'])} контейнеров, {len(baseline['running'])} работающих, сети, тома и образы сохранены. Собственные временные ресурсы удалены.",
                    )
                )
    result = "PASS" if failure is None and cleanup_ok else "FAIL"
    rows = "\n".join(f"| {name} | PASS | {detail} |" for name, detail in checks)
    args.report.write_text(
        f"# Подключение метрик и панели владельца: {result}\n\nДата начала UTC: {started}.\n\n"
        f"Образы: Grafana 12.1.0, Prometheus 3.5.0, Python экспортёра 3.13.7; отправитель `{sender}`. Python драйвера: {sys.version.split()[0]}.\n\n"
        "Рабочий архив не использовался: все контрольные данные находились только в собственных временных томах. Тест не останавливал рабочие сервисы.\n\n"
        "| Проверка | Результат | Подтверждение |\n|---|---|---|\n"
        + rows
        + "\n\n"
        + (f"Ошибка: {failure}\n\n" if failure else "")
        + "[Скрипт](onboarding.py), [руководство](../USER_MANUAL.md), [учебный пример](../examples/owner-metrics/compose.yml). Снимок браузера сохраняется рядом при успешной проверке панели.\n\n"
        "Не проверялись: WAN/межстрановая сеть, рабочая PKI/SVID и ротация, нагрузка, исчерпание диска, отказ диска, резервное восстановление, доставка логов и трасс. Loopback Chromium игнорировал только временную самоподписанную PKI; API и все контейнерные соединения проверяли CA/SAN/mTLS. Операторский драйвер ограничивает свои TLS-соединения разрешённым TLS 1.2: в исходном прогоне TLS 1.3 с его OpenSSL 3.6.5 не проходил handshake. Настройки продуктов и клиент примера Python 3.13.7 не ограничивались/не изменялись.\n"
    )
    print(f"{result}: {args.report}")
    if failure:
        print(failure)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
