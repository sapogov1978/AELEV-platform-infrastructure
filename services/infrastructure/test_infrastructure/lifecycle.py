"""Test the actual shared infrastructure, then remove only created resources."""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.parse import urlsplit

from resources import (
    Inventory,
    cleanup_plan,
    inventory,
    mounted_volumes,
    owned_containers,
    preflight,
    verify_preserved,
)

ROOT = Path(__file__).resolve().parents[1]
REPORT = Path(__file__).with_name("lifecycle-result.md")
PROFILES = ["--profile", "ui"]


def interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def execute(arguments: list[str], cwd: Path = ROOT) -> str:
    result = subprocess.run(
        arguments, cwd=cwd, capture_output=True, text=True, timeout=1200
    )
    if result.returncode:
        # Compose diagnostics may contain expanded credentials; do not echo them.
        raise RuntimeError(
            "command failed (exit "
            + str(result.returncode)
            + "): "
            + " ".join(arguments)
        )
    return result.stdout


def compose_prefix() -> list[str]:
    return ["docker", "compose", "-f", str(ROOT / "compose.yml")]


def model_validation(model: dict[str, Any]) -> None:
    services = model["services"]
    if list(services).count("kafka") != 1:
        raise RuntimeError("exactly one canonical Kafka is required")
    for name in ("postgresql-1", "postgresql-2", "postgresql-3"):
        row = services[name]
        mechanism = row["environment"]["AELEV_INFRASTRUCTURE_POSTGRESQL_FENCING_MODE"]
        if mechanism not in {"docker", "kubernetes", "watchdog"}:
            raise RuntimeError("explicit PostgreSQL fencing is required")
        if bool(row.get("devices")) != (mechanism == "watchdog"):
            raise RuntimeError("PostgreSQL device does not match selected fencing")
        if mechanism == "docker" and "postgresql-fencer" not in services:
            raise RuntimeError("Docker fencing executor disappeared")
    if services["translator-topic-provisioner"]["depends_on"]["kafka"] != {
        "condition": "service_healthy",
        "required": True,
    }:
        raise RuntimeError("Kafka readiness barrier disappeared")
    for name in (
        "translator-api",
        "translator-priority-consumer",
        "translator-normal-consumer",
        "translator-outbox",
    ):
        if (
            services[name]["depends_on"]["translator-topic-provisioner"]["condition"]
            != "service_completed_successfully"
        ):
            raise RuntimeError("topic provisioning barrier disappeared")
    if "configuration-applier" in services:
        raise RuntimeError("unbound generic configuration template")
    if any(row.get("external") for row in model.get("networks", {}).values()):
        raise RuntimeError("clean combined start must not require external networks")
    for row in services.values():
        for mount in row.get("volumes", []):
            if mount["type"] == "bind":
                source = Path(mount["source"])
                own_socket = (
                    row.get("command") == ["fencing-serve"]
                    and mount.get("target") == "/run/docker.sock"
                )
                if (
                    not source.exists() or not source.is_relative_to(ROOT)
                ) and not own_socket:
                    raise RuntimeError("missing or nonportable owner mount")
        if row.get("restart") not in {
            "no",
            "on-failure",
            "on-failure:3",
            "unless-stopped",
        }:
            raise RuntimeError("missing canonical restart policy")


def states(project: str) -> dict[str, dict[str, Any]]:
    identities = execute(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=com.docker.compose.project=" + project,
        ]
    ).splitlines()
    result: dict[str, dict[str, Any]] = {}
    for identity in identities:
        name = execute(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "com.docker.compose.service"}}',
                identity,
            ]
        ).strip()
        result[name] = json.loads(
            execute(["docker", "inspect", "--format", "{{json .State}}", identity])
        )
    return result


def validate_owner_models(model: dict[str, Any]) -> None:
    for owner in (
        "keycloak",
        "platform-ui",
        "observability",
        "translator-service",
        "postgresql",
        "public-egress-gateway",
    ):
        native = json.loads(
            execute(
                ["docker", "compose", "--profile", "*", "config", "--format", "json"],
                cwd=ROOT / owner,
            )
        )
        for name, service in native["services"].items():
            if model["services"].get(name) != service:
                raise RuntimeError(
                    "owner/orchestrated model differs: " + owner + "/" + name
                )


def wait_ready(project: str, services: dict[str, Any], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = states(project)
        pending = []
        for name, description in services.items():
            state = current.get(name, {})
            if description["restart"] in {"no", "on-failure"}:
                ready = state.get("Status") == "exited" and state.get("ExitCode") == 0
            else:
                ready = state.get("Running") is True
                if description.get("healthcheck"):
                    ready = ready and state.get("Health", {}).get("Status") == "healthy"
            if not ready:
                pending.append(name)
        if not pending:
            return
        time.sleep(3)
    raise RuntimeError("readiness deadline: " + ", ".join(sorted(pending)))


def ui_snapshot(args: argparse.Namespace, ids: set[str]) -> dict[str, Any]:
    # Model/resource regressions do not execute a browser. Load its platform-
    # specific dependency only for the actual operator UI exercise.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch(
            executable_path=str(args.chromium), headless=True
        )
        try:
            page = browser.new_page(
                ignore_https_errors=args.allow_local_certificate,
                viewport={"width": 1440, "height": 1100},
            )
            page.goto(args.ui_url)
            page.locator("#username").fill(args.viewer_user)
            password = args.viewer_password_file.read_text().strip()
            try:
                page.locator("#password").fill(password)
            except Exception:
                raise RuntimeError("password entry failed") from None
            del password
            with page.expect_response(
                lambda response: response.url == args.ui_url + "/api/modules"
            ) as initial:
                page.locator("#kc-login").click()
            if initial.value.status != 200:
                raise RuntimeError("real OIDC viewer request failed")
            authorization = initial.value.request.all_headers()["authorization"]
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                response = page.request.get(
                    args.ui_url + "/api/modules",
                    headers={"Authorization": authorization},
                )
                if response.status == 200:
                    rows = {
                        row["module_id"]: row
                        for row in response.json()["modules"]
                        if row["module_id"] in ids
                    }
                    if set(rows) == ids and all(
                        row["service_kind"] == "INFRASTRUCTURE"
                        and not row["sections"]
                        and row["status"]["state"] == "ONLINE"
                        and row["components"]
                        and all(
                            part["state"] == "ONLINE"
                            for part in row["status"]["components"]
                        )
                        for row in rows.values()
                    ):
                        with page.expect_response(
                            lambda reply: reply.url == args.ui_url + "/api/modules"
                        ) as refreshed:
                            page.reload()
                        if refreshed.value.status != 200:
                            raise RuntimeError("authenticated UI reload failed")
                        for identity, row in rows.items():
                            group = page.locator(
                                'tbody[data-module-id="' + identity + '"]'
                            )
                            group.wait_for(state="visible")
                            toggle = group.locator("button[aria-expanded]")
                            if toggle.get_attribute("aria-expanded") != "true":
                                toggle.click()
                            if group.locator(".status-component").count() != len(
                                row["components"]
                            ):
                                raise RuntimeError("API/DOM composition differs")
                        page.screenshot(
                            path=str(REPORT.with_suffix(".png")), full_page=True
                        )
                        return rows
                time.sleep(3)
            raise RuntimeError("injected state did not become ONLINE")
        finally:
            browser.close()


def assert_sequence_advanced(previous: dict[str, Any], current: dict[str, Any]) -> None:
    if previous.keys() != current.keys():
        raise RuntimeError("publisher identities changed")
    for identity, row in previous.items():
        if current[identity]["status"]["sequence"] <= row["status"]["sequence"]:
            raise RuntimeError("publisher sequence did not advance")
        if current[identity]["components"] != row["components"]:
            raise RuntimeError("owner composition changed")


def cleanup(
    before: Inventory, project: str, witnessed: set[str], compose: list[str]
) -> dict[str, int]:
    current = inventory(execute)
    containers = owned_containers(current, project)
    witnessed.update(mounted_volumes(execute, containers))
    try:
        execute([*compose, *PROFILES, "down"])
    except RuntimeError:
        # Still remove only resources proved to be created by this run.
        pass
    plan = cleanup_plan(before, inventory(execute), project, witnessed)
    # Every remaining resource is individually owned and absent from the baseline.
    if plan["containers"]:
        execute(["docker", "container", "rm", "-f", *plan["containers"]])
    if plan["networks"]:
        execute(["docker", "network", "rm", *plan["networks"]])
    if plan["volumes"]:
        execute(["docker", "volume", "rm", *plan["volumes"]])
    after = inventory(execute)
    verify_preserved(before, after, project)
    for name in witnessed - before["volumes"].keys():
        if name in after["volumes"]:
            raise RuntimeError("created mounted volume remains")
    return {kind: len(before[kind]) for kind in before}


def run(args: argparse.Namespace) -> int:
    started = datetime.now(timezone.utc)
    checks: list[tuple[str, str]] = []
    before: Inventory | None = None
    prefix = compose_prefix()
    project = ""
    witnessed: set[str] = set()
    rows: dict[str, Any] = {}
    protected: dict[str, int] = {}
    failure = ""
    admitted = False

    def passed(name: str, detail: str = "") -> None:
        checks.append((name, detail))
        print("PASS: " + name, flush=True)

    try:
        normal = json.loads(
            execute([*compose_prefix(), "--profile", "*", "config", "--format", "json"])
        )
        model_validation(normal)
        passed(
            "Каноническая композиция, один Kafka и явно выбранное ограждение PostgreSQL"
        )
        validate_owner_models(normal)
        passed("Самостоятельные и оркестрируемые роли владельцев полностью совпадают")
        model = json.loads(
            execute([*prefix, "--profile", "*", "config", "--format", "json"])
        )
        project = model["name"]
        regular_names = set(
            execute([*prefix, *PROFILES, "config", "--services"]).splitlines()
        )
        regular = {
            name: row
            for name, row in model["services"].items()
            if name in regular_names
        }
        ids = {
            str(value)
            for name, row in regular.items()
            if name.endswith("-status-publisher")
            for key, value in row.get("environment", {}).items()
            if key.endswith("_UI_MODULE_ID")
        }
        if not ids:
            raise RuntimeError("no owner publisher identities in model")
        before = inventory(execute)
        preflight(execute, before, model, args.reuse_existing_data)
        admitted = True
        passed("Исходные ресурсы учтены; существующих писателей общих томов нет")
        execute([*prefix, "up", "-d", "--no-build", "--pull", "never", "kafka"])
        wait_ready(project, {"kafka": regular["kafka"]}, args.timeout)
        if any(name.startswith("translator-") for name in states(project)):
            raise RuntimeError("Kafka startup unexpectedly created translator roles")
        passed("Kafka самостоятельно healthy: ни одна роль переводчика не запущена")
        for task in sorted(
            name
            for name in model["services"]
            if name.endswith("-configuration-init")
            and name != "platform-ui-configuration-init"
        ):
            execute([*prefix, "--profile", "provision", "run", "--rm", task])
            passed("Настоящая инициализация собственного тома: " + task + " — exit 0")
        execute([*prefix, "up", "-d", "--no-build", "--pull", "never", "keycloak"])
        wait_ready(
            project,
            {name: regular[name] for name in ("keycloak-database", "keycloak")},
            args.timeout,
        )
        passed("Настоящие Keycloak, его БД, TLS и импорт собственного realm")
        for task in ["platform-ui-configuration-init"] + sorted(
            name for name in model["services"] if name.endswith("-status-provision")
        ):
            execute([*prefix, "--profile", "provision", "run", "--rm", task])
            passed("Реальная задача владельца: " + task + " — exit 0")
        execute(
            [
                *prefix,
                "--profile",
                "provision",
                "run",
                "--rm",
                "site-replication-applier",
            ]
        )
        passed("Реальная проверка/настройка MinIO site replication: exit 0")
        execute([*prefix, *PROFILES, "up", "-d", "--no-build", "--pull", "never"])
        wait_ready(project, regular, args.timeout)
        passed(
            "Общий старт: healthcheck, автоматические миграции и topics готовы",
            str(len(regular))
            + " ролей; "
            + str(sum(bool(row.get("healthcheck")) for row in regular.values()))
            + " healthcheck; остальные постоянные роли RUNNING и далее проверяются владельцами через UI",
        )
        rows = ui_snapshot(args, ids)
        passed(
            "Настоящие OIDC, API и DOM: все собственные строки ONLINE",
            str(len(rows)) + " строк",
        )
        first_ids = set(owned_containers(inventory(execute), project))
        execute([*prefix, *PROFILES, "stop"])
        if any(row.get("Running") for row in states(project).values()):
            raise RuntimeError("a project container is still running after stop")
        passed("Общая остановка: ни один контейнер группы не работает")
        execute([*prefix, *PROFILES, "up", "-d", "--no-build", "--pull", "never"])
        wait_ready(project, regular, args.timeout)
        resumed = ui_snapshot(args, ids)
        assert_sequence_advanced(rows, resumed)
        if set(owned_containers(inventory(execute), project)) != first_ids:
            raise RuntimeError("stop/up unexpectedly replaced containers")
        rows = resumed
        passed(
            "Повторный старт: прежние контейнеры, identities и возрастающие последовательности"
        )
        current = inventory(execute)
        witnessed.update(mounted_volumes(execute, owned_containers(current, project)))
        execute([*prefix, *PROFILES, "down"])
        if owned_containers(inventory(execute), project):
            raise RuntimeError("project containers remain after down")
        data_names = {row["name"] for row in model["volumes"].values()}
        if (
            data_names.intersection(current["volumes"])
            - inventory(execute)["volumes"].keys()
        ):
            raise RuntimeError("persistent data missing")
        passed("Общее down сняло контейнеры; постоянные тома сохранены")
        execute([*prefix, *PROFILES, "up", "-d", "--no-build", "--pull", "never"])
        wait_ready(project, regular, args.timeout)
        recreated = ui_snapshot(args, ids)
        assert_sequence_advanced(rows, recreated)
        if first_ids & set(owned_containers(inventory(execute), project)):
            raise RuntimeError("down/up did not recreate all containers")
        rows = recreated
        passed("Down/up: новые контейнеры работают с прежними данными и identities")
    except (Exception, KeyboardInterrupt) as error:
        failure = type(error).__name__
        if isinstance(error, RuntimeError):
            failure += ": " + str(error)
        print("FAIL: " + failure, flush=True)
    finally:
        if admitted and before is not None:
            try:
                protected = cleanup(before, project, witnessed, prefix)
                passed(
                    "Очистка: созданное снято, все исходные ресурсы и чужие состояния сохранены"
                )
            except Exception as error:
                failure += "; cleanup: " + type(error).__name__ + ": " + str(error)
                print("FAIL: cleanup", flush=True)
        result = "FAIL" if failure else "PASS"
        report = "# Проверка общей инфраструктурной оркестрации\n\n"
        report += f"Начало: {started.isoformat()}. Итог: {result}.\n\n"
        report += "Использованы реальные поставки и собственные env, удостоверения и тома. Поддельных Kafka, IdP, API или издателей нет. Данные существовавших до запуска томов не удалялись; технические отчёты и реальные задачи владельцев обновлялись штатно.\n\n"
        report += f"Механизм ограждения берётся из собственного env PostgreSQL, тест его не отключает. Повторное использование существующих данных: {args.reuse_existing_data}. WAN, аппаратное ограждение, продукционная PKI, нагрузка и независимое резервное восстановление не проверялись.\n\n"
        report += "| Проверка | Результат | Подробность |\n|---|---|---|\n"
        for name, detail in checks:
            report += f"| {name} | PASS | {detail} |\n"
        if failure:
            report += f"\nОтказ: {failure}. Невыполненные последующие этапы не считаются успешными.\n"
        if rows:
            report += "\n| Название владельца | Компоненты | Последовательность |\n|---|---|---|\n"
            for row in rows.values():
                title = row["title"]["ru"].replace("|", "\\|")
                report += f"| {title} | {len(row['components'])} ONLINE | {row['status']['sequence']} |\n"
            report += "\n[Страница состояния](lifecycle-result.png).\n"
        if protected:
            report += (
                "\nСверены сохранённые исходные ресурсы: "
                + ", ".join(f"{kind}: {count}" for kind, count in protected.items())
                + ". Созданных контейнеров, сетей и томов теста не осталось; рабочие env, секреты и образы сохранены.\n"
            )
        REPORT.write_text(report)
    return int(bool(failure))


def main() -> int:
    signal.signal(signal.SIGTERM, interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui-url", required=True)
    parser.add_argument("--viewer-user", required=True)
    parser.add_argument("--viewer-password-file", type=Path, required=True)
    parser.add_argument("--chromium", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--reuse-existing-data", action="store_true")
    parser.add_argument("--allow-local-certificate", action="store_true")
    args = parser.parse_args()
    if urlsplit(args.ui_url).scheme != "https" or args.timeout <= 0:
        parser.error("HTTPS UI and a positive timeout required")
    if args.allow_local_certificate and urlsplit(args.ui_url).hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        parser.error("certificate exception is limited to loopback")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
