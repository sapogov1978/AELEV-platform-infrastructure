"""Verify actual operator TLS, self-hosted Swagger and its published UI link."""

from __future__ import annotations

import argparse
import hashlib
import json
import ssl
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Protocol
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "AELEV_INFRASTRUCTURE_TRANSLATOR_"


class URLMessage(Protocol):
    @property
    def url(self) -> str: ...


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ca", required=True, type=Path)
    parser.add_argument("--cert", required=True, type=Path)
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--untrusted-cert", required=True, type=Path)
    parser.add_argument("--untrusted-key", required=True, type=Path)
    parser.add_argument("--chromium", required=True, type=Path)
    parser.add_argument("--ui-url", required=True)
    parser.add_argument("--viewer-user", required=True)
    parser.add_argument("--viewer-password-file", required=True, type=Path)
    parser.add_argument("--allow-local-certificate", action="store_true")
    args = parser.parse_args()
    cfg = dict(
        line.split("=", 1)
        for line in (ROOT / ".env").read_text().splitlines()
        if line.startswith(PREFIX)
    )
    base = cfg[PREFIX + "API_PUBLIC_URL"].rstrip("/")
    shell = args.ui_url.rstrip("/")
    if args.allow_local_certificate and any(
        urlsplit(url).hostname not in {"127.0.0.1", "localhost", "::1"}
        for url in (base, shell)
    ):
        parser.error("certificate exception is allowed only for loopback")
    if any(urlsplit(url).scheme != "https" for url in (base, shell)):
        parser.error("HTTPS is required")
    steps: list[str] = []
    passed = False
    try:
        trusted = ssl.create_default_context(cafile=str(args.ca))
        trusted.load_cert_chain(args.cert, args.key)
        with urlopen(base + "/openapi.json", context=trusted, timeout=10) as reply:
            schema = json.load(reply)
        expected_paths = {
            "/healthz",
            "/readyz",
            "/v1/status",
            "/v1/components",
            "/metrics",
        }
        if cfg[PREFIX + "API_TRANSLATION_ENABLED"] == "true":
            expected_paths.add("/v1/translate")
            assert set(schema["paths"]["/v1/translate"]) == {"post"}
        assert set(schema["paths"]) == expected_paths
        for path, operations in schema["paths"].items():
            if "get" not in operations:
                continue
            with urlopen(base + path, context=trusted, timeout=10) as reply:
                assert reply.status == 200
        steps.append(
            "Настоящий HTTPS/mTLS, проверенные CA/SAN "
            "и пять существующих операций чтения"
        )
        for cert, key in ((None, None), (args.untrusted_cert, args.untrusted_key)):
            denied = ssl.create_default_context(cafile=str(args.ca))
            if cert is not None and key is not None:
                denied.load_cert_chain(cert, key)
            try:
                with urlopen(base + "/docs", context=denied, timeout=10):
                    raise AssertionError("UNAUTHORIZED_TLS_WAS_ACCEPTED")
            except (URLError, ssl.SSLError, ConnectionError):
                pass
        steps.append(
            "Без сертификата и с удостоверением чужой CA доступ отвергнут до HTTP"
        )
        for name, digest in (
            (
                "swagger-ui-bundle.js",
                "050bc415ee7048dcd881682678f720264e7da5e373f7461d7c58c755305255f7",
            ),
            (
                "swagger-ui.css",
                "1ac324f7dcd27e4b9386b4bd6421271ec147e922a22c05ba24b11515e9aa6321",
            ),
        ):
            with urlopen(
                base + "/docs/assets/" + name, context=trusted, timeout=10
            ) as reply:
                assert hashlib.sha256(reply.read()).hexdigest() == digest
        steps.append("Реальный runtime отдаёт закреплённые локальные файлы Swagger")
        support = import_module("playwright.sync_api")
        with support.sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(args.chromium), headless=True
            )
            try:
                context = browser.new_context(
                    ignore_https_errors=args.allow_local_certificate,
                    client_certificates=[
                        {"origin": base, "certPath": args.cert, "keyPath": args.key}
                    ],
                    viewport={"width": 1440, "height": 1000},
                )
                page = context.new_page()
                page.goto(shell)
                page.locator("#username").fill(args.viewer_user)
                try:
                    page.locator("#password").fill(
                        args.viewer_password_file.read_text().strip()
                    )
                except Exception:
                    raise RuntimeError("OPERATOR_SIGNIN_FAILED") from None

                def module_response(response: URLMessage) -> bool:
                    return response.url == shell + "/api/modules"

                with page.expect_response(module_response) as received:
                    page.locator("#kc-login").click()
                assert received.value.status == 200
                modules = received.value.json()["modules"]
                module_id = cfg[PREFIX + "UI_MODULE_ID"]
                owner = next(
                    module for module in modules if module["module_id"] == module_id
                )
                assert owner["revision"] == int(cfg[PREFIX + "UI_DESCRIPTION_REVISION"])
                assert owner["swagger_url"] == base + "/docs"
                assert "ui_url" not in owner
                group = page.locator('tbody[data-module-id="' + module_id + '"]')
                group.locator("button[aria-expanded]").click()
                assert group.locator("a.status-open:visible").count() == 2
                for part in owner["components"]:
                    assert ("swagger_url" in part) == (part["id"] == "api")
                link = group.locator("a.status-open").first
                with page.expect_popup() as opened:
                    link.click()
                popup = opened.value
                support.expect(
                    popup.locator(".swagger-ui .info .title")
                ).to_contain_text("DRP translator operations")
                assert popup.url == base + "/docs"
                assert popup.evaluate(
                    "window.opener === null && document.referrer === ''"
                )
                assert (
                    popup.locator(".opblock-summary-method").all_text_contents()
                    == ["GET"] * 5
                )
                steps.append(
                    "Ссылки группы и API приняты настоящей оболочкой; "
                    "Swagger открыт без opener/Referer"
                )
                external: list[str] = []

                def capture(request: URLMessage) -> None:
                    if not request.url.startswith(base + "/"):
                        external.append(request.url)

                popup.on("request", capture)
                popup.reload()
                support.expect(popup.locator(".opblock-summary-method")).to_have_count(
                    5
                )
                assert not external
                popup.locator(".opblock-summary").filter(has_text="/healthz").click()
                popup.get_by_role("button", name="Try it out", exact=True).click()

                def health_response(response: URLMessage) -> bool:
                    return response.url == base + "/healthz"

                with popup.expect_response(health_response) as operation:
                    popup.get_by_role("button", name="Execute", exact=True).click()
                assert operation.value.status == 200
                assert operation.value.json() == {"status": "ok"}
                steps.append(
                    "Swagger загрузился без CDN "
                    "и выполнил настоящий GET /healthz с mTLS"
                )
                popup.screenshot(path=str(ROOT / "test/swagger.png"), full_page=True)
            finally:
                browser.close()
        passed = True
    finally:
        report = [
            "# translator-service — настоящий Swagger и операторский доступ",
            "",
            "Дата UTC: "
            + datetime.now(UTC).isoformat()
            + ". Итог: "
            + ("PASS" if passed else "FAIL")
            + ".",
            "",
            *("- PASS: " + step for step in steps),
            "",
            "Тест только читает рабочие endpoints и публикации. "
            "Ответы не подменялись; контейнеры, identities, сети, тома "
            "и переводческие задания не создавались. Браузер закрыт. "
            "Проверка серверной CA/SAN обязательна в HTTPS-пробах; "
            "браузерное исключение возможно только явно на loopback "
            "с локальной PKI. WAN, внешняя PKI, качество перевода "
            "и полный остановочный жизненный цикл не проверялись.",
        ]
        (ROOT / "test/swagger-live-result.md").write_text("\n".join(report) + "\n")
    print("PASS: translator Swagger, mTLS and own platform links")


if __name__ == "__main__":
    main()
