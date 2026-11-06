"""Real IdP/UI onboarding with canonical dependency models and isolated storage."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
import ipaddress
import json
import os
import secrets
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from lifecycle import NATIVE, OWNER, PREFIX, free_port, inventory, project, run

sys.path.insert(0, str(OWNER))
import prepare  # noqa: E402


def env(path: Path) -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    }


def model(owner: Path, values: dict[str, str], token: str) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env") as settings:
        settings.write("\n".join(key + "=" + value for key, value in values.items()))
        settings.flush()
        return json.loads(
            run(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    settings.name,
                    "-p",
                    token,
                    "-f",
                    str(owner / "compose.yml"),
                    "--profile",
                    "*",
                    "config",
                    "--format",
                    "json",
                ],
                environment=os.environ | values,
                directory=owner,
            )
        )


class LoginForm(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = dict(attrs)
        if tag == "form" and fields.get("id") == "kc-form-login":
            self.action = fields.get("action") or ""


class SwaggerTags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = dict(attrs)
        if tag == "h3" and "opblock-tag" in (fields.get("class") or "").split():
            self.tags.append(fields.get("data-tag") or "")


def render_swagger(chromium: Path, port: int, certificate: Path, scratch: Path) -> None:
    """Pin only the temporary server key in an isolated browser test profile."""
    public = subprocess.run(
        ["openssl", "x509", "-in", str(certificate), "-pubkey", "-noout"],
        capture_output=True,
        check=True,
    ).stdout
    der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-outform", "DER"],
        input=public,
        capture_output=True,
        check=True,
    ).stdout
    fingerprint = base64.b64encode(hashlib.sha256(der).digest()).decode()
    arguments = [
        str(chromium),
        "--headless",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--disable-default-apps",
        "--user-data-dir=" + str(scratch / "browser-profile"),
        "--ignore-certificate-errors-spki-list=" + fingerprint,
        "--virtual-time-budget=5000",
        "--dump-dom",
        "--window-size=1440,1000",
        "--screenshot=" + str(scratch / "swagger.png"),
        f"https://localhost:{port}/docs",
    ]
    process = subprocess.Popen(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        try:
            output, diagnostic = process.communicate(timeout=50)
        except subprocess.TimeoutExpired as error:
            detail = (error.stderr or b"").decode(errors="replace")
            raise RuntimeError("browser timeout: " + detail[-1000:]) from error
        if process.returncode != 0:
            raise RuntimeError("isolated browser failed: " + diagnostic[-1000:])
        rendered = SwaggerTags()
        rendered.feed(output)
        assert rendered.tags == [
            "default",
            "gateway",
            "metrics",
        ], "Swagger groups were not rendered"
        assert "fetch a bounded" in output.lower() and "/v1/fetch" in output
        shutil.copyfile(scratch / "swagger.png", OWNER / "test/swagger.png")
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def operator_token(identity_port: int, ui_port: int, ca: Path, password: str) -> str:
    context = ssl.create_default_context(cafile=str(ca))
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=context),
        HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    redirect = f"https://localhost:{ui_port}/"
    issuer = f"https://localhost:{identity_port}/realms/aelev/protocol/openid-connect"
    parameters = {
        "client_id": "platform-ui",
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": "openid",
        "state": secrets.token_urlsafe(24),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    with opener.open(issuer + "/auth?" + urlencode(parameters), timeout=30) as page:
        form = LoginForm()
        form.feed(page.read().decode())
    if not form.action:
        raise RuntimeError("native login form was not returned")
    with opener.open(
        Request(
            form.action,
            data=urlencode(
                {"username": "platform-operator", "password": password}
            ).encode(),
        ),
        timeout=30,
    ) as reply:
        result = parse_qs(urlsplit(str(reply.geturl())).query)
        if result.get("state") != [parameters["state"]] or "code" not in result:
            raise RuntimeError(
                "OIDC login did not return the matching authorization code"
            )
    with opener.open(
        Request(
            issuer + "/token",
            data=urlencode(
                {
                    "grant_type": "authorization_code",
                    "client_id": "platform-ui",
                    "code": result["code"][0],
                    "redirect_uri": redirect,
                    "code_verifier": verifier,
                }
            ).encode(),
        ),
        timeout=30,
    ) as response:
        return str(json.loads(response.read())["access_token"])


def modules(port: int, ca: Path, token: str) -> list[dict[str, Any]]:
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context(cafile=str(ca))),
    )
    with opener.open(
        Request(
            f"https://localhost:{port}/api/modules",
            headers={"Authorization": "Bearer " + token},
        ),
        timeout=20,
    ) as response:
        value: Any = json.loads(response.read())
        return cast(
            list[dict[str, Any]], value["modules"] if isinstance(value, dict) else value
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--infrastructure",
        type=Path,
        default=OWNER.parent,
        help="prepared independent Keycloak/UI delivery directories",
    )
    parser.add_argument(
        "--chromium",
        type=Path,
        help="existing Chromium/Chrome executable for a real Swagger DOM check",
    )
    parser.add_argument(
        "--allow-local-certificate",
        action="store_true",
        help=(
            "explicitly allow only the temporary server SPKI "
            "in an isolated browser profile"
        ),
    )
    args = parser.parse_args()
    if args.chromium is not None and (
        not args.allow_local_certificate or not args.chromium.is_file()
    ):
        parser.error(
            "the browser test needs an existing executable "
            "and explicit local certificate permission"
        )
    before = {
        kind: inventory(kind) for kind in ("container", "network", "volume", "image")
    }
    token = "aelev-egress-status-" + uuid.uuid4().hex[:12]
    checks: list[str] = []
    failure: str | None = None
    image_tags = {"aelev/egress-check:" + token, "aelev/egress-status-check:" + token}
    with tempfile.TemporaryDirectory(prefix=token + "-") as scratch:
        root = Path(scratch)
        idp, ui = args.infrastructure / "keycloak", args.infrastructure / "platform-ui"
        idp_port, ui_port = free_port(), free_port()
        prepare.initialize(root / "pki", "public-egress-gateway", None)
        prepare.issue(
            root / "pki/local-ca",
            root / "idp-identity",
            "server",
            "DNS:localhost,DNS:host.docker.internal,DNS:keycloak,IP:127.0.0.1",
            "serverAuth",
        )
        admin_password, database_password = secrets.token_urlsafe(
            32
        ), secrets.token_urlsafe(32)
        (root / "admin-password").write_text(admin_password)
        (root / "admin-password").chmod(0o600)
        kp = "AELEV_INFRASTRUCTURE_KEYCLOAK_"
        kv = env(idp / ".env.example") | {
            kp + "COMPOSE_PROJECT": token,
            kp + "ADMIN_PASSWORD": admin_password,
            kp + "DATABASE_PASSWORD": database_password,
            kp + "BIND": "127.0.0.1",
            kp + "HTTPS_PORT": str(idp_port),
            kp + "PUBLIC_URL": f"https://localhost:{idp_port}",
            kp + "ADMIN_ENDPOINT": f"https://localhost:{idp_port}",
            kp + "DATABASE_VOLUME": token + "-idp-data",
            kp + "NETWORK": token + "-idp-backend",
            kp + "INGRESS_NETWORK": token + "-idp-ingress",
            kp + "MEMORY_LIMIT": "1g",
            kp + "DATABASE_MEMORY_LIMIT": "256m",
        }
        km = model(idp, kv, token)
        km["services"]["keycloak"]["group_add"] = [str(os.getgid())]
        for mount in km["services"]["keycloak"]["volumes"]:
            if mount["target"].endswith("tls.crt"):
                mount["source"] = str(root / "idp-identity/server.pem")
            if mount["target"].endswith("tls.key"):
                mount["source"] = str(root / "idp-identity/server.key")
        up = "AELEV_INFRASTRUCTURE_PLATFORM_UI_"
        uv = env(ui / ".env.example") | {
            up + "COMPOSE_PROJECT": token,
            up + "BIND": "127.0.0.1",
            up + "PORT": str(ui_port),
            up + "PUBLIC_URL": f"https://localhost:{ui_port}",
            up + "KEYCLOAK_URL": f"https://localhost:{idp_port}",
            up + "KEYCLOAK_BACKCHANNEL_URL": f"https://host.docker.internal:{idp_port}",
            up + "SECRET_ROOT": str(root / "ui-secrets"),
            up + "SECRET_GID": str(os.getgid()),
            up + "RUNTIME_ROOT": str(root / "ui-runtime"),
            up + "BACKEND_NETWORK": token + "-ui-backend",
            up + "EGRESS_NETWORK": token + "-ui-egress",
            up + "DATA_VOLUME": token + "-ui-data",
            up + "STATUS_TTL_SECONDS": "8",
        }
        settings = root / "ui.env"
        settings.write_text("\n".join(key + "=" + value for key, value in uv.items()))
        settings.chmod(0o600)
        run(
            [
                sys.executable,
                str(ui / "prepare.py"),
                "development-pki",
                "--env",
                str(settings),
                "--identity-ca",
                str(root / "pki/local-ca/ca.pem"),
                "--admin-password-file",
                str(root / "admin-password"),
            ]
        )
        run([sys.executable, str(ui / "prepare.py"), "render", "--env", str(settings)])
        um = model(ui, uv, token)
        (root / "grants").mkdir()
        status_root, status_admin = root / "status", root / "status-admin"
        status_admin.mkdir(mode=0o750)
        issuer = root / "publisher-issuer"
        issuer.mkdir(mode=0o700)
        shutil.copyfile(root / "ui-secrets/issuer/publisher.crt", issuer / "ca.pem")
        shutil.copyfile(root / "ui-secrets/issuer/publisher.key", issuer / "ca.key")
        prepare.issue(
            issuer,
            status_root,
            "publisher",
            "URI:spiffe://example.test/infrastructure/gateway-status",
            "clientAuth",
        )
        (status_root / "publisher.pem").rename(status_root / "publisher.crt")
        shutil.copyfile(root / "ui-secrets/nginx/ca.crt", status_root / "shell-ca.crt")
        shutil.copyfile(root / "pki/local-ca/ca.pem", status_root / "identity-ca.crt")
        (status_admin / "admin-user").write_text("admin")
        (status_admin / "admin-password").write_text(admin_password)
        for path in status_admin.iterdir():
            path.chmod(0o640)
        module_id = str(uuid.uuid4())
        gateway_port, docs_port = free_port(), free_port()
        gv = env(OWNER / ".env.example") | {
            PREFIX + "COMPOSE_PROJECT": token,
            PREFIX + "IMAGE": "aelev/egress-check:" + token,
            PREFIX + "UI_IMAGE": "aelev/egress-status-check:" + token,
            PREFIX + "PORT": str(gateway_port),
            PREFIX + "PUBLIC_URL": f"https://localhost:{gateway_port}",
            PREFIX + "DOCS_PORT": str(docs_port),
            PREFIX + "DOCS_PUBLIC_URL": f"https://localhost:{docs_port}",
            PREFIX + "SECRET_ROOT": str(root / "pki/gateway"),
            PREFIX + "SECRET_GID": str(os.getgid()),
            PREFIX + "GRANTS_ROOT": str(root / "grants"),
            PREFIX + "NETWORK": token + "-access",
            PREFIX + "PUBLIC_NETWORK": token + "-public",
            PREFIX + "UI_EGRESS_NETWORK": token + "-status-egress",
            PREFIX + "UI_STATE_VOLUME": token + "-outbox",
            PREFIX + "UI_URL": f"https://host.docker.internal:{ui_port}",
            PREFIX + "UI_IDENTITY_URL": f"https://host.docker.internal:{idp_port}",
            PREFIX + "UI_MODULE_ID": module_id,
            PREFIX + "UI_SECRET_ROOT": str(status_root),
            PREFIX + "UI_SECRET_GID": str(os.getgid()),
            PREFIX + "UI_ADMIN_ROOT": str(status_admin),
            PREFIX + "UI_PROVISION_UID": str(os.getuid()),
            PREFIX + "UI_INTERVAL_SECONDS": "1",
        }
        gm = model(OWNER, gv, token)
        chosen = {
            "keycloak",
            "keycloak-database",
            "platform-ui",
            "platform-ui-api",
            "platform-ui-configuration-init",
            NATIVE,
            "public-egress-gateway-status-provision",
            "public-egress-gateway-status-publisher",
        }
        combined: dict[str, Any] = {
            "name": token,
            "services": {},
            "networks": {},
            "volumes": {},
        }
        for document in (km, um, gm):
            for name, service in document["services"].items():
                if name in chosen:
                    combined["services"][name] = service
            combined["networks"].update(document.get("networks", {}))
            combined["volumes"].update(document.get("volumes", {}))
        used_networks = {
            name
            for service in combined["services"].values()
            for name in service.get("networks", {})
        }
        combined["networks"] = {
            name: value
            for name, value in combined["networks"].items()
            if name in used_networks
        }
        occupied = [
            ipaddress.ip_network(entry["Subnet"])
            for row in before["network"].values()
            for entry in cast(
                list[dict[str, str]],
                cast(dict[str, Any], row.get("IPAM") or {}).get("Config") or [],
            )
            if entry.get("Subnet")
        ]
        available = [
            subnet
            for subnet in ipaddress.ip_network("10.254.240.0/20").subnets(new_prefix=24)
            if not any(
                subnet.version == existing.version and subnet.overlaps(existing)
                for existing in occupied
            )
        ]
        if len(available) < len(combined["networks"]):
            raise RuntimeError(
                "isolated subnet range is occupied; do not change the Docker daemon"
            )
        for index, network in enumerate(combined["networks"].values()):
            network["ipam"] = {"config": [{"subnet": str(available[index])}]}
        # Never allocate unused canonical volumes from another role's template.
        used_volumes = {
            mount["source"]
            for service in combined["services"].values()
            for mount in service.get("volumes", [])
            if mount["type"] == "volume"
        }
        combined["volumes"] = {
            name: value
            for name, value in combined["volumes"].items()
            if name in used_volumes
        }
        path = root / "canonical-models.json"
        path.write_text(json.dumps(combined))
        path.chmod(0o600)
        compose = ["docker", "compose", "-p", token, "-f", str(path)]
        try:
            run(compose + ["--profile", "*", "config", "--quiet"])
            run(
                compose
                + [
                    "--profile",
                    "ui",
                    "build",
                    NATIVE,
                    "public-egress-gateway-status-publisher",
                ]
            )
            run(
                compose
                + [
                    "up",
                    "-d",
                    "--no-build",
                    "--pull",
                    "never",
                    "--wait",
                    "--wait-timeout",
                    "180",
                    "keycloak",
                ]
            )
            run(
                compose
                + [
                    "--profile",
                    "provision",
                    "run",
                    "--rm",
                    "platform-ui-configuration-init",
                ]
            )
            run(
                compose
                + [
                    "up",
                    "-d",
                    "--no-build",
                    "--pull",
                    "never",
                    "--wait",
                    "platform-ui",
                    NATIVE,
                ]
            )
            checks.append(
                "Реальные канонические Keycloak/PostgreSQL/UI/API "
                "в отдельном проекте и томах; рабочая инфраструктура "
                "не используется для тестовых записей."
            )
            run(
                compose
                + [
                    "--profile",
                    "provision",
                    "run",
                    "--rm",
                    "public-egress-gateway-status-provision",
                ]
            )
            first_secret = hashlib.sha256(
                (status_root / "client-secret").read_bytes()
            ).hexdigest()
            run(
                compose
                + [
                    "--profile",
                    "provision",
                    "run",
                    "--rm",
                    "public-egress-gateway-status-provision",
                ]
            )
            assert (
                hashlib.sha256((status_root / "client-secret").read_bytes()).hexdigest()
                == first_secret
            )
            checks.append(
                "Настоящая собственная одноразовая подготовка identity: два exit 0, "
                "идемпотентный secret, без административных файлов в издателе."
            )
            trust = root / "reader-ca.pem"
            trust.write_bytes(
                (root / "pki/local-ca/ca.pem").read_bytes()
                + (root / "ui-secrets/nginx/ca.crt").read_bytes()
            )
            reader = operator_token(
                idp_port,
                ui_port,
                trust,
                (root / "ui-secrets/provision/operator-password").read_text().strip(),
            )
            run(
                compose
                + [
                    "--profile",
                    "ui",
                    "up",
                    "-d",
                    "--no-build",
                    "--pull",
                    "never",
                    "public-egress-gateway-status-publisher",
                ]
            )

            def wait_for(expected: bool | None) -> dict[str, Any] | None:
                end = time.monotonic() + 45
                while time.monotonic() < end:
                    own = next(
                        (
                            row
                            for row in modules(ui_port, trust, reader)
                            if row["module_id"] == module_id
                        ),
                        None,
                    )
                    if expected is None and own is None:
                        return None
                    if own is not None:
                        states = {
                            item["id"]: item["state"]
                            for item in own["status"].get("components", [])
                        }
                        if states.get("publisher") == "ONLINE" and (
                            (states.get("gateway") == "ONLINE") is expected
                        ):
                            return own
                    time.sleep(0.5)
                raise AssertionError("expected acknowledged status was not observed")

            initial = wait_for(True)
            assert (
                initial is not None
                and initial["service_kind"] == "INFRASTRUCTURE"
                and initial["sections"] == []
            )
            checks.append(
                "mTLS/JWT-публикация принята реальной оболочкой; "
                "две составные строки ONLINE, без функционального раздела."
            )
            assert initial is not None
            expected_url = f"https://localhost:{docs_port}/docs"
            assert initial["swagger_url"] == expected_url
            components = {item["id"]: item for item in initial["components"]}
            assert components["gateway"]["swagger_url"] == expected_url
            assert not components["publisher"].get("swagger_url")
            checks.append(
                "Реальный UI принял Swagger-ссылку владельца и gateway-компонента; "
                "адрес задан для браузера, не внутренним Docker DNS."
            )
            if args.chromium is not None:
                render_swagger(
                    args.chromium, docs_port, root / "pki/gateway/server.pem", root
                )
                checks.append(
                    "Настоящий Chromium отрисовал Swagger: default → gateway → "
                    "metrics и /v1/fetch; без клиентского сертификата и CDN. "
                    "Временный профиль доверял только ключу тестового сервера, "
                    "системное доверие не изменялось."
                )
            run(compose + ["stop", NATIVE])
            wait_for(False)
            run(
                compose
                + ["up", "-d", "--no-build", "--pull", "never", "--wait", NATIVE]
            )
            restored = wait_for(True)
            assert (
                restored is not None
                and restored["status"]["sequence"] > initial["status"]["sequence"]
            )
            checks.append(
                "При остановке gateway его компонент перестаёт быть ONLINE, "
                "издатель остаётся ONLINE; восстановление получает "
                "новую подтверждённую последовательность."
            )
            run(compose + ["stop", "public-egress-gateway-status-publisher"])
            wait_for(None)
            checks.append(
                "Штатная остановка издателя подтверждает отписку: "
                "строка исчезает из динамического списка."
            )
        except (
            AssertionError,
            OSError,
            RuntimeError,
            ValueError,
            subprocess.SubprocessError,
        ) as error:
            failure = type(error).__name__ + ": " + str(error)[:180]
            diagnostic = run(
                compose
                + [
                    "--profile",
                    "*",
                    "logs",
                    "--no-color",
                    "--tail",
                    "30",
                    "public-egress-gateway-status-publisher",
                    "public-egress-gateway-status-provision",
                    "platform-ui-api",
                ]
            )
            for secret in (
                admin_password,
                database_password,
                (root / "ui-secrets/provision/operator-password").read_text().strip(),
            ):
                diagnostic = diagnostic.replace(secret, "[REDACTED]")
            if (status_root / "client-secret").exists():
                diagnostic = diagnostic.replace(
                    (status_root / "client-secret").read_text().strip(), "[REDACTED]"
                )
            checks.append(
                "Диагностика временного стенда: "
                + diagnostic[-3500:].replace("\n", " | ").strip()
            )
        finally:
            for identifier, row in inventory("container").items():
                if (
                    identifier not in before["container"]
                    and project(row, "container") == token
                ):
                    run(["docker", "container", "stop", "-t", "5", identifier])
                    run(["docker", "container", "rm", identifier])
            for kind in ("network", "volume"):
                for identifier, row in inventory(kind).items():
                    if identifier not in before[kind] and project(row, kind) == token:
                        run(["docker", kind, "rm", str(row["Name"])])
            for row in inventory("image").values():
                for tag in cast(list[str], row.get("RepoTags") or []):
                    if tag in image_tags:
                        run(["docker", "image", "rm", tag])
            for kind, original in before.items():
                current = inventory(kind)
                if not set(original).issubset(current):
                    failure = (failure + "; " if failure else "") + (
                        "preexisting " + kind + " resource removed"
                    )
                if kind == "container":
                    for identifier, row in original.items():
                        if identifier in current and (
                            current[identifier]["State"]["Status"]
                            != row["State"]["Status"]
                        ):
                            failure = (failure + "; " if failure else "") + (
                                "preexisting container state changed"
                            )
            checks.append(
                "Тестовые контейнеры, сети, тома, образы, секреты и UI/IdP записи "
                "удалены вместе с изолированным стендом; рабочий архив не изменялся."
            )
    report = [
        "# Public Egress Gateway — профиль состояния",
        "",
        "UTC: " + datetime.now(UTC).isoformat(),
        "",
        "Результат: " + ("FAIL" if failure else "PASS"),
        "",
    ] + ["- " + item for item in checks]
    report += [
        "",
        "WAN, SPIRE-ротация, защищённый аудит и аппаратная MFA "
        "этим сценарием не проверялись.",
    ]
    if args.chromium is None:
        report += ["", "Браузерный этап не запускался: --chromium не указан."]
    if failure:
        report += ["", "Ошибка: " + failure]
    (OWNER / "test/status-result.md").write_text("\n".join(report) + "\n")
    if failure:
        raise SystemExit(failure)


if __name__ == "__main__":
    main()
