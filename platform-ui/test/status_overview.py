"""Read injected infrastructure composition without a hardcoded owner catalogue."""

import argparse
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from urllib.parse import urlsplit

from aelev_platform_ui_client import https_url


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui-url", required=True)
    parser.add_argument("--viewer-user", required=True)
    parser.add_argument("--viewer-password-file", required=True, type=Path)
    parser.add_argument("--chromium", required=True, type=Path)
    parser.add_argument("--expected-module-count", required=True, type=int)
    parser.add_argument("--allow-local-certificate", action="store_true")
    args = parser.parse_args()
    url = https_url(args.ui_url)
    if args.expected_module_count <= 0:
        parser.error("positive infrastructure module count required")
    if args.allow_local_certificate and urlsplit(url).hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        parser.error("certificate exception requires explicit loopback")
    root = Path(__file__).resolve().parent
    support = import_module("playwright.sync_api")
    with support.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(args.chromium), headless=True
        )
        try:
            page = browser.new_page(
                ignore_https_errors=args.allow_local_certificate,
                viewport={"width": 1440, "height": 1000},
            )
            page.goto(url)
            page.locator("#username").fill(args.viewer_user)
            try:
                page.locator("#password").fill(
                    args.viewer_password_file.read_text().strip()
                )
            except Exception:
                raise RuntimeError("password entry failed") from None
            with page.expect_response(
                lambda reply: reply.url == url + "/api/modules"
            ) as received:
                page.locator("#kc-login").click()
            assert received.value.status == 200
            rows = [
                row
                for row in received.value.json()["modules"]
                if row["service_kind"] == "INFRASTRUCTURE"
            ]
            assert len(rows) == args.expected_module_count
            assert len({row["module_id"] for row in rows}) == len(rows)
            for row in rows:
                assert row["status"]["state"] == "ONLINE"
                assert row["components"] and not row["sections"]
                assert all(
                    part["state"] == "ONLINE" for part in row["status"]["components"]
                )
                group = page.locator('tbody[data-module-id="' + row["module_id"] + '"]')
                support.expect(group).to_be_visible()
                toggle = group.locator("button[aria-expanded]")
                support.expect(toggle).to_contain_text(row["title"]["ru"])
                if toggle.get_attribute("aria-expanded") == "false":
                    toggle.click()
                assert group.locator(".status-component").count() == len(
                    row["components"]
                )
                for pill in group.locator(".status-pill").all():
                    support.expect(pill).to_have_class("status-pill online")
                for index, component in enumerate(row["components"]):
                    support.expect(
                        group.locator(".status-component td:first-child").nth(index)
                    ).to_contain_text(component["title"]["ru"])
                toggle.click()
                support.expect(toggle).to_have_attribute("aria-expanded", "false")
                toggle.click()
                support.expect(toggle).to_have_attribute("aria-expanded", "true")
            page.get_by_role("button", name="EN", exact=True).click()
            for row in rows:
                group = page.locator('tbody[data-module-id="' + row["module_id"] + '"]')
                toggle = group.locator("button[aria-expanded]")
                support.expect(toggle).to_contain_text(row["title"]["en"])
                support.expect(toggle).to_have_attribute("aria-expanded", "true")
                for index, component in enumerate(row["components"]):
                    support.expect(
                        group.locator(".status-component td:first-child").nth(index)
                    ).to_contain_text(component["title"]["en"])
            page.get_by_role("button", name="RU", exact=True).click()
            page.screenshot(path=str(root / "status-overview.png"), full_page=True)
            report = "# Сверка инфраструктурных строк в настоящем браузере\n\n"
            report += f"Дата: {datetime.now(timezone.utc).isoformat()}. Итог: PASS.\n\n"
            report += (
                "Команда: `python3.13 test/status_overview.py --expected-module-count "
                + str(args.expected_module_count)
                + "` с обязательными URL, Chromium, учётной записью просмотра и файлом её пароля по README.\n\n"
            )
            report += "| Назначение, объявленное владельцем | Ревизия | Состояние | Состав владельца |\n|---|---|---|---|\n"
            for row in rows:
                title = row["title"]["ru"].replace("|", "\\|").replace("\n", " ")
                components = ", ".join(
                    part["title"]["ru"].replace("|", "\\|").replace("\n", " ")
                    for part in row["components"]
                )
                report += f"| {title} | {row['revision']} | ONLINE | {components} |\n"
            report += "\nВсе строки получены динамически через настоящий OIDC/API. Названия назначения и компонентов обоих языков сверены с DOM; раскрытие, сворачивание и сохранение раскрытия при смене языка проверены. Все компоненты показаны ONLINE. Тест не содержит списка продуктов или их адресов, не создаёт и не изменяет ресурсов. [Снимок страницы](status-overview.png). Отдельные stop/start и независимость публикаций подтверждаются только тестами владельцев, не этим снимком. WAN и продукционная PKI не проверялись.\n"
            (root / "status-overview-result.md").write_text(report)
            print(
                "PASS: "
                + str(len(rows))
                + " строк, "
                + str(sum(len(row["components"]) for row in rows))
                + " компонентов в API и браузере"
            )
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        (Path(__file__).resolve().parent / "status-overview-result.md").write_text(
            "# Сверка инфраструктурных строк\n\n"
            + f"Дата: {datetime.now(timezone.utc).isoformat()}. Итог: FAIL.\n\n"
            + "Тип ошибки: `"
            + type(error).__name__
            + "`. Ресурсы не изменялись.\n"
        )
        raise RuntimeError("infrastructure overview failed; see own report") from None
