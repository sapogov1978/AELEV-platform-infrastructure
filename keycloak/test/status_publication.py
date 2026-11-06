"""Verify the real owner's publication and visible row; create no test identities."""

from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import HTTPSHandler, ProxyHandler, build_opener

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from status_publisher import PREFIX, Publisher, Settings, request  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui-url", required=True)
    parser.add_argument("--viewer-user", required=True)
    parser.add_argument("--viewer-password-file", type=Path, required=True)
    parser.add_argument("--chromium", type=Path, required=True)
    parser.add_argument("--restart-publisher", action="store_true")
    parser.add_argument("--component-lifecycle", action="store_true")
    parser.add_argument("--status-ttl-seconds", type=int)
    args = parser.parse_args()
    if args.status_ttl_seconds is not None and args.status_ttl_seconds <= 0:
        parser.error("positive status TTL required")
    if args.component_lifecycle and args.status_ttl_seconds is None:
        parser.error("--component-lifecycle requires --status-ttl-seconds")
    if args.component_lifecycle or args.restart_publisher:
        snapshot = subprocess.run(
            ["docker", "compose", "--env-file", ".env", "ps", "--format", "json"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        running = {
            item["Service"]
            for line in snapshot.stdout.splitlines()
            if (item := json.loads(line)).get("State") == "running"
        }
        if (
            not {"keycloak", "keycloak-database", "keycloak-status-publisher"}
            <= running
        ):
            raise RuntimeError(
                "lifecycle requires all three original containers running"
            )
    for line in (ROOT / ".env").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.startswith(PREFIX):
            os.environ[key] = value
    cfg = Settings.environment()
    secret_root = (ROOT / os.environ[PREFIX + "UI_SECRET_ROOT"]).resolve()
    trust = ssl.create_default_context(cafile=str(secret_root / "shell-ca.crt"))
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=trust))
    with opener.open(args.ui_url + "/config.json", timeout=10) as reply:
        public = json.load(reply)
    worker = Publisher(
        replace(
            cfg,
            ui_url=args.ui_url,
            identity_url=public["keycloak_url"],
            secret_root=secret_root,
        )
    )
    token = worker.access_token()
    rows: list[str] = []
    failed = True

    def check(condition: bool, label: str) -> None:
        if not condition:
            print("FAIL: " + label, flush=True)
            raise AssertionError(label)
        rows.append(label)
        print("PASS: " + label, flush=True)

    try:
        check(
            public["poll_seconds"] == 60,
            "Конфигурация оболочки получена с проверенным HTTPS, опрос 60 с",
        )
        code, _ = request(
            worker.transport,
            args.ui_url + "/api/modules",
            10,
            headers={"Authorization": "Bearer " + token},
        )
        check(code == 403, "Издатель не получил роль просмотра")
        code, _ = request(
            trust,
            args.ui_url + "/api/modules/" + cfg.module_id,
            10,
            "PUT",
            cfg.description(),
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
                "X-Verified-Publisher": "SUCCESS",
            },
        )
        check(code == 403, "JWT и поддельный заголовок не заменяют mTLS")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(args.chromium), headless=True
            )
            page = None
            try:
                context = browser.new_context(
                    ignore_https_errors=True, viewport={"width": 1440, "height": 1000}
                )
                page = context.new_page()
                page.clock.install()
                with page.expect_response(
                    lambda response: response.url == args.ui_url + "/api/modules"
                ) as received:
                    page.goto(args.ui_url)
                    page.locator("#username").fill(args.viewer_user)
                    page.locator("#password").fill(
                        args.viewer_password_file.read_text().strip()
                    )
                    page.locator("#kc-login").click()
                check(
                    received.value.status == 200,
                    "Настоящий OIDC-вход, API принял JWT оператора",
                )

                def module(data: dict) -> dict:
                    matches = [
                        item
                        for item in data["modules"]
                        if item["module_id"] == cfg.module_id
                    ]
                    check(
                        len(matches) == 1,
                        "В настоящем ответе API один модуль этого Keycloak",
                    )
                    return matches[0]

                before = module(received.value.json())
                if args.component_lifecycle:
                    deadline = time.monotonic() + 120
                    while (
                        before["status"]["state"] != "ONLINE"
                        or {part["state"] for part in before["status"]["components"]}
                        != {"ONLINE"}
                    ) and time.monotonic() < deadline:
                        time.sleep(2)
                        response = page.request.get(
                            args.ui_url + "/api/modules",
                            headers={
                                "Authorization": received.value.request.all_headers()[
                                    "authorization"
                                ]
                            },
                        )
                        if response.status != 200:
                            raise AssertionError("initial viewer request failed")
                        before = next(
                            item
                            for item in response.json()["modules"]
                            if item["module_id"] == cfg.module_id
                        )
                    with page.expect_response(
                        lambda response: response.url == args.ui_url + "/api/modules"
                    ) as initial:
                        page.reload()
                    before = module(initial.value.json())
                check(
                    before["service_kind"] == "INFRASTRUCTURE"
                    and before["sections"] == [],
                    "Keycloak объявил инфраструктурный тип без функциональных страниц",
                )
                check(
                    before["status"]["state"] == "ONLINE"
                    and before["status"]["sequence"] > 0,
                    "API возвращает реальный ONLINE с положительной последовательностью",
                )
                check(
                    [item["id"] for item in before["components"]]
                    == ["keycloak", "database", "publisher"]
                    and {item["state"] for item in before["status"]["components"]}
                    == {"ONLINE"},
                    "Владелец объявил три компонента с отдельными подтверждёнными состояниями",
                )
                group = page.locator(f'tbody[data-module-id="{cfg.module_id}"]')
                row = group.locator("tr").first
                row.wait_for()
                check(
                    row.locator(".status-pill.online").count() == 1,
                    "Keycloak виден как доступный в странице состояния",
                )
                toggle = group.locator("button[aria-expanded]")
                expect(toggle).to_contain_text(before["title"]["ru"])
                check(
                    True, "Название группы получено из собственного описания владельца"
                )
                children = group.locator(".status-component")
                check(
                    toggle.get_attribute("aria-expanded") == "false"
                    and children.count() == 3
                    and not any(child.is_visible() for child in children.all()),
                    "Состав по умолчанию свёрнут, отдельные компоненты скрыты",
                )
                toggle.click()
                check(
                    toggle.get_attribute("aria-expanded") == "true"
                    and all(child.is_visible() for child in children.all())
                    and children.locator(".status-pill.online").count() == 3,
                    "Щелчок раскрывает Keycloak, Keycloak Database и Keycloak Status Publisher",
                )
                with page.expect_response(
                    lambda response: response.url == args.ui_url + "/api/modules"
                ):
                    page.clock.fast_forward(public["poll_seconds"] * 1000 + 1000)
                check(
                    toggle.get_attribute("aria-expanded") == "true",
                    "Настоящее повторное чтение API не сворачивает открытую группу",
                )
                toggle.click()
                check(
                    not any(child.is_visible() for child in children.all()),
                    "Повторный щелчок сворачивает состав",
                )
                toggle.focus()
                toggle.press("Enter")
                check(
                    toggle.get_attribute("aria-expanded") == "true",
                    "Группа раскрывается клавиатурой",
                )
                toggle.press("Space")
                check(
                    toggle.get_attribute("aria-expanded") == "false"
                    and not any(child.is_visible() for child in children.all()),
                    "Пробел сворачивает группу без потери управления клавиатурой",
                )
                toggle.press("Enter")
                page.get_by_role("button", name="EN", exact=True).click()
                check(
                    toggle.get_attribute("aria-expanded") == "true"
                    and children.locator(".status-pill").all_text_contents()
                    == ["Online"] * 3,
                    "Смена языка сохраняет раскрытие и переводит подписи состояния",
                )
                page.get_by_role("button", name="RU", exact=True).click()
                check(
                    page.locator(".service-navigation")
                    .get_by_text("Keycloak", exact=True)
                    .count()
                    == 0,
                    "Keycloak не добавлен в меню функциональных сервисов",
                )
                if args.component_lifecycle:
                    viewer_token = received.value.request.all_headers()["authorization"]

                    def compose(action: str, service: str) -> None:
                        subprocess.run(
                            [
                                "docker",
                                "compose",
                                "--env-file",
                                ".env",
                                action,
                                service,
                            ],
                            cwd=ROOT,
                            check=True,
                            capture_output=True,
                            timeout=90,
                        )

                    def wait_component(component_id: str, state: str) -> None:
                        print(f"WAIT: {component_id} → {state}", flush=True)
                        deadline = time.monotonic() + args.status_ttl_seconds + 90
                        while time.monotonic() < deadline:
                            response = page.request.get(
                                args.ui_url + "/api/modules",
                                headers={"Authorization": viewer_token},
                            )
                            if response.status != 200:
                                print(
                                    f"FAIL: viewer HTTP {response.status}", flush=True
                                )
                                raise AssertionError("viewer request failed")
                            value = next(
                                item
                                for item in response.json()["modules"]
                                if item["module_id"] == cfg.module_id
                            )
                            current = next(
                                item
                                for item in value["status"]["components"]
                                if item["id"] == component_id
                            )
                            if current["state"] == state:
                                # Advance polling timers without moving JWT wall time
                                # into the future while the real IdP is unavailable.
                                page.clock.set_fixed_time(datetime.now(timezone.utc))
                                with page.expect_response(
                                    lambda reply: reply.url
                                    == args.ui_url + "/api/modules"
                                ):
                                    page.clock.fast_forward(
                                        public["poll_seconds"] * 1000 + 1000
                                    )
                                if toggle.get_attribute("aria-expanded") != "true":
                                    toggle.click()
                                child = children.nth(
                                    ["keycloak", "database", "publisher"].index(
                                        component_id
                                    )
                                )
                                expect(
                                    child.locator(".status-pill." + state.lower())
                                ).to_have_count(1)
                                check(
                                    child.locator(
                                        ".status-pill." + state.lower()
                                    ).count()
                                    == 1,
                                    f"Браузер: {component_id} → {state}",
                                )
                                return
                            time.sleep(2)
                        raise AssertionError(f"no transition: {component_id} → {state}")

                    # Refresh the browser through the real OIDC flow between outages;
                    # a multi-stage test must not reuse its first short-lived token.
                    for service, component_id, stopped_state in (
                        ("keycloak", "keycloak", "UNKNOWN"),
                        ("keycloak-database", "database", "OFFLINE"),
                        ("keycloak-status-publisher", "publisher", "UNKNOWN"),
                    ):
                        page.clock.set_fixed_time(datetime.now(timezone.utc))
                        with page.expect_response(
                            lambda reply: reply.url == args.ui_url + "/api/modules"
                        ) as authenticated:
                            page.reload()
                        check(
                            authenticated.value.status == 200,
                            "Между отказами получен новый настоящий OIDC-сеанс просмотра",
                        )
                        viewer_token = authenticated.value.request.all_headers()[
                            "authorization"
                        ]
                        try:
                            compose("stop", service)
                            if component_id == "publisher":
                                response = page.request.get(
                                    args.ui_url + "/api/modules",
                                    headers={"Authorization": viewer_token},
                                )
                                check(
                                    response.status == 200
                                    and not any(
                                        item["module_id"] == cfg.module_id
                                        for item in response.json()["modules"]
                                    ),
                                    "Штатная отписка издателя убрала собственную строку",
                                )
                                with page.expect_response(
                                    lambda reply: reply.url
                                    == args.ui_url + "/api/modules"
                                ):
                                    page.clock.fast_forward(
                                        public["poll_seconds"] * 1000 + 1000
                                    )
                                check(
                                    group.count() == 0,
                                    "Отписанная строка исчезла в браузере",
                                )
                            else:
                                wait_component(component_id, stopped_state)
                        finally:
                            compose("start", service)
                        wait_component(component_id, "ONLINE")
                    check(
                        True,
                        "Три настоящих контейнера остановлены по одному и восстановлены без удаления томов",
                    )
                if args.restart_publisher:

                    def publisher_command(action: str) -> None:
                        subprocess.run(
                            [
                                "docker",
                                "compose",
                                "--env-file",
                                ".env",
                                action,
                                "keycloak-status-publisher",
                            ],
                            cwd=ROOT,
                            check=True,
                            capture_output=True,
                            timeout=90,
                        )

                    try:
                        publisher_command("stop")
                        with page.expect_response(
                            lambda response: response.url
                            == args.ui_url + "/api/modules"
                        ) as withdrawn:
                            page.reload()
                        check(
                            withdrawn.value.status == 200
                            and all(
                                item["module_id"] != cfg.module_id
                                for item in withdrawn.value.json()["modules"]
                            ),
                            "Штатная отписка Keycloak подтверждена: модуль отсутствует в API",
                        )
                        expect(
                            page.locator(
                                'tbody[data-module-id="' + cfg.module_id + '"]'
                            )
                        ).to_have_count(0)
                        check(
                            True,
                            "После штатной остановки издателя строка Keycloak исчезла из браузера",
                        )
                    finally:
                        publisher_command("start")
                    deadline = time.monotonic() + 45
                    after = before
                    while (
                        after["status"]["sequence"] <= before["status"]["sequence"]
                        and time.monotonic() < deadline
                    ):
                        with page.expect_response(
                            lambda response: response.url
                            == args.ui_url + "/api/modules"
                        ) as refreshed:
                            page.reload()
                        matches = [
                            item
                            for item in refreshed.value.json()["modules"]
                            if item["module_id"] == cfg.module_id
                        ]
                        if matches:
                            after = matches[0]
                        if after["status"]["sequence"] <= before["status"]["sequence"]:
                            page.wait_for_timeout(500)
                    check(
                        after["status"]["sequence"] > before["status"]["sequence"],
                        "После рестарта только издателя sequence увеличилась, UUID сохранился",
                    )
                    check(
                        after["status"]["state"] == "ONLINE",
                        "После рестарта издателя Keycloak продолжает присылать ONLINE",
                    )
                    check(
                        after["revision"] == before["revision"]
                        and after["status"]["connection"] == "CONNECTED",
                        "Возврат Keycloak сохранил ревизию и восстановил подтверждённую связь",
                    )
                row.wait_for()
                if toggle.get_attribute("aria-expanded") != "true":
                    toggle.click()
                page.screenshot(
                    path=str(ROOT / "test/status-publication.png"), full_page=True
                )
                check(
                    True,
                    "Сохранён снимок настоящей страницы, без подмены ответа или фиктивных модулей",
                )
            finally:
                try:
                    if page is not None and not page.is_closed():
                        logout = page.get_by_role("button", name="Выход", exact=True)
                        if logout.count():
                            logout.click(timeout=10000)
                            page.locator("#username").wait_for(timeout=10000)
                            check(True, "Собственная тестовая OIDC-сессия завершена")
                finally:
                    browser.close()
        failed = False
    finally:
        report = "# Keycloak — публикация в состояние платформы\n\n"
        report += f"Дата: {datetime.now(timezone.utc).isoformat()}. Итог: {'FAIL' if failed else 'PASS'}.\n\n"
        if args.component_lifecycle:
            report += f"Режим: `--component-lifecycle --status-ttl-seconds {args.status_ttl_seconds}`. Срок свежести проверяется настоящим сервером и реальным временем, не браузерными часами. Между отказами JWT получается через настоящий OIDC-вход.\n\n"
        report += "\n".join("- PASS: " + row for row in rows) + "\n\n"
        report += "Использованы реальные Keycloak, PostgreSQL и platform-ui. Ответы API и страницы не подменялись. Проверки не создают и не удаляют модуль или identity; остаётся обычный рабочий издатель с собственной регистрацией. "
        report += (
            "Включены реальные stop/start Keycloak, его БД и издателя; каждый остановленный компонент запускается в finally, постоянные тома не удаляются. "
            if args.component_lifecycle
            else "Keycloak и БД не останавливались; рестарт затрагивает только издателя при явном флаге. "
        )
        report += "Оболочка не останавливалась. Браузер закрыт. Секреты/токены не сохранены в отчёт.\n\n"
        report += "Для локальной PKI браузерный контекст допускает тестовый сертификат; независимые HTTPS-запросы проверяют CA/SAN. Это не допуск внешней production PKI или WAN.\n\n"
        report += "[Снимок состояния](status-publication.png). Модульные проверки устойчивости описаны в [unit-status-result.md](unit-status-result.md).\n"
        (ROOT / "test/status-publication-result.md").write_text(report)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Publication test failed: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
