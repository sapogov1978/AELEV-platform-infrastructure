"""Real Compose lifecycle in temporary projects without live archive writes."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

OWNER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OWNER))

import prepare  # noqa: E402
import provision  # noqa: E402

PREFIX = "AELEV_INFRASTRUCTURE_PUBLIC_EGRESS_GATEWAY_"
NATIVE = "public-egress-gateway"
IDENTITY = "spiffe://example.test/workload/client-a"


def classify_command_failure(output: str) -> str:
    normalized = output.lower()
    classifications = (
        (
            "BIND_MOUNT_UNAVAILABLE",
            (
                "bind source path does not exist",
                "invalid mount config",
                "error while creating mount source path",
            ),
        ),
        ("HOST_PORT_CONFLICT", ("port is already allocated", "address already in use")),
        ("IMAGE_UNAVAILABLE", ("no such image", "manifest unknown")),
        ("DEPENDENCY_NOT_READY", ("dependency failed to start", "is unhealthy")),
        ("RUNTIME_PERMISSION_DENIED", ("permission denied",)),
    )
    for code, markers in classifications:
        if any(marker in normalized for marker in markers):
            return code
    return "PROCESS_FAILED"


def run(
    arguments: list[str],
    *,
    environment: dict[str, str] | None = None,
    directory: Path | None = None,
) -> str:
    result = subprocess.run(
        arguments,
        env=environment,
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode:
        # Build/Compose diagnostics may contain configuration; do not echo them.
        message = result.stderr.splitlines()[0] if result.stderr else "no diagnostic"
        # Only Compose model errors are exposed; never dump a rendered model.
        if "config" in arguments:
            raise RuntimeError("Compose config: " + message[:220])
        operation = next(
            (
                item
                for item in arguments
                if item in {"up", "run", "stop", "build", "down", "exec"}
            ),
            "command",
        )
        role = (
            arguments[-1]
            if arguments[-1].isidentifier() or arguments[-1].replace("-", "").isalnum()
            else "target"
        )
        reason = classify_command_failure(result.stdout + "\n" + result.stderr)
        raise RuntimeError(
            "command failed: " + operation + " " + role + "; reason=" + reason
        )
    return result.stdout


def inventory(kind: str) -> dict[str, dict[str, Any]]:
    listing = run(
        ["docker", kind, "ls", *(["-a"] if kind == "container" else []), "-q"]
    )
    identifiers = sorted(set(listing.split()))
    if not identifiers:
        return {}
    documents = json.loads(run(["docker", kind, "inspect", *identifiers]))
    return {row.get("Id") or row.get("ID") or row["Name"]: row for row in documents}


def project(row: dict[str, object], kind: str) -> str:
    config = (
        cast(dict[str, object], row.get("Config", {})) if kind == "container" else row
    )
    labels = cast(dict[str, str], config.get("Labels") or {})
    return labels.get("com.docker.compose.project", "")


def free_port() -> int:
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return cast(tuple[str, int], connection.getsockname())[1]


def context(secrets: Path, identity: str | None) -> ssl.SSLContext:
    result = ssl.create_default_context(cafile=str(secrets / "gateway/server-ca.pem"))
    if identity is not None:
        result.load_cert_chain(
            secrets / identity / "client.pem", secrets / identity / "client.key"
        )
    return result


def call(
    port: int,
    tls: ssl.SSLContext,
    method: str,
    path: str,
    document: dict[str, object] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPSConnection("localhost", port, context=tls, timeout=30)
    try:
        connection.request(
            method,
            path,
            body=json.dumps(document).encode() if document is not None else None,
            headers=(
                {"Content-Type": "application/json"} if document is not None else {}
            ),
        )
        response = connection.getresponse()
        return (
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            response.read(),
        )
    finally:
        connection.close()


def fetch(url: str, **changes: object) -> dict[str, object]:
    document: dict[str, object] = {
        "method": "GET",
        "url": url,
        "headers": {},
        "body": "",
        "max_bytes": 65536,
        "truncate": False,
    }
    return document | changes


def declaration(identity: str = IDENTITY) -> dict[str, object]:
    return {
        "schema_version": 1,
        "identity": identity,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
        "mode": "EXPLORATION",
        "max_response_bytes": 65536,
        "timeout_seconds": 15,
        "max_concurrency": 1,
        "per_host_interval_seconds": 0.1,
        "credential_scopes": {},
        "post_scopes": {},
    }


def applied(root: Path, value: dict[str, object]) -> None:
    source = root / "requested.json"
    source.write_text(json.dumps(value))
    (root / "audit").mkdir(exist_ok=True)
    provision.apply(source, root / "grants", root / "audit", "lifecycle-operator")


def validate_isolated_model(model: dict[str, Any], token: str) -> None:
    service = model["services"][NATIVE]
    missing = [
        mount["target"]
        for mount in service.get("volumes", [])
        if mount["type"] == "bind" and not Path(mount["source"]).exists()
    ]
    if missing:
        raise RuntimeError(
            "isolated lifecycle bind source missing: " + ", ".join(sorted(missing))
        )
    for kind in ("networks", "volumes"):
        foreign = [
            name
            for name, row in model.get(kind, {}).items()
            if not str(row.get("name", "")).startswith(token + "-")
        ]
        if foreign:
            raise RuntimeError(
                "isolated lifecycle has non-temporary "
                + kind
                + ": "
                + ", ".join(sorted(foreign))
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root-compose", type=Path, default=OWNER.parent / "compose.yml"
    )
    parser.add_argument(
        "--skip-root",
        action="store_true",
        help="standalone extracted repository only; root check remains open",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=OWNER / "test/lifecycle-result.md",
    )
    args = parser.parse_args()
    before = {
        kind: inventory(kind) for kind in ("container", "network", "volume", "image")
    }
    token = "aelev-egress-check-" + uuid.uuid4().hex[:12]
    native_image = "aelev/public-egress-check:" + token
    projects = {token, token + "-root"}
    checks: list[str] = []
    failure: str | None = None
    with tempfile.TemporaryDirectory(prefix=token + "-") as scratch:
        root = Path(scratch)
        extracted = root / "provider"
        shutil.copytree(
            OWNER,
            extracted,
            ignore=shutil.ignore_patterns(
                ".env",
                "secrets",
                "grants",
                "__pycache__",
                ".venv",
                ".pytest_cache",
                ".ruff_cache",
            ),
        )
        (root / "grants").mkdir(mode=0o755)
        prepare.initialize(root / "secrets", "public-egress-gateway", None)
        for name, identity in (
            ("client-a", IDENTITY),
            ("client-b", "spiffe://example.test/workload/client-b"),
            ("unknown", "spiffe://example.test/workload/unknown"),
        ):
            prepare.issue(
                root / "secrets/local-ca",
                root / "secrets" / name,
                "client",
                "URI:" + identity,
                "clientAuth",
            )
        port, docs_port = free_port(), free_port()
        values = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in (OWNER / ".env.example").read_text().splitlines()
            if line and not line.startswith("#")
        }
        values.update(
            {
                PREFIX + "COMPOSE_PROJECT": token,
                PREFIX + "IMAGE": native_image,
                PREFIX + "PORT": str(port),
                PREFIX + "PUBLIC_URL": f"https://localhost:{port}",
                PREFIX + "DOCS_PORT": str(docs_port),
                PREFIX + "DOCS_PUBLIC_URL": f"https://localhost:{docs_port}",
                PREFIX + "SECRET_ROOT": str(root / "secrets/gateway"),
                PREFIX + "SECRET_GID": str(os.getgid()),
                PREFIX + "GRANTS_ROOT": str(root / "grants"),
                PREFIX + "NETWORK": token + "-access",
                PREFIX + "PUBLIC_NETWORK": token + "-public",
                PREFIX + "UI_EGRESS_NETWORK": token + "-status",
                PREFIX + "UI_STATE_VOLUME": token + "-status",
                PREFIX + "UI_SECRET_ROOT": str(root / "status"),
                PREFIX + "UI_ADMIN_ROOT": str(root / "status-admin"),
                PREFIX + "TELEMETRY_SECRET_ROOT": str(root / "secrets/telemetry"),
                PREFIX + "TELEMETRY_SECRET_GID": str(os.getgid()),
                PREFIX + "TELEMETRY_VOLUME": token + "-telemetry",
                PREFIX + "TELEMETRY_NETWORK": token + "-telemetry",
            }
        )
        env_file = root / "settings.env"
        env_file.write_text(
            "\n".join(key + "=" + value for key, value in values.items()) + "\n"
        )
        environment = os.environ | values
        compose = [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-p",
            token,
            "-f",
            str(extracted / "compose.yml"),
        ]
        tls_a, tls_b, unknown, no_key = (
            context(root / "secrets", name)
            for name in ("client-a", "client-b", "unknown", None)
        )
        try:
            model = json.loads(
                run(compose + ["config", "--format", "json"], environment=environment)
            )
            validate_isolated_model(model, token)
            run(compose + ["build", NATIVE], environment=environment)
            checks.append(
                "Изолированная сборка только каталога провайдера: "
                "Python 3.13.7/Bookworm, без shared/соседних исходников."
            )
            applied(root, declaration())
            applied(root, declaration("spiffe://example.test/workload/client-b"))
            run(
                compose
                + [
                    "up",
                    "-d",
                    "--no-build",
                    "--pull",
                    "never",
                    "--wait",
                    "--wait-timeout",
                    "90",
                    NATIVE,
                ],
                environment=environment,
            )
            assert call(port, no_key, "GET", "/readyz")[0] == 200
            checks.append(
                "Собственный Compose: реальный HTTPS и native healthcheck healthy."
            )
            page = call(docs_port, no_key, "GET", "/docs")
            assert page[0] == 200 and b"swagger-ui-bundle.js" in page[2]
            assert "connect-src 'self'" in page[1]["content-security-policy"]
            document = json.loads(call(docs_port, no_key, "GET", "/openapi.json")[2])
            assert [tag["name"] for tag in document["tags"]] == [
                "default",
                "gateway",
                "metrics",
            ]
            assert document["paths"]["/v1/fetch"]["post"]["security"] == [
                {"workloadIdentity": []}
            ]
            assert document["paths"]["/v1/fetch"]["post"]["servers"] == [
                {"url": f"https://localhost:{port}"}
            ]
            for path in (
                "/docs/assets/swagger-ui-bundle.js",
                "/docs/assets/swagger-ui.css",
                "/docs/assets/initialize.js",
                "/metrics",
            ):
                assert call(docs_port, no_key, "GET", path)[0] == 200
            assert (
                call(
                    docs_port,
                    no_key,
                    "GET",
                    "/docs/assets/../secrets/gateway/server.key",
                )[0]
                == 404
            )
            checks.append(
                "Настоящие HTTPS Swagger/OpenAPI/локальные файлы доступны "
                "на отдельном read-only HTTPS-входе того же контейнера "
                "без запроса клиентского ключа; группы default → gateway → metrics "
                "и mTLS fetch сохранены."
            )
            for tls in (no_key, context(root / "secrets", "client-a")):
                assert (
                    call(
                        docs_port,
                        tls,
                        "POST",
                        "/v1/fetch",
                        fetch("https://example.com"),
                    )[0]
                    == 405
                )
            checks.append(
                "Вход документации отклоняет POST /v1/fetch (405), "
                "в том числе у разрешённого клиента; проксирования и обхода mTLS нет."
            )
            for tls in (no_key, unknown):
                assert (
                    call(port, tls, "POST", "/v1/fetch", fetch("https://example.com"))[
                        0
                    ]
                    == 403
                )
            checks.append("Fetch без сертификата и с неизвестным доверенным URI: 403.")
            # The native 3.13 client exercises TLS 1.3 independently of the host.
            response = run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    token + "-access",
                    "--mount",
                    "type=bind,src="
                    + str(root / "secrets/gateway/server-ca.pem")
                    + ",dst=/run/client/ca.pem,readonly",
                    "--mount",
                    "type=bind,src="
                    + str(root / "secrets/client-a/client.pem")
                    + ",dst=/run/client/client.pem,readonly",
                    "--mount",
                    "type=bind,src="
                    + str(root / "secrets/client-a/client.key")
                    + ",dst=/run/client/client.key,readonly",
                    "--group-add",
                    str(os.getgid()),
                    native_image,
                    "python",
                    "-c",
                    "import ssl,http.client,json; "
                    "c=ssl.create_default_context(cafile='/run/client/ca.pem'); "
                    "c.load_cert_chain('/run/client/client.pem',"
                    "'/run/client/client.key'); "
                    "h=http.client.HTTPSConnection('public-egress-gateway',8080,"
                    "context=c,timeout=30); h.connect(); "
                    "assert h.sock and h.sock.version()=='TLSv1.3'; "
                    "h.request('POST','/v1/fetch',body=json.dumps("
                    + repr(fetch("https://example.com/"))
                    + "),headers={'Content-Type':'application/json'}); "
                    "r=h.getresponse(); "
                    "assert r.status==200 and b'Example Domain' in r.read(); h.close()",
                ],
                environment=environment,
            )
            del response
            checks.append(
                "Python 3.13.7: реальный TLS 1.3/mTLS fetch example.com, "
                "без отключения hostname/CA; драйвер также не ограничивает TLS 1.3."
            )
            status, headers, content = call(
                port, tls_a, "POST", "/v1/fetch", fetch("https://example.com/")
            )
            assert (
                status == 200
                and headers.get("x-aelev-egress-result") == "ALLOWED"
                and b"Example Domain" in content
            )
            checks.append(
                "Настоящее публичное HTTPS-чтение example.com: "
                "status 200, ALLOWED, ожидаемые байты."
            )
            for url in (
                "http://127.0.0.1/",
                "http://169.254.169.254/",
                "https://10.0.0.1/",
                "https://metadata.google.internal/",
                "https://example.com:8443/",
            ):
                assert call(port, tls_a, "POST", "/v1/fetch", fetch(url))[0] == 403
            checks.append(
                "Private, loopback, metadata и нестандартный порт закрыто запрещены."
            )
            try:
                overflow_status, _, overflow_body = call(
                    port,
                    tls_a,
                    "POST",
                    "/v1/fetch",
                    fetch("https://example.com/", max_bytes=64),
                )
                assert (
                    overflow_status == 403
                    and json.loads(overflow_body)["code"] == "EGRESS_RESPONSE_TOO_LARGE"
                )
            except http.client.IncompleteRead as overflow:
                assert len(overflow.partial) <= 64
                assert "EGRESS_RESPONSE_TOO_LARGE" in run(
                    compose + ["logs", "--no-color", "--tail", "3", NATIVE],
                    environment=environment,
                )
            status, headers, content = call(
                port,
                tls_a,
                "POST",
                "/v1/fetch",
                fetch("https://example.com/", max_bytes=64, truncate=True),
            )
            assert (
                status == 200
                and len(content) == 64
                and headers.get("x-aelev-response-truncated") == "bounded"
            )
            checks.append(
                "Обычное превышение размера запрещено; "
                "явный ограниченный префикс возвращает ровно 64 байта."
            )
            # Hold one admission before JSON completion, not a fake upstream.
            held = tls_a.wrap_socket(
                socket.create_connection(("127.0.0.1", port)),
                server_hostname="localhost",
            )
            try:
                held.sendall(
                    b"POST /v1/fetch HTTP/1.1\r\nHost: localhost\r\n"
                    b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{"
                )
                until = time.monotonic() + 5
                while (
                    b'active_requests{mode="PROVIDER"} 1'
                    not in call(port, no_key, "GET", "/metrics")[2]
                ):
                    if time.monotonic() > until:
                        raise AssertionError("held admission was not observed")
                    time.sleep(0.1)
                assert (
                    call(port, tls_a, "POST", "/v1/fetch", fetch("http://127.0.0.1/"))[
                        0
                    ]
                    == 429
                )
                b_status, _, b_content = call(
                    port, tls_b, "POST", "/v1/fetch", fetch("http://127.0.0.1/")
                )
                assert (
                    b_status == 403
                    and json.loads(b_content)["code"] == "EGRESS_ADDRESS_NOT_GLOBAL"
                )
            finally:
                held.close()
            checks.append(
                "Занятая квота identity A даёт 429 только A; "
                "B проходит допуск и получает собственный сетевой отказ."
            )
            time.sleep(0.2)
            provision.revoke(
                IDENTITY, root / "grants", root / "audit", "lifecycle-operator"
            )
            assert (
                call(port, tls_a, "POST", "/v1/fetch", fetch("https://example.com/"))[0]
                == 403
            )
            applied(root, declaration())
            run(compose + ["stop", NATIVE], environment=environment)
            run(
                compose
                + ["up", "-d", "--no-build", "--pull", "never", "--wait", NATIVE],
                environment=environment,
            )
            assert (
                call(port, tls_a, "POST", "/v1/fetch", fetch("https://example.com/"))[0]
                == 200
            )
            run(compose + ["down"], environment=environment)
            run(
                compose
                + ["up", "-d", "--no-build", "--pull", "never", "--wait", NATIVE],
                environment=environment,
            )
            assert (
                call(port, tls_a, "POST", "/v1/fetch", fetch("https://example.com/"))[0]
                == 200
            )
            checks.append(
                "Отзыв без перезапуска, повторное применение, stop/up и down/up "
                "сохраняют точные права и работоспособность."
            )
            run(compose + ["down"], environment=environment)
            if not args.skip_root:
                real_own = [
                    "docker",
                    "compose",
                    "--env-file",
                    str(env_file),
                    "-p",
                    token + "-root",
                    "-f",
                    str(OWNER / "compose.yml"),
                    "--profile",
                    "*",
                ]
                orchestrated = [
                    "docker",
                    "compose",
                    "--env-file",
                    str(args.root_compose.resolve().parent / ".env"),
                    "-p",
                    token + "-root",
                    "-f",
                    str(args.root_compose.resolve()),
                    "--profile",
                    "*",
                ]
                own_model = json.loads(
                    run(
                        real_own + ["config", "--format", "json"],
                        environment=environment,
                    )
                )
                root_model = json.loads(
                    run(
                        orchestrated + ["config", "--format", "json"],
                        environment=environment,
                        directory=args.root_compose.resolve().parent,
                    )
                )
                for role, specification in own_model["services"].items():
                    assert root_model["services"][role] == specification, role
                checks.append(
                    "Все три роли и настройки gateway в общем Compose "
                    "точно равны собственному Compose."
                )
                # Only this provider; no other runtime or initializer is selected.
                run(
                    orchestrated
                    + [
                        "up",
                        "-d",
                        "--no-deps",
                        "--no-build",
                        "--pull",
                        "never",
                        "--wait",
                        NATIVE,
                    ],
                    environment=environment,
                    directory=args.root_compose.resolve().parent,
                )
                assert (
                    call(
                        port, tls_a, "POST", "/v1/fetch", fetch("https://example.com/")
                    )[0]
                    == 200
                )
                run(
                    orchestrated + ["stop", NATIVE],
                    environment=environment,
                    directory=args.root_compose.resolve().parent,
                )
                run(
                    orchestrated
                    + [
                        "up",
                        "-d",
                        "--no-deps",
                        "--no-build",
                        "--pull",
                        "never",
                        "--wait",
                        NATIVE,
                    ],
                    environment=environment,
                    directory=args.root_compose.resolve().parent,
                )
                assert call(port, no_key, "GET", "/readyz")[0] == 200
                checks.append(
                    "Gateway запущен и повторно поднят настоящим общим Compose; "
                    "остальные продукты не запускались/не останавливались."
                )
        except (
            AssertionError,
            OSError,
            RuntimeError,
            ValueError,
            subprocess.SubprocessError,
            http.client.HTTPException,
        ) as error:
            failure = type(error).__name__ + ": " + str(error)[:150]
            diagnostics = run(
                compose + ["logs", "--no-color", "--tail", "20", NATIVE],
                environment=environment,
            )
            checks.append(
                "Диагностика провайдера при отказе: "
                + diagnostics[-2500:].replace("\n", " | ")
            )
        finally:
            for identifier, row in inventory("container").items():
                if (
                    identifier not in before["container"]
                    and project(row, "container") in projects
                ):
                    run(["docker", "container", "stop", "-t", "5", identifier])
                    run(["docker", "container", "rm", identifier])
            for kind in ("network", "volume"):
                for identifier, row in inventory(kind).items():
                    if (
                        identifier not in before[kind]
                        and project(row, kind) in projects
                    ):
                        name = cast(str, row["Name"])
                        run(["docker", kind, "rm", name])
            current_images = inventory("image")
            for row in current_images.values():
                if native_image in cast(list[str], row.get("RepoTags") or []):
                    run(["docker", "image", "rm", native_image])
            for kind, original in before.items():
                current = inventory(kind)
                assert set(original).issubset(current), (
                    "preexisting " + kind + " was removed"
                )
                if kind == "container":
                    for identifier, row in original.items():
                        assert (
                            cast(dict[str, object], current[identifier]["State"])[
                                "Status"
                            ]
                            == cast(dict[str, object], row["State"])["Status"]
                        ), "preexisting container changed"
            checks.append(
                "Удалены только созданные тестом контейнеры, сети, тома и образ; "
                "исходные ресурсы и состояния контейнеров сохранены."
            )
    report = [
        "# Public Egress Gateway — проверка жизненного цикла",
        "",
        "Время UTC: " + datetime.now(UTC).isoformat(),
        "",
        "Результат: " + ("FAIL" if failure else "PASS"),
        "",
    ]
    report.extend("- " + item for item in checks)
    report.extend(
        [
            "",
            "Отдельный сценарий проверяет публикацию состояния/identity. "
            "Здесь не проверены WAN/Kubernetes, SPIRE-ротация, защищённый аудит "
            "и нагрузочное масштабирование. Рабочий архив не получал тестовых метрик.",
        ]
    )
    if args.skip_root:
        report.extend(
            [
                "",
                "Общий Compose явно не проверялся: самостоятельный extracted checkout.",
            ]
        )
    if failure:
        report.extend(["", "Ошибка: " + failure])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(report) + "\n")
    if failure:
        raise SystemExit(failure)


if __name__ == "__main__":
    main()
