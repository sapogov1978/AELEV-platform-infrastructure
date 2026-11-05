"""Read-only verification of real protected connections between owner products."""

from __future__ import annotations

import base64
import json
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def env(role: str) -> dict[str, str]:
    prefix = "AELEV_INFRASTRUCTURE_" + role.replace("-", "_").upper() + "_"
    return {
        key.removeprefix(prefix): value
        for line in (ROOT / role / ".env").read_text().splitlines()
        if line.startswith(prefix)
        for key, _, value in [line.partition("=")]
    }


def request(role: str, path: str, *, authenticated: bool = False) -> Any:
    settings = env(role)
    secrets = ROOT / role / settings["SECRET_ROOT"]
    context = ssl.create_default_context(cafile=str(secrets / "ca.crt"))
    context.load_cert_chain(secrets / "client.crt", secrets / "client.key")
    url = "https://" + settings["PUBLIC_HOST"] + ":" + settings["PORT"] + path
    headers = {}
    if authenticated:
        credentials = (
            settings["ADMIN_USER"]
            + ":"
            + (secrets / "admin-password").read_text().strip()
        )
        headers["Authorization"] = (
            "Basic " + base64.b64encode(credentials.encode()).decode()
        )
    with urlopen(
        Request(url, headers=headers), context=context, timeout=15
    ) as response:
        return json.loads(response.read())


def query(expression: str) -> list[dict[str, Any]]:
    result = request("prometheus", "/api/v1/query?" + urlencode({"query": expression}))
    assert result["status"] == "success", "Prometheus query failed"
    return list(result["data"]["result"])


def check() -> int:
    checks: list[str] = []
    failure = ""
    try:
        metrics = query('up{job!="infrastructure-health",job!~"owner-.*"}')
        for role in (
            "prometheus",
            "delivery",
            "otel-collector",
            "blackbox-exporter",
            "loki",
            "tempo",
        ):
            assert any(
                row["metric"]["job"] == role and row["value"][1] == "1"
                for row in metrics
            ), ("missing healthy scrape: " + role)
        checks.append(
            "PASS: Prometheus читает реальные метрики шести технических продуктов по HTTPS/mTLS через собственную сеть или опубликованные порты."
        )
        probes = query('probe_success{job="infrastructure-health"}')
        settings = env("prometheus")
        for key in (
            "PROMETHEUS",
            "DELIVERY",
            "COLLECTOR",
            "BLACKBOX",
            "LOKI",
            "TEMPO",
            "GRAFANA",
        ):
            target = settings[key + "_HEALTH_URL"]
            assert any(
                row["metric"]["instance"] == target and row["value"][1] == "1"
                for row in probes
            ), ("failed protected readiness probe: " + key)
        checks.append(
            "PASS: независимый Blackbox Exporter подтверждает семь опубликованных HTTPS readiness-интерфейсов; общей сети с ними нет."
        )
        for uid in ("aelev-prometheus", "aelev-loki"):
            result = request(
                "grafana", "/api/datasources/uid/" + uid + "/health", authenticated=True
            )
            assert result["status"] == "OK", "Grafana datasource is not ready: " + uid
        # Tempo plugin does not implement the optional datasource-health method.
        tempo = request(
            "grafana",
            "/api/datasources/proxy/uid/aelev-tempo/api/search?limit=1",
            authenticated=True,
        )
        assert isinstance(tempo.get("traces"), list), "Grafana Tempo query failed"
        checks.append(
            "PASS: Grafana выполняет настоящие защищённые запросы ко всем трём источникам данных через опубликованные адреса."
        )
    except Exception as error:
        failure = type(error).__name__ + ": защищённая связь не подтверждена."
    lines = [
        "# Проверка связей самостоятельных продуктов наблюдения",
        "",
        "Время UTC: " + datetime.now(timezone.utc).isoformat(),
        "",
        "Итог: " + ("FAIL" if failure else "PASS") + ".",
        "",
        *["- " + row for row in checks],
    ]
    if failure:
        lines += ["", failure]
    lines += [
        "",
        "Запросы только чтения: нет тестовых пакетов, учётных записей, контейнеров или удаления данных. Это проверка опубликованных маршрутов на Docker Desktop, не доказательство WAN/production PKI, полного воспроизведения трёх сигналов, нагрузки или резервного восстановления. Почта выключена и не проверялась.",
        "",
    ]
    (ROOT / "test/routes-result.md").write_text("\n".join(lines))
    print(
        json.dumps(
            {
                "result": "FAIL" if failure else "PASS",
                "checks": len(checks),
                "error_type": failure.split(":")[0],
            }
        )
    )
    return int(bool(failure))


if __name__ == "__main__":
    raise SystemExit(check())
