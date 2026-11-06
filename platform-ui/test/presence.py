"""Verify confirmed withdrawal and lost contact using this real owner only."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prepare import directory, settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chromium", type=Path, required=True)
    parser.add_argument("--allow-local-certificate", action="store_true")
    args = parser.parse_args()
    cfg = settings(ROOT / ".env")
    base = cfg["PUBLIC_URL"]
    if args.allow_local_certificate:
        from urllib.parse import urlsplit

        if urlsplit(base).hostname not in {"localhost", "127.0.0.1", "::1"}:
            parser.error("certificate exception requires explicit loopback")
    secret = directory(cfg["SECRET_ROOT"], ROOT / ".env")
    identifier = cfg["UI_MODULE_ID"]
    ttl = int(cfg["STATUS_TTL_SECONDS"])
    publisher = "platform-ui-status-publisher"
    stages: list[str] = []
    started = time.monotonic()
    passed = False
    paused = stopped = False

    def command(*arguments: str) -> str:
        return subprocess.run(
            arguments, cwd=ROOT, check=True, capture_output=True, text=True, timeout=90
        ).stdout

    def compose(*arguments: str) -> str:
        return command("docker", "compose", "--profile", "ui", *arguments)

    def inventory() -> dict[str, list[str]]:
        return {
            kind: (
                sorted(command("docker", kind, "ls", "-q", "--no-trunc").split())
                if kind == "image"
                else sorted(
                    command(
                        "docker", kind, "ls", "-aq" if kind == "container" else "-q"
                    ).split()
                )
            )
            for kind in ("container", "volume", "network", "image")
        }

    def check(condition: bool, label: str) -> None:
        if not condition:
            raise AssertionError(label)
        stages.append(label)
        print("PASS: " + label, flush=True)

    try:
        initial_containers = [
            json.loads(line) for line in compose("ps", "--format", "json").splitlines()
        ]
        baseline = {row["Service"]: row["ID"] for row in initial_containers}
        check(
            len(baseline) == 3
            and publisher in baseline
            and all(
                row["State"] == "running" and row["Health"] == "healthy"
                for row in initial_containers
            ),
            "Три настоящих контейнера собственной поставки исходно здоровы",
        )
        before_inventory = inventory()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(args.chromium), headless=True
            )
            try:
                page = browser.new_page(
                    ignore_https_errors=args.allow_local_certificate,
                    viewport={"width": 1440, "height": 1000},
                )
                page.clock.install()
                with page.expect_response(
                    lambda response: response.url == base + "/api/modules"
                ) as response:
                    page.goto(base)
                    page.locator("#username").fill(cfg["OPERATOR_USER"])
                    page.locator("#password").fill(
                        (secret / "provision/operator-password").read_text().strip()
                    )
                    page.locator("#kc-login").click()
                check(
                    response.value.status == 200,
                    "Настоящий OIDC-вход и HTTPS-чтение API",
                )
                authorization = response.value.request.all_headers()["authorization"]

                def modules() -> list[dict[str, Any]]:
                    nonlocal authorization
                    reply = page.request.get(
                        base + "/api/modules", headers={"Authorization": authorization}
                    )
                    if reply.status == 401:
                        with page.expect_response(
                            lambda response: response.url == base + "/api/modules"
                        ) as refreshed:
                            page.reload()
                        authorization = refreshed.value.request.all_headers()[
                            "authorization"
                        ]
                        reply = page.request.get(
                            base + "/api/modules",
                            headers={"Authorization": authorization},
                        )
                    if reply.status != 200:
                        raise AssertionError("viewer request rejected")
                    return list(reply.json()["modules"])

                def own() -> dict[str, Any] | None:
                    return next(
                        (row for row in modules() if row["module_id"] == identifier),
                        None,
                    )

                def await_owner(connection: str) -> dict[str, Any]:
                    deadline = time.monotonic() + ttl + 90
                    while time.monotonic() < deadline:
                        row = own()
                        if (
                            row
                            and row["status"]["connection"] == connection
                            and (
                                connection == "LOST"
                                or row["status"]["state"] == "ONLINE"
                            )
                        ):
                            return row
                        page.wait_for_timeout(1000)
                    raise AssertionError(
                        "owner connection did not change: " + connection
                    )

                def poll() -> None:
                    page.clock.set_fixed_time(datetime.now(timezone.utc))
                    with page.expect_response(
                        lambda reply: reply.url == base + "/api/modules"
                    ):
                        page.clock.fast_forward(int(cfg["POLL_SECONDS"]) * 1000)

                group = page.locator('tbody[data-module-id="' + identifier + '"]')
                initial = await_owner("CONNECTED")
                revision = initial["revision"]
                sequence = initial["status"]["sequence"]
                poll()
                expect(group).to_have_count(1)
                expect(page.locator(".data-refresh--ready")).to_have_count(1)
                check(
                    True,
                    "Свежие подтверждённые отчёты видны в строке и общем индикаторе",
                )

                stopped = True
                compose("stop", publisher)
                check(
                    own() is None, "Штатная отписка подтверждена API без ожидания TTL"
                )
                poll()
                expect(group).to_have_count(0)
                check(
                    True,
                    "После минутного опроса штатно остановленный владелец исчез из браузера",
                )
                compose("start", publisher)
                stopped = False
                returned = await_owner("CONNECTED")
                check(
                    returned["revision"] == revision
                    and returned["status"]["sequence"] > sequence,
                    "Возврат сохранил UUID, ревизию и увеличил устойчивую последовательность",
                )
                poll()
                expect(group).to_have_count(1)
                check(
                    True,
                    "Вернувшийся владелец динамически появился без пересборки оболочки",
                )

                paused = True
                compose("pause", publisher)
                print(
                    f"WAIT: проверка потери связи настоящим сервером, TTL {ttl} с",
                    flush=True,
                )
                lost = await_owner("LOST")
                check(
                    lost["module_id"] == identifier,
                    "Без отписки строка осталась; сервер подтвердил LOST по реальному времени",
                )
                poll()
                expect(group.locator(".publisher-connection").first).to_have_text(
                    "Нет связи с издателем"
                )
                expect(page.locator(".data-refresh--disconnected")).to_have_count(1)
                check(
                    True,
                    "Браузер показывает «Нет связи» и красный общий индикатор, не скрывает отказ",
                )
                page.screenshot(
                    path=str(ROOT / "test/presence-lost.png"), full_page=True
                )
                compose("unpause", publisher)
                paused = False
                restored = await_owner("CONNECTED")
                check(
                    restored["status"]["sequence"] > lost["status"]["sequence"],
                    "После зависания продолжается прежняя последовательность",
                )
                poll()
                expect(group.locator(".status-pill").first).to_have_text("Доступен")
                expect(page.locator(".data-refresh--ready")).to_have_count(1)
                page.screenshot(
                    path=str(ROOT / "test/presence-restored.png"), full_page=True
                )
                check(True, "Связь, доступность и общий индикатор восстановились")
                page.get_by_role("button", name="Выход", exact=True).click()
            finally:
                browser.close()
        current = {
            row["Service"]: row["ID"]
            for row in (
                json.loads(line)
                for line in compose("ps", "--format", "json").splitlines()
            )
        }
        check(
            current == baseline and inventory() == before_inventory,
            "Исходные контейнеры, образы, сети и тома сохранены; тест не создал Docker-ресурсов",
        )
        passed = True
    finally:
        if paused:
            compose("unpause", publisher)
        if stopped:
            compose("start", publisher)
        report = (
            "# Подтверждённое присутствие издателя\n\n"
            + f"Дата: {datetime.now(timezone.utc).isoformat()}. Итог: {'PASS' if passed else 'FAIL'}. Длительность: {time.monotonic() - started:.1f} с.\n\n"
            + f"Команда: `python test/presence.py --chromium <Chromium> --allow-local-certificate`. Python 3.13.7, Playwright 1.56.0; серверный срок свежести {ttl} с.\n\n"
            + "| Этап | Результат |\n|---|---|\n"
            + "".join("| " + stage + " | PASS |\n" for stage in stages)
            + "\nИспользованы существующие самостоятельные поставки UI и IdP с настоящими JWT и HTTPS. Управляется только собственный издатель UI: stop/start проверяет штатную отписку, pause/unpause — отсутствие отписки при зависании. Время TTL не ускоряется, управляемые часы ускоряют только минутный опрос браузера. Фиктивные модули, identities, контейнеры и тома не создаются; первоначально запущенные компоненты остаются запущенными. Тест не доказывает WAN или аварийное отключение всего узла.\n"
        )
        (ROOT / "test/presence-result.md").write_text(report)
    if not passed:
        raise RuntimeError("presence test failed")


if __name__ == "__main__":
    main()
