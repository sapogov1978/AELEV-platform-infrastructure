"""Check the existing real standalone installation; create no runtime resources."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / "deployment.json").read_text())
ROLE = SPEC["role"]
COMPOSE = ["docker", "compose", "--env-file", ".env", "--profile", "ui"]


def command(*args: str) -> str:
    result = subprocess.run(
        [*COMPOSE, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("AELEV_")},
        timeout=40,
    )
    return result.stdout


def check() -> int:
    checks: list[str] = []
    failure = ""
    try:
        raw = command("ps", "-a", "--format", "json")
        parsed: Any = (
            json.loads(raw)
            if raw.lstrip().startswith("[")
            else [json.loads(line) for line in raw.splitlines() if line]
        )
        rows = {row["Service"]: row for row in parsed}
        for name in (ROLE, ROLE + "-status-publisher"):
            assert rows[name]["State"] == "running", name + " is not running"
            if rows[name].get("Health"):
                assert rows[name]["Health"] == "healthy", name + " is not healthy"
        checks.append(
            "PASS: нативный процесс и собственный издатель running; имеющиеся Docker healthchecks healthy."
        )
        probe = json.loads(
            command(
                "exec",
                "-T",
                ROLE + "-status-publisher",
                "python",
                "-c",
                'import sys,json; sys.path.insert(0,"/opt/aelev"); import status_publisher as p; '
                "print(json.dumps(p.probe(p.Settings.environment(p.PREFIX))))",
            )
        )
        assert probe[0] == "ONLINE", "native readiness is not ONLINE"
        checks.append(
            "PASS: реальная HTTPS/mTLS-проба собственного процесса — ONLINE, без отключения проверки CA/SAN."
        )
        command(
            "exec",
            "-T",
            ROLE + "-status-publisher",
            "python",
            "/opt/aelev/status_publisher.py",
            "health",
        )
        publications = [
            json.loads(line)
            for line in command(
                "logs",
                "--since",
                "120s",
                "--no-log-prefix",
                "--no-color",
                ROLE + "-status-publisher",
            ).splitlines()
            if line.startswith("{")
        ]
        acknowledgements = [
            item
            for item in publications
            if item.get("event") == "status_published"
            and item.get("state") == "ONLINE"
            and item.get("sequence", 0) > 0
        ]
        assert acknowledgements, "no recent acknowledged ONLINE publication"
        checks.append(
            "PASS: свежая ONLINE-публикация подтверждена UI; sequence "
            + str(acknowledgements[-1]["sequence"])
            + ". Heartbeat отдельно подтверждает работу цикла, не выдаётся за квитанцию."
        )
        checks.append(
            "PASS: проверка использовала только собственный Compose; контейнеры, данные и секреты не создавались и не удалялись."
        )
    except (subprocess.SubprocessError, AssertionError, KeyError, ValueError) as error:
        failure = type(error).__name__ + ": проверка не выполнена или условие нарушено."
    text = [
        "# Проверка самостоятельного запуска " + ROLE,
        "",
        "Время UTC: " + datetime.now(timezone.utc).isoformat(),
        "",
        "Итог: " + ("FAIL" if failure else "PASS") + ".",
        "",
        *["- " + item for item in checks],
    ]
    if failure:
        text += ["", failure]
    text += [
        "",
        "Проверка запуска, не полный жизненный цикл. WAN, production PKI/ротация, нагрузка, отказоустойчивость и резервное восстановление этим прогоном не проверялись.",
        "",
    ]
    (ROOT / "test/startup-result.md").write_text("\n".join(text))
    print(
        json.dumps(
            {
                "role": ROLE,
                "result": "FAIL" if failure else "PASS",
                "checks": len(checks),
            }
        )
    )
    return int(bool(failure))


if __name__ == "__main__":
    raise SystemExit(check())
