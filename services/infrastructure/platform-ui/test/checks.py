"""Build the own test image and retain human-readable results; no daemon mutation in unit tests."""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prepare import settings  # noqa: E402


def main() -> None:
    cfg = settings(ROOT / ".env")
    image = "aelev/platform-ui-checks:local"
    started = time.monotonic()
    build = [
        "docker",
        "build",
        "--target",
        "checks",
        "--build-context",
        "tests=./test",
        "-t",
        image,
    ]
    for key in ("NODE_IMAGE", "NGINX_IMAGE", "PYTHON_IMAGE"):
        build.extend(["--build-arg", key + "=" + cfg[key]])
    subprocess.run([*build, "."], cwd=ROOT, check=True)
    checks = [
        ["npm", "test"],
        ["npm", "run", "build"],
        ["docker", "compose", "--env-file", ".env", "config", "--quiet"],
    ]
    for args in (
        [
            "black",
            "--check",
            "api.py",
            "prepare.py",
            "provision.py",
            "health.py",
            "publisher_authority.py",
            "test",
        ],
        [
            "ruff",
            "check",
            "api.py",
            "prepare.py",
            "provision.py",
            "health.py",
            "publisher_authority.py",
            "test",
        ],
        [
            "mypy",
            "api.py",
            "prepare.py",
            "provision.py",
            "health.py",
            "publisher_authority.py",
        ],
        ["pytest", "-q"],
    ):
        checks.append(["docker", "run", "--rm", image, *args])
    rows = []
    failed = False
    for check in checks:
        result = subprocess.run(check, cwd=ROOT, capture_output=True, text=True)
        passed = result.returncode == 0
        failed = failed or not passed
        output = (result.stdout + result.stderr).replace(
            str(ROOT), "<source-repository>/services/infrastructure/platform-ui"
        )
        rows.append((check, passed, output))
        print(("PASS: " if passed else "FAIL: ") + " ".join(check), flush=True)
    report = (
        "# Platform UI — проверки поставки\n\n"
        + f"Дата: {datetime.now(timezone.utc).isoformat()}. Итог: {'FAIL' if failed else 'PASS'}. Длительность: {time.monotonic() - started:.1f} с.\n\nPython-проверки выполнены на Python 3.13 в собственном образе без Docker socket, сервер API использует те же закреплённые версии. Frontend проверен собственными Vitest/TypeScript/Vite.\n\n"
    )
    for check, passed, output in rows:
        report += f"## {'PASS' if passed else 'FAIL'}: `{' '.join(check)}`\n\n```text\n{output.strip()}\n```\n\n"
    report += "Проверка конфигурации не является запуском общей оркестрации. Настоящий вход, TLS/mTLS, изоляция и очистка проверяются отдельно в [жизненном цикле](lifecycle-result.md).\n"
    (ROOT / "test/checks-result.md").write_text(report)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
