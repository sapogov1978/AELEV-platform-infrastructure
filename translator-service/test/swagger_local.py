"""Real local Swagger/PKCE proof, with explicitly optional Kafka translation."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "AELEV_INFRASTRUCTURE_TRANSLATOR_"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", required=True, type=Path)
    parser.add_argument("--ui-url", required=True)
    parser.add_argument("--viewer-password-file", required=True, type=Path)
    parser.add_argument("--allow-local-idp-certificate", action="store_true")
    parser.add_argument("--report-prefix", default="swagger-local")
    parser.add_argument(
        "--translate",
        action="store_true",
        help="Submit two translation jobs; their canonical records remain",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]*", args.report_prefix):
        parser.error("report prefix must be a simple service-owned filename")
    cfg = dict(
        line.split("=", 1)
        for line in (ROOT / ".env").read_text().splitlines()
        if line.startswith(PREFIX)
    )
    base = cfg[PREFIX + "API_PUBLIC_URL"].rstrip("/")
    shell = args.ui_url.rstrip("/")
    issuer = cfg[PREFIX + "API_OIDC_ISSUER"]
    translation_enabled = cfg[PREFIX + "API_TRANSLATION_ENABLED"] == "true"
    if args.translate and not translation_enabled:
        parser.error("operator translation must be explicitly enabled")
    if cfg[PREFIX + "ENVIRONMENT"] != "local" or urlsplit(base).scheme != "http":
        parser.error("explicit local HTTP mode is required")
    loopback = {"localhost", "127.0.0.1", "::1"}
    if urlsplit(base).hostname not in loopback:
        parser.error("Swagger must be loopback")
    if args.allow_local_idp_certificate and any(
        urlsplit(url).hostname not in loopback for url in (issuer, shell)
    ):
        parser.error("certificate exception is limited to loopback IdP and shell")
    stages: list[str] = []
    passed = False
    failure = ""
    phase = "documentation"
    support = import_module("playwright.sync_api")
    try:
        with support.sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(args.chromium), headless=True
            )
            try:
                context = browser.new_context(
                    ignore_https_errors=args.allow_local_idp_certificate,
                    viewport={"width": 1440, "height": 1000},
                )
                page = context.new_page()
                page.goto(base + "/docs")
                page.locator(".swagger-ui .info .title").wait_for()
                assert context.request.get(base + "/docs").status == 200
                schema = context.request.get(base + "/openapi.json").json()
                assert len(schema["paths"]) == (6 if translation_enabled else 5)
                groups = ["default", "translator", "metrics"]
                assert [tag["name"] for tag in schema["tags"]] == groups
                headers = page.locator(".opblock-tag")
                page.locator("#operations-tag-metrics").wait_for()
                assert headers.count() == len(groups)
                assert [headers.nth(i).get_attribute("id") for i in range(3)] == [
                    "operations-tag-" + group for group in groups
                ]
                for path, group in {
                    "/healthz": "default",
                    "/readyz": "default",
                    "/v1/status": "default",
                    "/v1/components": "default",
                    "/metrics": "metrics",
                    "/v1/translate": "translator",
                }.items():
                    if path == "/v1/translate" and not translation_enabled:
                        continue
                    method = "post" if path == "/v1/translate" else "get"
                    assert schema["paths"][path][method]["tags"] == [group]
                    operation_id = schema["paths"][path][method]["operationId"]
                    page.locator("#operations-" + group + "-" + operation_id).wait_for(
                        state="attached"
                    )
                stages.append(
                    "Настоящий Swagger отрисовал ровно три группы: default, "
                    "translator, metrics; состав операций соответствует схеме"
                )
                for path in schema["paths"]:
                    if path == "/v1/translate":
                        assert context.request.post(base + path, data={}).status == 401
                        continue
                    assert context.request.get(base + path).status == 401
                stages.append(
                    "Swagger и схема открылись без клиентского сертификата; "
                    "все включённые операции без токена дали 401"
                )
                page.get_by_role("button", name="Authorize", exact=True).first.click()
                phase = "oauth_popup"
                with page.expect_popup() as opened:
                    page.locator(".dialog-ux button.authorize").click()
                login = opened.value
                phase = "identity_form"
                login.locator("#username").wait_for()
                query = parse_qs(urlsplit(cast(str, login.url)).query)
                assert query["client_id"] == [cfg[PREFIX + "API_OIDC_CLIENT_ID"]]
                assert query["response_type"] == ["code"]
                assert query["code_challenge_method"] == ["S256"]
                assert query["redirect_uri"] == [base + "/docs/oauth2-redirect.html"]
                assert len(query["code_challenge"][0]) >= 43
                login.locator("#username").fill(cfg[PREFIX + "API_OPERATOR_USER"])
                phase = "password_entry"
                login.locator("#password").fill(
                    args.viewer_password_file.read_text().strip()
                )
                login.locator("#kc-login").click()
                phase = "token_exchange"
                page.locator(".dialog-ux button").filter(has_text="Logout").wait_for(
                    timeout=45000
                )
                stages.append(
                    "Настоящий Keycloak Authorization Code/PKCE S256 "
                    "и точный callback; пароль не передавался Swagger"
                )
                own_token = page.evaluate(
                    "Object.values(window.ui.getSystem().authSelectors"
                    ".authorized().toJS()).find(x=>x.token?.access_token)"
                    ".token.access_token"
                )
                phase = "authorized_operations"
                for path in schema["paths"]:
                    if path == "/v1/translate":
                        continue
                    assert (
                        context.request.get(
                            base + path,
                            headers={"Authorization": "Bearer " + own_token},
                        ).status
                        == 200
                    )
                own_token = None
                phase = "swagger_execute"
                page.locator(".dialog-ux button.btn-done").click()
                operation_id = schema["paths"]["/healthz"]["get"]["operationId"]
                page.locator("#operations-default-" + operation_id).click()
                page.get_by_role("button", name="Try it out", exact=True).click()
                page.get_by_role("button", name="Execute", exact=True).click()
                page.locator(".live-responses-table .response-col_status").filter(
                    has_text="200"
                ).wait_for()
                stages.append(
                    "Собственный access token разрешил все пять операций, "
                    "Swagger Execute выполнил настоящий GET /healthz"
                )
                assert (
                    page.evaluate(
                        "Object.keys(localStorage).length + "
                        "Object.keys(sessionStorage).length"
                    )
                    == 0
                )
                if args.translate:
                    phase = "operator_translation"
                    operation_id = schema["paths"]["/v1/translate"]["post"][
                        "operationId"
                    ]
                    operation = page.locator("#operations-translator-" + operation_id)
                    operation.locator(".opblock-summary").click()
                    operation.get_by_role(
                        "button", name="Try it out", exact=True
                    ).click()
                    ids: dict[str, str] = {}
                    for queue in ("normal", "priority"):
                        operation.locator("textarea.body-param__text").fill(
                            json.dumps(
                                {
                                    "source_language": "en",
                                    "target_language": "de",
                                    "queue": queue,
                                    "text": "Hello, world!",
                                }
                            )
                        )
                        with page.expect_response(
                            base + "/v1/translate",
                            timeout=(
                                int(cfg[PREFIX + "API_TRANSLATION_TIMEOUT_SECONDS"])
                                + 30
                            )
                            * 1000,
                        ) as sent:
                            operation.get_by_role(
                                "button", name="Execute", exact=True
                            ).click()
                        reply = sent.value
                        assert reply.status == 200 and reply.request.method == "POST"
                        data = reply.json()
                        assert (
                            data["queue"] == queue and data["translated_text"].strip()
                        )
                        assert (
                            data["source_language"] == "en"
                            and data["target_language"] == "de"
                        )
                        ids[queue] = data["request_id"]
                        stages.append(
                            "Настоящий Swagger POST через "
                            + queue
                            + ": 200, перевод «"
                            + data["translated_text"]
                            + "», request_id="
                            + data["request_id"]
                        )
                    check = subprocess.run(
                        [
                            "docker",
                            "compose",
                            "-f",
                            "compose.remote.yml",
                            "--env-file",
                            ".env",
                            "exec",
                            "-T",
                            "translator-api",
                            "python",
                            "-c",
                            "import json,sys; from sqlalchemy import text; "
                            "from translator_service.runtime import repository; "
                            "r=repository(); "
                            "rows=r.engine.connect().execute(text("
                            "'SELECT j.request_id,j.priority,j.state,"
                            "o.state AS outbox_state FROM translation_job j "
                            "JOIN translation_outbox o USING(request_id) "
                            "WHERE request_id IN (:n,:p)'),"
                            "{'n':sys.argv[1],'p':sys.argv[2]}).mappings().all(); "
                            "print(json.dumps([dict(x) for x in rows])); "
                            "r.engine.dispose()",
                            ids["normal"],
                            ids["priority"],
                        ],
                        cwd=ROOT,
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    rows = {row["request_id"]: row for row in json.loads(check.stdout)}
                    assert len(rows) == 2
                    for queue, priority in (("normal", 100), ("priority", 0)):
                        row = rows[ids[queue]]
                        assert (
                            row["priority"] == priority and row["state"] == "SUCCEEDED"
                        )
                        assert row["outbox_state"] == "PUBLISHED"
                    stages.append(
                        "Собственная БД подтвердила очереди (100/0), SUCCEEDED "
                        "и outbox PUBLISHED; прямого вызова модели не было"
                    )
                page.screenshot(
                    path=str(ROOT / f"test/{args.report_prefix}.png"),
                    full_page=True,
                    mask=[
                        page.locator(".curl-command, .request-snippets"),
                        page.locator("pre").filter(has_text="Bearer "),
                    ],
                )
                page.reload()
                page.locator(".swagger-ui .info .title").wait_for()
                assert not page.evaluate(
                    "Object.values(window.ui.getSystem().authSelectors"
                    ".authorized().toJS()).some(x=>x.token?.access_token)"
                )
                stages.append(
                    "Перезагрузка не сохранила токен; localStorage/sessionStorage пусты"
                )
                shell_page = context.new_page()
                phase = "shell_link"
                shell_page.goto(shell)
                shell_page.locator(
                    'tbody[data-module-id="' + cfg[PREFIX + "UI_MODULE_ID"] + '"]'
                ).wait_for(timeout=45000)
                group = shell_page.locator(
                    'tbody[data-module-id="' + cfg[PREFIX + "UI_MODULE_ID"] + '"]'
                )
                link = group.locator("a.status-open").first
                assert link.get_attribute("href") == base + "/docs"
                with shell_page.expect_popup() as opened_link:
                    link.click()
                target = opened_link.value
                target.locator(".swagger-ui .info .title").wait_for()
                assert target.evaluate("window.opener === null")
                assert target.evaluate("document.referrer") == ""
                stages.append(
                    "Настоящая оболочка открыла объявленную HTTP-ссылку "
                    "в отдельной вкладке без opener, Referer и передачи токена"
                )
                context.close()
                passed = True
            finally:
                browser.close()
    except Exception as error:
        failure = type(error).__name__ + " (" + phase + ")"
    finally:
        report = "# translator-service — локальный Swagger и собственный OIDC\n\n"
        verdict = "PASS" if passed else "FAIL"
        report += f"Дата UTC: {datetime.now(UTC).isoformat()}. Итог: {verdict}.\n\n"
        report += "\n".join("- PASS: " + stage for stage in stages) + "\n\n"
        if failure:
            report += (
                "Проверка остановлена: "
                + failure
                + ". URL с OAuth code, токены и пароли не сохраняются.\n\n"
            )
        report += (
            "Тест использует настоящие API и собственный вход; "
            "ответы не подменяются. Нет клиентского сертификата Swagger "
            "или изменения системного доверия. Браузер закрыт, контейнеры, "
            "identities, topics и тома тест не создаёт и не удаляет. "
            "Явное браузерное исключение сертификата касается только уже "
            "существующих локальных HTTPS IdP/оболочки; удалённый HTTPS, "
            "WAN, production PKI и полный жизненный цикл не проверялись.\n"
        )
        if args.translate:
            report += (
                "\nЯвный --translate отправляет два учебных задания "
                "через рабочие очереди. Их записи, Kafka-сообщения и результаты "
                "сохраняются по штатной политике; "
                "рабочие журналы и данные ради очистки теста не удаляются.\n"
            )
        (ROOT / f"test/{args.report_prefix}-result.md").write_text(report)
        print("PASS" if passed else "FAIL: " + failure)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
