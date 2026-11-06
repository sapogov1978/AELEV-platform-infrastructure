"""Real component lifecycle and browser isolation; no synthetic Kafka/IdP."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import ssl
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from playwright.sync_api import sync_playwright

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prepare import directory, settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def command(*args: str) -> str:
    result = subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=180
    )
    return result.stdout


def inventory() -> dict[str, set[str]]:
    return {
        kind: set(
            command(
                "docker", kind, "ls", "-aq" if kind == "container" else "-q"
            ).split()
        )
        for kind in ("container", "volume", "network")
    }


def request(
    url: str,
    context: ssl.SSLContext,
    method: str = "GET",
    data: Any = None,
    token: str = "",
    form: bool = False,
) -> tuple[int, Any]:
    body = (
        (urlencode(data).encode() if form else json.dumps(data).encode())
        if data is not None
        else None
    )
    headers = {
        "Content-Type": (
            "application/x-www-form-urlencoded" if form else "application/json"
        )
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urlopen(
            Request(url, data=body, method=method, headers=headers),
            context=context,
            timeout=15,
        ) as response:
            value = response.read()
            return response.status, (
                json.loads(value)
                if value and response.headers.get_content_type() == "application/json"
                else value
            )
    except HTTPError as error:
        return error.code, None


class Page(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        content = b"""<!doctype html><html><head><title>Owner fixture</title></head><body><h2>Owner page</h2><div id="context"></div><script>
        window.received=[];window.parentReadable=false;try{window.parentReadable=!!parent.document.body}catch{}
        addEventListener('message',async e=>{if(e.source===parent&&e.origin===SHELL_ORIGIN&&e.data.type==='AELEV_UI_CONTEXT'){
          received.push(e.data);document.getElementById('context').textContent=e.data.language;
          const content=await(await fetch(e.data.stylesheet_url)).arrayBuffer();
          const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',content))].map(v=>v.toString(16).padStart(2,'0')).join('');
          if(hash!==e.data.stylesheet_sha256)throw Error('style digest mismatch');
          if(!document.querySelector('link')){const link=document.createElement('link');link.rel='stylesheet';link.href=e.data.stylesheet_url;document.head.append(link);}
          window.styleVerified=true;
        }});
        parent.postMessage({type:'AELEV_UI_READY',version:1},SHELL_ORIGIN);
        </script></body></html>""".replace(
            b"SHELL_ORIGIN", json.dumps(self.server.shell_origin).encode()
        )  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", "frame-ancestors " + self.server.shell_origin)  # type: ignore[attr-defined]
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def run(args: argparse.Namespace) -> None:
    cfg = settings(args.env)
    secret = directory(cfg["SECRET_ROOT"], args.env)
    base = cfg["PUBLIC_URL"]
    keycloak = cfg["KEYCLOAK_URL"]
    issuer = keycloak + "/realms/" + cfg["KEYCLOAK_REALM"]
    compose = [
        "docker",
        "compose",
        "--env-file",
        str(args.env.resolve()),
        "-f",
        str(ROOT / "compose.yml"),
    ]
    baseline = inventory()
    if (
        command(
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=com.docker.compose.project=" + cfg["COMPOSE_PROJECT"],
        ).strip()
        or cfg["DATA_VOLUME"] in baseline["volume"]
    ):
        raise RuntimeError(
            "refusing to test over an existing deployment or its data volume"
        )
    context = ssl.create_default_context(cafile=str(secret / "nginx/ca.crt"))
    identity_context = ssl.create_default_context(
        cafile=str(secret / "provision/oidc-ca.crt")
    )
    clients: list[str] = []
    stages: list[str] = []
    server: ThreadingHTTPServer | None = None
    admin = ""
    started = time.monotonic()
    error: BaseException | None = None

    def check(condition: bool, name: str) -> None:
        if not condition:
            raise AssertionError(name)
        stages.append(name)
        print("PASS: " + name, flush=True)

    def administrative(path: str, method: str = "GET", data: Any = None) -> Any:
        status, result = request(
            keycloak + "/admin/realms/" + cfg["KEYCLOAK_REALM"] + path,
            identity_context,
            method,
            data,
            admin,
        )
        if status not in (200, 201, 204):
            raise RuntimeError("identity administration rejected: " + str(status))
        return result

    try:
        command(
            *compose,
            "--profile",
            "provision",
            "run",
            "--rm",
            "platform-ui-configuration-init",
        )
        check(True, "Настоящая одноразовая настройка OIDC без обхода")
        command(
            *compose,
            "--profile",
            "provision",
            "run",
            "--rm",
            "platform-ui-configuration-init",
        )
        check(True, "Повторная настройка идемпотентна")
        command(*compose, "up", "-d", "--wait", "--wait-timeout", "90")
        check(
            request(base + "/api/readyz", context)[0] == 200,
            "HTTPS и готовность настоящего API",
        )
        check(
            request(base + "/api/modules", context)[0] == 401,
            "API не допускает анонимное чтение",
        )
        status, access = request(
            keycloak + "/realms/master/protocol/openid-connect/token",
            identity_context,
            "POST",
            {
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": cfg["PROVISION_ADMIN_USER"],
                "password": (secret / "provision/admin-password").read_text().strip(),
            },
            form=True,
        )
        check(status == 200, "Действительная административная identity Keycloak")
        admin = access["access_token"]
        role = administrative("/roles/platform-ui-publisher")
        tokens = []
        for _ in range(2):
            client_name = "ui-test-" + str(uuid4())
            password = secrets.token_urlsafe(32)
            description = {
                "clientId": client_name,
                "secret": password,
                "enabled": True,
                "publicClient": False,
                "serviceAccountsEnabled": True,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "protocolMappers": [
                    {
                        "name": "ui-audience",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-audience-mapper",
                        "config": {
                            "included.client.audience": cfg["KEYCLOAK_CLIENT_ID"],
                            "access.token.claim": "true",
                        },
                    }
                ],
            }
            administrative("/clients", "POST", description)
            client_id = administrative(
                "/clients?" + urlencode({"clientId": client_name})
            )[0]["id"]
            clients.append(client_id)
            user_id = administrative("/clients/" + client_id + "/service-account-user")[
                "id"
            ]
            administrative("/users/" + user_id + "/role-mappings/realm", "POST", [role])
            status, token = request(
                issuer + "/protocol/openid-connect/token",
                identity_context,
                "POST",
                {
                    "grant_type": "client_credentials",
                    "client_id": client_name,
                    "client_secret": password,
                },
                form=True,
            )
            check(status == 200, "Настоящий JWT отдельного временного издателя")
            tokens.append(token["access_token"])
        with tempfile.TemporaryDirectory(prefix="aelev-platform-ui-test-") as temporary:
            tmp = Path(temporary)
            # Certificate creation is test-local; publisher issuer keys never enter runtime containers.
            command(
                "openssl",
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                "/CN=temporary-publisher",
                "-keyout",
                str(tmp / "client.key"),
                "-out",
                str(tmp / "request.csr"),
            )
            (tmp / "extensions").write_text(
                "basicConstraints=critical,CA:FALSE\nextendedKeyUsage=clientAuth\nkeyUsage=digitalSignature\n"
            )
            command(
                "openssl",
                "x509",
                "-req",
                "-days",
                "1",
                "-in",
                str(tmp / "request.csr"),
                "-CA",
                str(secret / "issuer/publisher.crt"),
                "-CAkey",
                str(secret / "issuer/publisher.key"),
                "-set_serial",
                str(int.from_bytes(secrets.token_bytes(12), "big")),
                "-extfile",
                str(tmp / "extensions"),
                "-out",
                str(tmp / "client.crt"),
            )
            publisher = ssl.create_default_context(cafile=str(secret / "nginx/ca.crt"))
            publisher.load_cert_chain(str(tmp / "client.crt"), str(tmp / "client.key"))
            server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
            server.shell_origin = base  # type: ignore[attr-defined]
            server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_tls.load_cert_chain(
                str(secret / "nginx/server.crt"), str(secret / "nginx/server.key")
            )
            server.socket = server_tls.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            module_id = str(uuid4())
            module = {
                "schema_version": 1,
                "service_kind": "FUNCTIONAL",
                "module_id": module_id,
                "revision": 1,
                "title": {"ru": "Тестовый владелец", "en": "Test owner"},
                "sections": [
                    {
                        "id": "page",
                        "title": {"ru": "Тестовая страница", "en": "Test page"},
                        "url": f"https://127.0.0.1:{server.server_port}/",
                    }
                ],
            }
            path = base + "/api/modules/" + module_id
            check(
                request(path, context, "PUT", module, tokens[0])[0] == 403,
                "JWT без mTLS не разрешает публикацию",
            )
            check(
                request(path, publisher, "PUT", module, "invalid.jwt.value")[0] == 401,
                "mTLS без действительного JWT не разрешает публикацию",
            )
            check(
                request(path, publisher, "PUT", module, tokens[0])[0] == 200,
                "Инъекция произвольного описания владельцем",
            )
            check(
                request(path, publisher, "PUT", module, tokens[0])[0] == 200,
                "Повтор описания идемпотентен",
            )
            check(
                request(path, publisher, "PUT", module, tokens[1])[0] == 403,
                "Другой издатель не может менять модуль",
            )
            changed = {**module, "title": {"ru": "Конфликт", "en": "Conflict"}}
            check(
                request(path, publisher, "PUT", changed, tokens[0])[0] == 409,
                "Конфликт ревизии отклоняется",
            )
            state = {
                "sequence": 1,
                "state": "ONLINE",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
            check(
                request(path + "/status", publisher, "PUT", state, tokens[0])[0] == 200,
                "Состояние публикует владелец",
            )
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=str(args.chromium), headless=True
                )
                try:
                    browser_context = browser.new_context(
                        ignore_https_errors=True,
                        viewport={"width": 1440, "height": 1000},
                    )
                    page = browser_context.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda _: errors.append("browser error"))
                    page.goto(base)
                    page.locator("#username").fill(cfg["OPERATOR_USER"])
                    page.locator("#password").fill(
                        (secret / "provision/operator-password").read_text().strip()
                    )
                    page.locator("#kc-login").click()
                    page.get_by_role("heading", name="Состояние сервисов").wait_for()
                    page.get_by_text("Доступен", exact=True).wait_for()
                    check(True, "Настоящий вход оператора: Authorization Code с PKCE")
                    page.get_by_role("button", name="Тестовая страница").click()
                    owner_frame = page.frame_locator("iframe")
                    owner_frame.get_by_role("heading", name="Owner page").wait_for()
                    owner_frame.locator("#context").filter(has_text="ru").wait_for()
                    current = next(
                        frame for frame in page.frames if frame != page.main_frame
                    )
                    messages = current.evaluate("window.received")
                    check(
                        bool(messages)
                        and all(
                            set(message)
                            == {
                                "type",
                                "version",
                                "language",
                                "stylesheet_url",
                                "stylesheet_sha256",
                            }
                            for message in messages
                        ),
                        "В iframe передаётся только оформление без токенов",
                    )
                    check(
                        current.evaluate("window.parentReadable") is False,
                        "UI владельца не имеет доступа к DOM оболочки",
                    )
                    check(
                        messages[-1]["stylesheet_sha256"]
                        == hashlib.sha256(
                            (ROOT / "public/presentation/v6.css").read_bytes()
                        ).hexdigest(),
                        "Версия оформления проверена по SHA-256",
                    )
                    current.wait_for_function(
                        "window.styleVerified===true && getComputedStyle(document.body).backgroundColor==='rgba(0, 0, 0, 0)' && getComputedStyle(document.documentElement).getPropertyValue('--aelev-panel').trim()==='#0c1f18'"
                    )
                    check(
                        True,
                        "Владелец подключил общий CSS после проверки точных байтов",
                    )
                    page.get_by_role("button", name="EN", exact=True).click()
                    owner_frame.locator("#context").filter(has_text="en").wait_for()
                    check(True, "Переключение языка доставляется владельцу")
                    page.get_by_role("button", name="Status", exact=True).click()
                    command(*compose, "stop", "platform-ui", "platform-ui-api")
                    command(*compose, "up", "-d", "--wait", "--wait-timeout", "90")
                    page.reload()
                    page.get_by_text("Доступен", exact=True).wait_for()
                    check(True, "Полный перезапуск сохраняет описание и состояние")
                    state = {
                        "sequence": 2,
                        "state": "OFFLINE",
                        "observed_at": (
                            datetime.now(timezone.utc)
                            - timedelta(seconds=int(cfg["STATUS_TTL_SECONDS"]) + 5)
                        ).isoformat(),
                    }
                    check(
                        request(path + "/status", publisher, "PUT", state, tokens[0])[0]
                        == 200,
                        "Просроченное состояние принимается с исходным временем",
                    )
                    page.reload()
                    page.get_by_text("Нет данных", exact=True).wait_for()
                    check(True, "Просроченное состояние UNKNOWN, не OFFLINE")
                    check(not errors, "В браузере нет ошибок исполнения")
                    page.screenshot(
                        path=str(ROOT / "test/browser-result.png"), full_page=True
                    )
                    page.get_by_role("button", name="Выход", exact=True).click()
                    page.locator("#username").wait_for()
                    check(True, "Выход завершает операторскую сессию")
                finally:
                    browser.close()
            check(
                request(path, publisher, "DELETE", token=tokens[0])[0] == 200,
                "Отзыв модулей доступен владельцу",
            )
    except BaseException as failure:
        error = failure
    finally:
        if server:
            server.shutdown()
            server.server_close()
        for client_id in clients:
            try:
                administrative("/clients/" + client_id, "DELETE")
            except Exception as failure:
                error = error or failure
        try:
            command(*compose, "down", "--volumes", "--remove-orphans")
        except Exception as failure:
            error = error or failure
        after = inventory()
        clean = baseline == after
        if clean:
            stages.append("Опись Docker совпала с исходной; временные identity удалены")
        else:
            error = error or RuntimeError("Docker inventory differs after cleanup")
        report = (
            "# Platform UI — проверка жизненного цикла\n\n"
            + f"Дата: {datetime.now(timezone.utc).isoformat()}. Итог: {'FAIL' if error else 'PASS'}. Длительность: {time.monotonic() - started:.1f} с.\n\nИспользованы канонические образы оболочки/API и работающий настоящий инфраструктурный Keycloak. Временная HTTPS-страница проверяет технический протокол неизвестного владельца, не подменяет инфраструктурный компонент. В браузере разрешена только локальная тестовая PKI; API/JWKS/mTLS проверяют доверие без обхода.\n\n"
            + "\n".join("- PASS: " + stage for stage in stages)
            + "\n\n"
            + f"Браузерный драйвер: Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}, Playwright 1.56.0; runtime API и модульные проверки — Python 3.13 в закреплённом образе.\n\n"
            + ("Ошибка: " + type(error).__name__ + ".\n\n" if error else "")
            + "Тест отказался бы работать поверх существующей поставки оболочки или её данных. Исходный Keycloak и его постоянные роли/клиент/оператор сохранены; временные OAuth-клиенты и сертификат удалены, тестовый том с метаданными удалён. Образы, рабочие настройки и сертификаты не являются временными данными теста. Production PKI, WAN и общая оркестрация этим прогоном не подтверждаются.\n"
        )
        (ROOT / "test/lifecycle-result.md").write_text(report)
    if error:
        raise error


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--chromium", type=Path, required=True)
    run(parser.parse_args())
