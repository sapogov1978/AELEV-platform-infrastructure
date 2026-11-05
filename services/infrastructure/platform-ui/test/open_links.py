"""Read real owner links and verify browser navigation without runtime mutations."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prepare import directory, settings  # noqa: E402


def target(module: dict[str, Any], links: dict[str, Any]) -> str | None:
    if module.get("service_kind") == "FUNCTIONAL" and module["sections"]:
        value = links.get("swagger_url")
    else:
        value = links.get("ui_url") or links.get("swagger_url")
    return value if isinstance(value, str) else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chromium", required=True, type=Path)
    parser.add_argument("--allow-local-certificate", action="store_true")
    parser.add_argument(
        "--client-certificate",
        nargs=3,
        action="append",
        default=[],
        metavar=("ORIGIN", "CERTIFICATE", "KEY"),
    )
    args = parser.parse_args()
    cfg = settings(ROOT / ".env")
    base = cfg["PUBLIC_URL"]
    loopback = {"localhost", "127.0.0.1", "::1"}
    if args.allow_local_certificate and urlsplit(base).hostname not in loopback:
        parser.error("certificate exception requires explicit loopback")
    certificates = [
        {"origin": origin, "certPath": Path(cert), "keyPath": Path(key)}
        for origin, cert, key in args.client_certificate
    ]
    report = ROOT / "test/open-links-live-result.md"
    stages: list[str] = []
    passed = False
    support = import_module("playwright.sync_api")
    try:
        with support.sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(args.chromium), headless=True
            )
            try:
                context = browser.new_context(
                    ignore_https_errors=args.allow_local_certificate,
                    client_certificates=certificates,
                    viewport={"width": 1440, "height": 1000},
                )
                page = context.new_page()
                page.goto(base)
                page.locator("#username").fill(cfg["OPERATOR_USER"])
                password = (
                    directory(cfg["SECRET_ROOT"], ROOT / ".env")
                    / "provision/operator-password"
                )
                try:
                    page.locator("#password").fill(password.read_text().strip())
                except Exception:
                    raise RuntimeError("password entry failed") from None
                with page.expect_response(
                    lambda reply: reply.url == base + "/api/modules"
                ) as received:
                    page.locator("#kc-login").click()
                assert received.value.status == 200
                modules = received.value.json()["modules"]
                assert modules
                stages.append(
                    "Настоящий OIDC-вход и чтение опубликованного владельцами API без подмены ответов"
                )
                opened: set[str] = set()
                for module in modules:
                    group = page.locator(
                        'tbody[data-module-id="' + module["module_id"] + '"]'
                    )
                    toggle = group.locator("button[aria-expanded]")
                    if toggle.count():
                        toggle.click()
                    declarations = [module, *module.get("components", [])]
                    rows = group.locator("tr")
                    assert rows.count() == len(declarations)
                    for index, declaration in enumerate(declarations):
                        url = target(module, declaration)
                        link = rows.nth(index).locator("a.status-open")
                        assert link.count() == int(url is not None)
                        if url is None:
                            continue
                        support.expect(link).to_be_visible()
                        support.expect(link).to_have_attribute("href", url)
                        support.expect(link).to_have_attribute("target", "_blank")
                        support.expect(link).to_have_attribute(
                            "rel", "noopener noreferrer"
                        )
                        if url in opened:
                            continue
                        if (
                            args.allow_local_certificate
                            and urlsplit(url).hostname not in loopback
                        ):
                            raise ValueError("refusing remote TLS exception")
                        with page.expect_popup() as popup:
                            link.click()
                        destination = popup.value
                        try:
                            destination.wait_for_load_state("domcontentloaded")
                            assert destination.evaluate("window.opener === null")
                            assert destination.evaluate("document.referrer") == ""
                            assert (
                                urlsplit(destination.url).netloc == urlsplit(url).netloc
                            )
                            support.expect(destination.locator("body")).to_contain_text(
                                re.compile(r"\S"), timeout=15000
                            )
                        finally:
                            destination.close()
                        opened.add(url)
                        stages.append(
                            "Реальная отдельная вкладка без opener/Referer: "
                            + declaration["title"]["ru"]
                        )
                assert opened, "no live owner published external links"
                stages.append(
                    "У всех строк/компонентов кнопки соответствуют их собственным ссылкам; без ссылки кнопки нет"
                )
                page.screenshot(path=str(ROOT / "test/open-links.png"), full_page=True)
                passed = True
            finally:
                browser.close()
    finally:
        report.write_text(
            "# Platform UI — ссылки на настоящие интерфейсы\n\n"
            f"Дата: {datetime.now(timezone.utc).isoformat()}. Итог: {'PASS' if passed else 'FAIL'}.\n\n"
            + "\n".join("- PASS: " + stage for stage in stages)
            + "\n\nПроверка только читает настоящие публикации и открывает их страницы. Каталога продуктов, фиктивных модулей, identities, контейнеров, сетей и томов нет; браузер закрыт. Клиентские удостоверения предоставлены оператором явно, токен оболочки целевым страницам не передаётся. Исключение доверия локальной PKI допустимо только с явным флагом и только для loopback; рабочие проверки TLS/mTLS не меняются. Swagger функционального UI проверяется отдельными контрактными/модульными регрессиями: отсутствие такого живого издателя не подменяется фиктивной публикацией. WAN, внешний CA и целевые административные действия этим тестом не проверяются.\n"
        )
    if not passed:
        raise RuntimeError("live navigation checks failed")
    print("PASS: read-only live links and navigation", flush=True)


if __name__ == "__main__":
    main()
