"""Check the shared indicator in the live shell without changing stored modules."""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

from playwright.sync_api import Route, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prepare import directory, settings  # noqa: E402
from lifecycle import Page  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chromium", type=Path, required=True)
    args = parser.parse_args()
    cfg = settings(ROOT / ".env")
    secret = directory(cfg["SECRET_ROOT"], ROOT / ".env")
    base = cfg["PUBLIC_URL"]
    rows: list[str] = []
    failed = False

    def check(condition: bool, label: str) -> None:
        if not condition:
            raise AssertionError(label)
        rows.append(label)
        print("PASS: " + label, flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    server.shell_origin = base  # type: ignore[attr-defined]
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(
        str(secret / "nginx/server.crt"), str(secret / "nginx/server.key")
    )
    server.socket = server_tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        tls = ssl.create_default_context(cafile=str(secret / "nginx/ca.crt"))
        with urlopen(base + "/config.json", context=tls, timeout=10) as response:
            public = json.load(response)
        check(public["poll_seconds"] == 60, "HTTPS runtime задаёт минутный интервал")
        module = {
            "schema_version": 1,
            "service_kind": "FUNCTIONAL",
            "module_id": str(uuid4()),
            "revision": 1,
            "title": {"ru": "Произвольный источник", "en": "Arbitrary source"},
            "sections": [
                {
                    "id": name,
                    "title": {"ru": title, "en": name},
                    "url": f"https://127.0.0.1:{server.server_port}/{name}",
                }
                for name, title in (
                    ("first", "Первый раздел"),
                    ("second", "Другой раздел"),
                )
            ],
            "status": {
                "state": "UNKNOWN",
                "observed_at": None,
                "sequence": 0,
            },
        }
        other_modules = [
            {
                **module,
                "module_id": str(uuid4()),
                "service_kind": "INFRASTRUCTURE",
                "title": {"ru": "Технический участник", "en": "Technical owner"},
            },
            {
                **{
                    key: value for key, value in module.items() if key != "service_kind"
                },
                "module_id": str(uuid4()),
                "title": {"ru": "Участник без типа", "en": "Unclassified owner"},
            },
            {
                **module,
                "module_id": str(uuid4()),
                "service_kind": None,
                "title": {"ru": "Участник с null", "en": "Null owner"},
            },
        ]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(args.chromium), headless=True
            )
            try:
                context = browser.new_context(
                    ignore_https_errors=True, viewport={"width": 1440, "height": 1000}
                )
                page = context.new_page()
                mode = "hold"
                calls: list[Route] = []
                pending: list[tuple[Route, dict[str, object]]] = []
                errors: list[str] = []
                page.on("pageerror", lambda _: errors.append("browser error"))

                def metadata(route: Route) -> None:
                    calls.append(route)
                    if mode == "error":
                        route.fulfill(status=503, json={"detail": "fixture failure"})
                        return
                    response = route.fetch()
                    check(response.status == 200, "Настоящий API принял JWT оператора")
                    data = response.json()
                    data["modules"].extend([module, *other_modules])
                    if mode == "hold":
                        pending.append((route, data))
                    else:
                        route.fulfill(json=data)

                page.route(base + "/api/modules", metadata)
                page.goto(base)
                page.locator("#username").fill(cfg["OPERATOR_USER"])
                page.locator("#password").fill(
                    (secret / "provision/operator-password").read_text().strip()
                )
                page.locator("#kc-login").click()
                page.get_by_role("heading", name="Состояние сервисов").wait_for()
                deadline = time.monotonic() + 5
                while not pending and time.monotonic() < deadline:
                    page.wait_for_timeout(20)
                indicator = page.locator(".data-refresh")
                indicator_box = indicator.bounding_box()
                status_box = page.get_by_role(
                    "button", name="Состояние", exact=True
                ).bounding_box()
                check(
                    bool(indicator_box)
                    and bool(status_box)
                    and indicator_box["x"] + indicator_box["width"] <= status_box["x"],
                    "Общий индикатор расположен слева от кнопки состояния",
                )
                check(
                    len(pending) == 1
                    and indicator.count() == 1
                    and "Получение данных" in indicator.inner_text()
                    and indicator.locator("time").count() == 0,
                    "Один индикатор: первая загрузка без ложного времени успеха",
                )
                page.clock.install()
                page.clock.run_for(60_000)
                check(len(calls) == 1, "Медленный запрос не создаёт параллельный опрос")
                mode = "forward"
                route, data = pending.pop()
                route.fulfill(json=data)
                page.locator(".data-refresh--incomplete").wait_for()
                check(
                    "Есть неизвестные состояния" in indicator.inner_text()
                    and indicator.locator("time").count() == 1
                    and "Список:" not in indicator.inner_text(),
                    "HTTP 200 с UNKNOWN не выдаётся за свежие отчёты; время относится к списку",
                )
                navigation = page.locator(".service-navigation")
                check(
                    navigation.locator(".nav-caption").inner_text()
                    == "Подключенные сервисы"
                    and navigation.get_by_text(
                        module["title"]["ru"], exact=True
                    ).count()
                    == 1
                    and all(
                        navigation.get_by_text(owner["title"]["ru"], exact=True).count()
                        == 0
                        for owner in other_modules
                    ),
                    "В левом меню только явно объявленный функциональный владелец",
                )
                check(
                    all(
                        page.locator("table")
                        .get_by_text(owner["title"]["ru"], exact=True)
                        .count()
                        == 1
                        for owner in [module, *other_modules]
                    ),
                    "Состояние сохраняет функциональные, инфраструктурные и неопределённые описания",
                )
                page.screenshot(
                    path=str(ROOT / "test/service-navigation-preview.png"),
                    full_page=True,
                )
                shell_time = indicator.locator("time").get_attribute("datetime")
                page.clock.run_for(59_000)
                check(
                    len(calls) == 1, "Опрос не выполняется раньше минутного интервала"
                )
                page.clock.run_for(1_000)
                deadline = time.monotonic() + 5
                while (
                    indicator.locator("time").get_attribute("datetime") == shell_time
                    and time.monotonic() < deadline
                ):
                    page.wait_for_timeout(20)
                check(
                    len(calls) == 2
                    and indicator.locator("time").get_attribute("datetime")
                    != shell_time,
                    "После минуты получен следующий ответ оболочки",
                )
                page.get_by_role("button", name="Первый раздел", exact=True).click()
                current = page.frame_locator("iframe")
                current.get_by_role("heading", name="Owner page").wait_for()
                frame = next(f for f in page.frames if f != page.main_frame)
                check(
                    indicator.locator("time").count() == 0
                    and "Режим обновления не сообщён" in indicator.inner_text(),
                    "Новый раздел не наследует время и интервал оболочки",
                )
                stamp = page.evaluate("new Date(Date.now() - 30000).toISOString()")

                def report(sequence: int, phase: str, updated: str | None) -> None:
                    frame.evaluate(
                        "value => parent.postMessage(value.report, value.origin)",
                        {
                            "origin": base,
                            "report": {
                                "type": "AELEV_UI_REFRESH",
                                "version": 1,
                                "sequence": sequence,
                                "phase": phase,
                                "poll_seconds": 60,
                                "updated_at": updated,
                            },
                        },
                    )

                report(1, "LOADING", None)
                page.locator(".data-refresh--loading").wait_for()
                check(True, "Произвольный UI управляет получением данных")
                report(2, "READY", stamp)
                page.locator(".data-refresh--ready").wait_for()
                owner_time = indicator.locator("time").get_attribute("datetime")
                check(
                    owner_time == stamp.replace("+00:00", "Z")
                    and "раз в минуту" in indicator.inner_text(),
                    "Время и интервал получены от владельца, не подставлены оболочкой",
                )
                report(3, "LOADING", None)
                page.locator(".data-refresh--loading").wait_for()
                check(
                    indicator.locator("time").get_attribute("datetime") == owner_time,
                    "Фоновое получение сохраняет прежнее время успеха",
                )
                report(4, "ERROR", None)
                page.locator(".data-refresh--error").wait_for()
                check(
                    indicator.locator("time").get_attribute("datetime") == owner_time,
                    "Ошибка владельца сохраняет прежнее время успеха",
                )
                report(5, "READY", stamp)
                page.locator(".data-refresh--ready").wait_for()
                report(4, "ERROR", None)
                page.evaluate(
                    "() => window.postMessage({type:'AELEV_UI_REFRESH',version:1,sequence:999,phase:'ERROR',poll_seconds:60,updated_at:null},location.origin)"
                )
                page.wait_for_timeout(30)
                check(
                    "data-refresh--ready" in (indicator.get_attribute("class") or ""),
                    "Старая последовательность и чужой источник не управляют индикатором",
                )
                mode = "error"
                page.clock.run_for(60_000)
                deadline = time.monotonic() + 5
                while len(calls) < 3 and time.monotonic() < deadline:
                    page.wait_for_timeout(20)
                check(
                    len(calls) == 3
                    and "data-refresh--ready"
                    in (indicator.get_attribute("class") or "")
                    and indicator.locator("time").get_attribute("datetime")
                    == owner_time,
                    "Фоновая ошибка API оболочки не перехватывает индикатор владельца",
                )
                page.screenshot(
                    path=str(ROOT / "test/refresh-preview.png"), full_page=True
                )
                page.get_by_role("button", name="Другой раздел", exact=True).click()
                page.frame_locator("iframe").get_by_role(
                    "heading", name="Owner page"
                ).wait_for()
                check(
                    indicator.locator("time").count() == 0,
                    "Смена раздела сбрасывает показания предыдущего iframe",
                )
                frame = next(f for f in page.frames if f != page.main_frame)
                report(1, "READY", stamp)
                page.locator(".data-refresh--ready").wait_for()
                frame.goto(module["sections"][1]["url"])
                page.locator(".data-refresh--waiting").wait_for()
                check(
                    indicator.locator("time").count() == 0,
                    "Перезагрузка iframe не сохраняет старый успех",
                )
                report(1, "LOADING", None)
                page.locator(".data-refresh--loading").wait_for()
                check(True, "Новая загрузка iframe принимает новую последовательность")
                page.get_by_role("button", name="EN", exact=True).click()
                check(
                    "Data refreshes every minute" in indicator.inner_text()
                    and navigation.locator(".nav-caption").inner_text()
                    == "Connected services",
                    "Переключение языка сохраняет единый индикатор",
                )
                page.get_by_role("button", name="Status", exact=True).click()
                page.locator(".data-refresh--error").wait_for()
                check(True, "Возврат к состоянию выбирает источник самой оболочки")
                page.get_by_role("button", name="second", exact=True).click()
                page.frame_locator("iframe").get_by_role(
                    "heading", name="Owner page"
                ).wait_for()
                mode = "forward"
                module["service_kind"] = "INFRASTRUCTURE"
                module["revision"] = 2
                page.clock.run_for(60_000)
                page.get_by_role(
                    "heading", name="Service status", exact=True
                ).wait_for()
                check(
                    page.locator("iframe").count() == 0
                    and navigation.locator(".module-nav").count() == 0
                    and "Not connected yet" in navigation.inner_text(),
                    "Смена типа закрывает открытый iframe и очищает функциональное меню",
                )
                check(
                    "data-refresh--incomplete"
                    in (indicator.get_attribute("class") or "")
                    and page.locator("table")
                    .get_by_text(module["title"]["en"], exact=True)
                    .count()
                    == 1,
                    "После смены типа владелец остаётся в состоянии, индикатор возвращается оболочке",
                )
                module["service_kind"] = "FUNCTIONAL"
                module["revision"] = 3
                page.clock.run_for(60_000)
                navigation.get_by_role("button", name="second", exact=True).wait_for()
                check(
                    page.locator("iframe").count() == 0
                    and page.get_by_role(
                        "heading", name="Service status", exact=True
                    ).count()
                    == 1,
                    "Повторное объявление функционального типа не открывает закрытую страницу автоматически",
                )
                page.get_by_role("button", name="second", exact=True).click()
                page.frame_locator("iframe").get_by_role(
                    "heading", name="Owner page"
                ).wait_for()
                frame = next(f for f in page.frames if f != page.main_frame)
                current_time = page.evaluate("new Date().toISOString()")
                report(1, "READY", current_time)
                page.locator(".data-refresh--ready").wait_for()
                page.clock.run_for(120_000)
                page.locator(".data-refresh--stale").wait_for()
                check(
                    indicator.locator("time").get_attribute("datetime") == current_time,
                    "Без нового сообщения владельца успех истекает после двух интервалов; время сохранено",
                )
                report(2, "READY", page.evaluate("new Date().toISOString()"))
                page.locator(".data-refresh--ready").wait_for()
                check(True, "Новый успех владельца восстанавливает индикатор")
                page.get_by_role("button", name="Status", exact=True).click()
                page.set_viewport_size({"width": 375, "height": 900})
                check(
                    page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
                    "Узкий экран не создаёт горизонтальную прокрутку",
                )
                page.get_by_role("button", name="Sign out", exact=True).click()
                page.locator("#username").wait_for()
                check(not errors, "Вход/выход завершены без ошибок JavaScript")
                context.close()
            finally:
                browser.close()
    except Exception as error:
        failed = True
        rows.append("FAIL: " + type(error).__name__)
        print(rows[-1], flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    report_text = (
        "# Platform UI — общий индикатор получения данных\n\n"
        + f"Дата: {datetime.now(timezone.utc).isoformat()}. Итог: {'FAIL' if failed else 'PASS'}.\n\n"
        + "Проверены работающие nginx/API и настоящий инфраструктурный Keycloak. Произвольный HTTPS UI и описания разных типов — браузерные фикстуры: добавлялись только в перехваченный ответ этого браузерного контекста после успешного ответа настоящего API; SQLite и identities не изменялись. Отказ ответа и изменение типа смоделированы только в браузере. Минутный опрос проверен управляемыми часами Chromium, не длительной проверкой WAN.\n\n"
        + "\n".join("- " + row for row in rows)
        + "\n\n[Индикатор](refresh-preview.png), [меню и состояние](service-navigation-preview.png). Браузер и временный HTTPS-сервер закрыты; рабочая поставка оставлена для ручной проверки. Полный жизненный цикл и подключение реальных сервисных UI этим тестом не подтверждаются.\n"
    )
    (ROOT / "test/refresh-result.md").write_text(report_text)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
