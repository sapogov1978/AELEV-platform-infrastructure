"""Exercise the prepared native delivery and preserve all pre-existing resources."""

from __future__ import annotations

import argparse
import json
import signal
import ssl
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
REPORT = Path(__file__).with_name("lifecycle-result.md")
Inventory = dict[str, dict[str, dict[str, str]]]


class RejectRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def execute(arguments: list[str]) -> str:
    result = subprocess.run(
        arguments, cwd=ROOT, capture_output=True, text=True, timeout=600
    )
    if result.returncode:
        # Expanded configuration, provision output and tokens must never be logged.
        raise RuntimeError(f"operator command failed, exit {result.returncode}")
    return result.stdout


def compose(*arguments: str) -> str:
    return execute(["docker", "compose", *arguments])


def inventory() -> Inventory:
    snapshot: Inventory = {}
    for kind, noun in (
        ("containers", "container"),
        ("networks", "network"),
        ("volumes", "volume"),
        ("images", "image"),
    ):
        arguments = ["docker", noun, "ls", "--format", "json"]
        if noun == "container":
            arguments.append("--all")
        if noun != "volume":
            arguments.append("--no-trunc")
        rows: dict[str, dict[str, str]] = {}
        for line in execute(arguments).splitlines():
            item = json.loads(line)
            identity = item["Name"] if noun == "volume" else item["ID"]
            if noun == "image":
                references = set(
                    rows.get(identity, {}).get("references", "").split(",")
                )
                references.discard("")
                references.add(item["Repository"] + ":" + item["Tag"])
                rows[identity] = {"references": ",".join(sorted(references))}
                continue
            labels = dict(
                entry.split("=", 1)
                for entry in item.get("Labels", "").split(",")
                if "=" in entry
            )
            rows[identity] = {
                "project": labels.get("com.docker.compose.project", ""),
                "state": item.get("State", ""),
                "name": item.get("Name", item.get("Names", "")),
                "anonymous": str("com.docker.volume.anonymous" in labels),
            }
        snapshot[kind] = rows
    return snapshot


def volume_mounts(containers: list[str]) -> set[str]:
    names: set[str] = set()
    for identity in containers:
        mounts = json.loads(
            execute(["docker", "inspect", "--format", "{{json .Mounts}}", identity])
        )
        names.update(item["Name"] for item in mounts if item["Type"] == "volume")
    return names


def preflight(before: Inventory, model: dict[str, Any], reuse: bool) -> None:
    project = model["name"]
    if any(item["project"] == project for item in before["containers"].values()):
        raise RuntimeError("existing native containers; test refused")
    volumes = {item["name"] for item in model["volumes"].values()}
    if volumes & before["volumes"].keys() and not reuse:
        raise RuntimeError("existing data requires explicit --reuse-existing-data")
    running = [
        key for key, row in before["containers"].items() if row["state"] == "running"
    ]
    if volumes & volume_mounts(running):
        raise RuntimeError("another running container writes native data")
    for image in {item["image"] for item in model["services"].values()}:
        identity = execute(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image]
        ).strip()
        if identity not in before["images"]:
            raise RuntimeError("prepare native images before testing")


def states() -> dict[str, dict[str, Any]]:
    output = compose("ps", "--all", "--format", "json").strip()
    rows = (
        json.loads(output)
        if output.startswith("[")
        else [json.loads(line) for line in output.splitlines()]
    )
    return {item["Service"]: item for item in rows}


def wait_ready() -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + 240
    expected = {"keycloak", "keycloak-database", "keycloak-status-publisher"}
    while time.monotonic() < deadline:
        current = states()
        if expected <= current.keys() and all(
            current[name]["State"] == "running" and current[name]["Health"] == "healthy"
            for name in expected
        ):
            return current
        time.sleep(2)
    raise RuntimeError("native healthcheck deadline exceeded")


def identity(model: dict[str, Any]) -> tuple[str, str]:
    runtime = model["services"]["keycloak"]["environment"]
    provision = model["services"]["keycloak-status-provision"]["environment"]
    prefix = "AELEV_INFRASTRUCTURE_KEYCLOAK_"
    base = runtime["KC_HOSTNAME"].rstrip("/")
    realm = provision[prefix + "UI_REALM"]
    mounts = model["services"]["keycloak-status-provision"]["volumes"]
    secret_root = next(
        Path(row["source"]) for row in mounts if row["target"] == "/run/secrets/status"
    )
    context = ssl.create_default_context(cafile=str(secret_root / "identity-ca.crt"))
    opener = build_opener(HTTPSHandler(context=context), RejectRedirect())

    def request(path: str, data: bytes | None = None, token: str | None = None) -> Any:
        headers = {"Authorization": "Bearer " + token} if token else {}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        with opener.open(
            Request(base + path, data=data, headers=headers),
            timeout=15,
        ) as response:
            return json.load(response)

    issuer = request(f"/realms/{realm}/.well-known/openid-configuration")["issuer"]
    if issuer != base + "/realms/" + realm:
        raise RuntimeError("unexpected native OIDC issuer")
    token = request(
        "/realms/master/protocol/openid-connect/token",
        urlencode(
            {
                "client_id": "admin-cli",
                "grant_type": "password",
                "username": provision[prefix + "ADMIN_USER"],
                "password": Path(model["secrets"]["keycloak-admin-password"]["file"])
                .read_text(encoding="utf-8")
                .strip(),
            }
        ).encode(),
    )["access_token"]
    client_id = provision[prefix + "UI_CLIENT_ID"]
    clients = request(
        f"/admin/realms/{realm}/clients?" + urlencode({"clientId": client_id}),
        token=token,
    )
    if len(clients) != 1 or clients[0]["clientId"] != client_id:
        raise RuntimeError("own publisher client absent or duplicated")
    return str(issuer), str(clients[0]["id"])


def cleanup(before: Inventory, project: str, witnessed: set[str]) -> None:
    compose("down")
    current = inventory()
    for noun, kind in (
        ("container", "containers"),
        ("network", "networks"),
        ("volume", "volumes"),
    ):
        for key, item in current[kind].items():
            if key in before[kind]:
                continue
            owned = item["project"] == project
            anonymous = (
                kind == "volumes"
                and key in witnessed
                and item["anonymous"] == "True"
                and not item["project"]
            )
            if owned or anonymous:
                execute(["docker", noun, "rm", key])
    after = inventory()
    for kind, rows in before.items():
        if any(after[kind].get(key) != row for key, row in rows.items()):
            raise RuntimeError("pre-existing resources or foreign states changed")
    for kind in ("containers", "networks", "volumes"):
        if any(
            row["project"] == project and key not in before[kind]
            for key, row in after[kind].items()
        ):
            raise RuntimeError("test-created native resources remain")
    if witnessed - before["volumes"].keys() & after["volumes"].keys():
        raise RuntimeError("test-created mounted volume remains")


def run(reuse: bool) -> int:
    started = datetime.now(timezone.utc).isoformat()
    checks: list[tuple[str, str]] = []
    before: Inventory = {}
    armed = False
    witnessed: set[str] = set()
    project = ""
    passed = False
    try:
        model = json.loads(compose("--profile", "*", "config", "--format", "json"))
        project = model["name"]
        before = inventory()
        preflight(before, model, reuse)
        checks.append(("Исходные ресурсы учтены; других писателей нет", "PASS"))
        armed = True
        compose(
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "240",
            "keycloak",
        )
        checks.append(
            ("Настоящие Keycloak, собственная БД, TLS и импорт realm", "PASS")
        )
        compose(
            "--profile",
            "provision",
            "run",
            "--rm",
            "--no-deps",
            "--pull",
            "never",
            "keycloak-status-provision",
        )
        checks.append(
            ("Настоящий собственный keycloak-status-provision: exit 0", "PASS")
        )
        compose("up", "-d", "--no-build", "--pull", "never")
        first = wait_ready()
        witnessed.update(volume_mounts([row["ID"] for row in first.values()]))
        original_identity = identity(model)
        checks.append(
            (
                "Три штатных healthcheck; HTTPS discovery и единственный собственный клиент",
                "PASS",
            )
        )
        compose("stop")
        if any(row["State"] == "running" for row in states().values()):
            raise RuntimeError("native containers still running after stop")
        checks.append(("Остановка всех постоянных компонентов", "PASS"))
        compose("up", "-d", "--no-build", "--pull", "never")
        restarted = wait_ready()
        if {name: row["ID"] for name, row in first.items()} != {
            name: row["ID"] for name, row in restarted.items()
        } or identity(model) != original_identity:
            raise RuntimeError("stop/up did not preserve containers and identity")
        checks.append(
            ("Stop/up: те же контейнеры, issuer и ID собственного клиента", "PASS")
        )
        compose("down")
        if any(row["project"] == project for row in inventory()["containers"].values()):
            raise RuntimeError("native containers remain after down")
        compose("up", "-d", "--no-build", "--pull", "never")
        recreated = wait_ready()
        witnessed.update(volume_mounts([row["ID"] for row in recreated.values()]))
        if (
            any(recreated[name]["ID"] == row["ID"] for name, row in first.items())
            or identity(model) != original_identity
        ):
            raise RuntimeError(
                "down/up did not recreate containers and preserve identity"
            )
        checks.append(("Down/up: новые контейнеры, прежние данные и identity", "PASS"))
        compose(
            "--profile",
            "provision",
            "run",
            "--rm",
            "--no-deps",
            "--pull",
            "never",
            "keycloak-status-provision",
        )
        if identity(model) != original_identity:
            raise RuntimeError("provision is not idempotent")
        checks.append(
            ("Повтор собственного provision: exit 0, прежний ID, без дубля", "PASS")
        )
        passed = True
    except (Exception, KeyboardInterrupt) as error:
        checks.append(
            (
                "Проверка прервана: "
                + (
                    str(error)
                    if isinstance(error, RuntimeError)
                    else type(error).__name__
                ),
                "FAIL",
            )
        )
    finally:
        if armed:
            try:
                owned = [
                    key
                    for key, row in inventory()["containers"].items()
                    if row["project"] == project
                ]
                witnessed.update(volume_mounts(owned))
                cleanup(before, project, witnessed)
                checks.append(
                    (
                        "Очистка созданного; все исходные ресурсы и чужие состояния сохранены",
                        "PASS",
                    )
                )
            except Exception:
                passed = False
                checks.append(("Очистка или сохранность исходных ресурсов", "FAIL"))
        result = "PASS" if passed else "FAIL"
        summary = ", ".join(f"{kind}: {len(rows)}" for kind, rows in before.items())
        REPORT.write_text(
            "# Проверка самостоятельного жизненного цикла Keycloak\n\n"
            f"Начало: {started}. Итог: {result}.\n\n"
            "Использованы собственные Compose/env, реальные образы, БД, TLS и init. Фиктивных компонентов и отключения TLS нет. Административные credentials и токены не выводились.\n\n"
            f"Повторное использование существующих данных разрешено: {reuse}. Исходная опись: {summary}.\n\n"
            "| Проверка | Результат |\n|---|---|\n"
            + "".join(f"| {step} | {status} |\n" for step, status in checks)
            + "\nРабочие env, секреты, truststore, образы и исходные тома сохранены. WAN, резервное восстановление, высокая доступность, продукционная PKI и доставка в UI не проверялись. Публикация в UI проверяется отдельным браузерным тестом; полный комплекс — тестом оркестрации.\n",
            encoding="utf-8",
        )
        print(f"{result}: {REPORT}")
    return 0 if passed else 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse-existing-data", action="store_true")
    raise SystemExit(run(parser.parse_args().reuse_existing_data))
