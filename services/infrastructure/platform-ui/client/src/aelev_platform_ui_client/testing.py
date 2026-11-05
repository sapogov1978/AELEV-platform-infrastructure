"""Public browser-test support; owners supply all component and Compose mappings."""

from __future__ import annotations

import argparse
import json
from importlib import import_module
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def lifecycle(
    root: Path,
    prefix: str,
    components: dict[str, str],
    compose_options: list[str] | None = None,
    presence_ttl: int = 0,
    receiver: bool = False,
    compose_root: Path | None = None,
    verify_peers: bool = False,
) -> None:
    """Stop only already-running owner containers and restore in every outcome."""
    browser_support = import_module("playwright.sync_api")
    expect, sync_playwright = browser_support.expect, browser_support.sync_playwright

    parser = argparse.ArgumentParser()
    parser.add_argument("--ui-url", required=True)
    parser.add_argument("--viewer-user", required=True)
    parser.add_argument("--viewer-password-file", required=True, type=Path)
    parser.add_argument("--chromium", required=True, type=Path)
    parser.add_argument("--status-ttl-seconds", required=True, type=int)
    parser.add_argument("--component", action="append")
    parser.add_argument("--allow-local-certificate", action="store_true")
    args = parser.parse_args()
    if args.allow_local_certificate:
        from urllib.parse import urlsplit

        if urlsplit(args.ui_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            parser.error("certificate exception requires explicit loopback")
    if args.status_ttl_seconds <= 0:
        parser.error("positive receiver TTL required")
    selected = args.component or list(components)
    if set(selected).difference(components):
        parser.error("unknown owner component")
    env = dict(
        line.split("=", 1)
        for line in (root / ".env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    module_id = env[prefix + "UI_MODULE_ID"]
    command = [
        "docker",
        "compose",
        "--env-file",
        ".env",
        *(compose_options or []),
        "--profile",
        "ui",
    ]
    stages: list[str] = []
    started = time.monotonic()
    passed = False
    restore: str | None = None

    def docker(*arguments: str) -> str:
        result = subprocess.run(
            [*command, *arguments],
            cwd=compose_root or root,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.stdout

    def checked(value: bool, label: str) -> None:
        if not value:
            raise AssertionError(label)
        stages.append(label)
        print("PASS: " + label, flush=True)

    try:
        entries = [
            json.loads(line)
            for line in docker("ps", "--format", "json").splitlines()
            if line
        ]
        baseline = {
            entry["Service"]: entry["ID"]
            for entry in entries
            if entry["State"] == "running" and entry["Service"] in components.values()
        }
        checked(
            set(components.values()) <= set(baseline),
            "Все проверяемые контейнеры существовали и работали до теста",
        )
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
                authorization = ""
                logged_in = False

                def login() -> None:
                    nonlocal authorization, logged_in
                    with page.expect_response(
                        lambda reply: reply.url == args.ui_url + "/api/modules"
                    ) as reply:
                        page.goto(args.ui_url)
                        if not logged_in:
                            page.locator("#username").fill(args.viewer_user)
                            page.locator("#password").fill(
                                args.viewer_password_file.read_text().strip()
                            )
                            page.locator("#kc-login").click()
                    checked(
                        reply.value.status == 200,
                        "Настоящий OIDC-вход и авторизованное чтение состояния",
                    )
                    authorization = reply.value.request.all_headers()["authorization"]
                    logged_in = True

                def modules() -> list[dict[str, Any]]:
                    response = page.request.get(
                        args.ui_url + "/api/modules",
                        headers={"Authorization": authorization},
                    )
                    if response.status == 401:
                        login()
                        response = page.request.get(
                            args.ui_url + "/api/modules",
                            headers={"Authorization": authorization},
                        )
                    if response.status != 200:
                        raise AssertionError("viewer request rejected")
                    return list(response.json()["modules"])

                def module() -> dict[str, Any]:
                    matches = [
                        entry for entry in modules() if entry["module_id"] == module_id
                    ]
                    if len(matches) != 1:
                        raise AssertionError("owner module missing or ambiguous")
                    return dict(matches[0])

                def await_state(component: str, positive: bool) -> str:
                    deadline = (
                        time.monotonic()
                        + max(args.status_ttl_seconds, presence_ttl)
                        + 150
                    )
                    while time.monotonic() < deadline:
                        state = module()["status"]
                        rows = {
                            part["id"]: part["state"] for part in state["components"]
                        }
                        if (rows[component] == "ONLINE") == positive:
                            return str(rows[component])
                        time.sleep(2)
                    raise AssertionError("component state did not change: " + component)

                def visible(component: str, state: str) -> None:
                    group = page.locator('tbody[data-module-id="' + module_id + '"]')
                    toggle = group.locator("button[aria-expanded]")
                    if toggle.get_attribute("aria-expanded") == "false":
                        toggle.click()
                    declaration = module()["components"]
                    index = next(
                        index
                        for index, part in enumerate(declaration)
                        if part["id"] == component
                    )
                    pill = (
                        group.locator(".status-component")
                        .nth(index)
                        .locator(".status-pill")
                    )
                    expect(pill).to_have_class("status-pill " + state.lower())

                def poll() -> None:
                    page.clock.set_fixed_time(datetime.now(timezone.utc))
                    with page.expect_response(
                        lambda reply: reply.url == args.ui_url + "/api/modules"
                    ):
                        page.clock.fast_forward(60000)

                login()
                initial = module()
                checked(
                    initial["service_kind"] == "INFRASTRUCTURE",
                    "Владелец объявил инфраструктурный тип",
                )
                checked(
                    not initial["sections"],
                    "Владелец не создаёт функциональные разделы",
                )
                for component in components:
                    await_state(component, True)
                checked(
                    module()["status"]["state"] == "ONLINE",
                    "Исходное состояние всех частей подтверждено реальными пробами",
                )
                poll()
                for component in selected:
                    login()  # Acquire a fresh short-lived viewer token for each phase.
                    service = components[component]
                    restore = service
                    peers = (
                        {
                            entry["module_id"]: entry["status"]["observed_at"]
                            for entry in modules()
                            if entry["module_id"] != module_id
                            and entry["status"]["state"] == "ONLINE"
                        }
                        if verify_peers
                        else {}
                    )
                    docker("stop", service)
                    try:
                        if component == "publisher":
                            deadline = time.monotonic() + 45
                            while time.monotonic() < deadline:
                                if not any(
                                    entry["module_id"] == module_id
                                    for entry in modules()
                                ):
                                    break
                                time.sleep(1)
                            else:
                                raise AssertionError(
                                    "graceful publisher withdrawal was not confirmed"
                                )
                            poll()
                            expect(
                                page.locator(
                                    'tbody[data-module-id="' + module_id + '"]'
                                )
                            ).to_have_count(0)
                            checked(
                                True,
                                service
                                + ": штатная отписка убрала собственную строку из API и браузера",
                            )
                        elif receiver:
                            # A stopped receiver cannot acknowledge its own outage.
                            page.clock.set_fixed_time(datetime.now(timezone.utc))
                            page.clock.fast_forward(60000)
                            group = page.locator(
                                'tbody[data-module-id="' + module_id + '"]'
                            )
                            expect(
                                group.locator(".status-pill.unknown").first
                            ).to_be_visible()
                            checked(
                                True,
                                service
                                + ": потеря приёмника видна как UNKNOWN в уже открытом браузере",
                            )
                        else:
                            state = await_state(component, False)
                            poll()
                            visible(component, state)
                            checked(
                                state in {"UNKNOWN", "OFFLINE"},
                                service
                                + ": реальная остановка изменила API и строку браузера на "
                                + state,
                            )
                            if peers:
                                deadline = time.monotonic() + 75
                                while time.monotonic() < deadline:
                                    current_peers = {
                                        entry["module_id"]: entry["status"]
                                        for entry in modules()
                                    }
                                    if all(
                                        identifier in current_peers
                                        and current_peers[identifier]["observed_at"]
                                        != previous
                                        for identifier, previous in peers.items()
                                    ):
                                        break
                                    time.sleep(2)
                                else:
                                    raise AssertionError(
                                        "peer publication stopped during owner outage"
                                    )
                                checked(
                                    True,
                                    service
                                    + ": остальные исходно доступные владельцы продолжают независимо публиковаться",
                                )
                    finally:
                        docker("start", service)
                        restore = None
                    if receiver and component != "publisher":
                        deadline = time.monotonic() + 90
                        while time.monotonic() < deadline:
                            try:
                                login()
                                break
                            except Exception:
                                time.sleep(2)
                        else:
                            raise AssertionError("receiver did not recover")
                    await_state(component, True)
                    poll()
                    visible(component, "ONLINE")
                    checked(True, service + ": после возврата строка браузера ONLINE")
                page.screenshot(
                    path=str(root / "test/status-publication.png"), full_page=True
                )
            finally:
                browser.close()
        after = [
            json.loads(line)
            for line in docker("ps", "--format", "json").splitlines()
            if line
        ]
        current = {
            entry["Service"]: entry["ID"]
            for entry in after
            if entry["State"] == "running"
        }
        checked(
            all(
                current.get(service) == identifier
                for service, identifier in baseline.items()
            ),
            "Исходные контейнеры возвращены без пересоздания и удаления данных",
        )
        passed = True
    finally:
        if restore is not None:
            docker("start", restore)
        report = (
            "# Проверка публикации собственного состояния\n\n"
            + f"Дата: {datetime.now(timezone.utc).isoformat()}. Итог: {'PASS' if passed else 'FAIL'}. Длительность: {time.monotonic() - started:.1f} с.\n\n"
            + f"Команда: `python test/status_publication.py` с явно переданными URL, Chromium, учётной записью просмотра и сроком свежести {args.status_ttl_seconds} с.\n\n"
            + "Проверяемые компоненты: "
            + ", ".join("`" + component + "`" for component in selected)
            + ".\n\n"
            + "| Этап | Результат |\n|---|---|\n"
            + "".join("| " + stage + " | PASS |\n" for stage in stages)
            + "\nИспользованы настоящие поставки и OIDC, не фикстуры каталога или тестовые Kafka. Тест не создаёт identities, контейнеры, сети или тома и восстанавливает остановленные исходные контейнеры. Удостоверения и данные не удаляются. TTL проверяется реальным серверным временем; управляемые часы ускоряют только минутный опрос браузера. Локальная проверка не доказывает WAN, fencing, HA или допуск PKI.\n"
        )
        if receiver:
            report += "\nПолностью остановленный приёмник не может показать свежий собственный отчёт: проверяется UNKNOWN в ранее открытом интерфейсе и ONLINE после восстановления.\n"
        (root / "test/status-publication-result.md").write_text(report)
    if not passed:
        raise RuntimeError("status publication failed")
