"""Check canonical infrastructure and remove only resources created by this run."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def probe(action: str, identity: str, argument: str, timestamp: str) -> None:
    """Run this probe inside the actual API container, without adding a service."""
    from confluent_kafka import Consumer, Producer
    from sqlalchemy import text

    from translator_service.contracts import RESULT_TOPIC_V2, digest, parse
    from translator_service.runtime import kafka_configuration, repository

    if action == "state":
        repo = repository()
        try:
            with repo.engine.connect() as connection:
                row = (
                    connection.execute(
                        text(
                            "SELECT j.state,j.priority,j.attempt_count,"
                            "EXISTS(SELECT 1 FROM translation_outbox o "
                            "WHERE o.request_id=j.request_id AND o.state='PENDING') "
                            "AS pending_outbox FROM translation_job j "
                            "WHERE j.request_id=:id"
                        ),
                        {"id": identity},
                    )
                    .mappings()
                    .first()
                )
                print(json.dumps(dict(row) if row else {}))
        finally:
            repo.engine.dispose()
        return
    if action == "send":
        event = {
            "specversion": "1.0",
            "id": identity,
            "source": "urn:aelev:test-client",
            "type": "com.aelev.translation-requested.v2",
            "time": timestamp,
            "subject": "translation/" + identity,
            "datacontenttype": "application/json",
            "dataschema": "https://schemas.aelev.dev/events/v1#/$defs/TranslationRequestedV2",
            "correlationid": identity,
            "causationid": identity,
            "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
            "data": {
                "schema_version": "2.0.0",
                "recorded_at": timestamp,
                "provenance": {
                    "owner_context": "TEST_CLIENT",
                    "producer_id": "test-client",
                    "producer_version": "0.1.0",
                    "input_refs": [],
                },
                "request_id": identity,
                "text": "climate observatory",
                "source_digest": digest(b"climate observatory"),
                "source_language": "en",
                "target_language": "de",
            },
        }
        raw = json.dumps(event, sort_keys=True).encode()
        parse(raw, "TranslationRequestedV2")
        producer = Producer({**kafka_configuration(), "acks": "all"})
        failures: list[str] = []
        producer.produce(
            argument,
            key=identity.encode(),
            value=raw,
            on_delivery=lambda error, _: failures.append(str(error)) if error else None,
        )
        if producer.flush(30) or failures:
            raise RuntimeError("test request delivery failed")
        print(json.dumps({"request_id": identity, "topic": argument}))
        return
    consumer = Consumer(
        {
            **kafka_configuration(),
            "group.id": "test-" + uuid.uuid4().hex,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
            "isolation.level": "read_committed",
        }
    )
    consumer.subscribe([RESULT_TOPIC_V2])
    found: list[dict[str, object]] = []
    try:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            message = consumer.poll(1)
            if message is None:
                continue
            if message.error():
                raise RuntimeError(str(message.error()))
            raw = message.value()
            if raw is None:
                raise RuntimeError("empty result")
            data = parse(raw, "TranslationCompletedV2")["data"]
            if data["request_id"] != identity:
                continue
            assert data["status"] == "SUCCEEDED", data["error_code"]
            assert data["source_language"] == "en" and data["target_language"] == "de"
            assert data["translated_text"].strip()
            found.append(
                {"sha256": digest(raw), "translated_text": data["translated_text"]}
            )
            if len(found) >= int(argument):
                print(json.dumps(found, ensure_ascii=False))
                return
        raise TimeoutError("result not received in 300 seconds")
    finally:
        consumer.close()


def compose_rows(output: str) -> list[dict[str, Any]]:
    if not output.strip():
        return []
    data = (
        json.loads(output)
        if output.startswith("[")
        else [json.loads(line) for line in output.splitlines()]
    )
    return cast(list[dict[str, Any]], data)


def run(
    fail_after_start: bool = False, result_path: Path | None = None
) -> None:
    sys.dont_write_bytecode = True
    from lifecycle_resources import (
        Inventory,
        cleanup_plan,
        mounted_volumes,
        preflight,
        snapshot,
        verify_cleanup,
    )

    phases: list[dict[str, object]] = []
    report: dict[str, object] = {
        "started_at": datetime.now(UTC).isoformat(),
        "status": "FAILED",
        "phases": phases,
        "stopped": False,
        "not_tested": [
            "Semantic fidelity of translations",
            "Remote TLS/mTLS and ACL",
            "Multiple physical hosts",
            "Load test",
        ],
        "resources": (
            "Real Kafka and translator deliveries, working .env and persistent volumes"
        ),
        "mode": "failure_cleanup" if fail_after_start else "full_lifecycle",
    }
    kafka_root = ROOT.parent / "kafka"
    for owner in (ROOT, kafka_root):
        if not (owner / ".env").is_file():
            raise FileNotFoundError(f"Working .env required: {owner / '.env'}")
    compose = [
        "docker",
        "compose",
        "--env-file",
        str(ROOT / ".env"),
        "-f",
        str(ROOT / "compose.remote.yml"),
        "--profile",
        "bundled-database",
    ]
    compute = [
        "docker",
        "compose",
        "--env-file",
        str(ROOT / ".env"),
        "-f",
        str(ROOT / "compose.compute.yml"),
    ]
    kafka_compose = [
        "docker",
        "compose",
        "--env-file",
        str(kafka_root / ".env"),
        "-f",
        str(kafka_root / "compose.yml"),
    ]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AELEV_", "COMPOSE_"))
    }

    def command(
        label: str,
        arguments: list[str],
        source: str | None = None,
        *,
        record_output: bool = True,
        timeout: int | None = 900,
    ) -> str:
        print(label, flush=True)
        began = time.monotonic()
        result = subprocess.run(
            arguments,
            cwd=ROOT,
            input=source,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        phases.append(
            {
                "name": label,
                "status": "PASS" if result.returncode == 0 else "FAIL",
                "seconds": round(time.monotonic() - began, 2),
                "output": output[-8000:] if record_output else "",
            }
        )
        if result.returncode:
            raise RuntimeError(f"{label}: exit {result.returncode}")
        return result.stdout.strip()

    def check(label: str, assertion: bool) -> None:
        assert assertion, label
        phases.append({"name": label, "status": "PASS"})

    def kafka(action: str, identity: str, argument: str, timestamp: str) -> Any:
        output = command(
            "Kafka " + action,
            compose
            + [
                "exec",
                "-T",
                "translator-api",
                "python",
                "-",
                "--probe",
                action,
                identity,
                argument,
                timestamp,
            ],
            Path(__file__).read_text(),
        )
        return json.loads(output)

    def state(identity: str, wanted: str, timestamp: str) -> dict[str, Any]:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            data = cast(dict[str, Any], kafka("state", identity, "0", timestamp))
            if data.get("state") == wanted:
                return data
            time.sleep(2)
        raise TimeoutError("job state did not become " + wanted)

    def ready(expected: int) -> None:
        endpoint = command("API endpoint", compose + ["port", "translator-api", "8096"])
        try:
            with urllib.request.urlopen(
                f"http://{endpoint}/readyz", timeout=15
            ) as response:
                actual = response.status
        except urllib.error.HTTPError as error:
            actual = error.code
        check("Readiness HTTP " + str(expected), actual == expected)

    up = [
        "up",
        "-d",
        "--no-build",
        "--pull",
        "never",
        "--wait",
        "--wait-timeout",
        "600",
    ]
    kafka_started, translator_started, compute_started = False, False, False
    before: Inventory | None = None
    projects: set[str] = set()

    def inventory_command(arguments: list[str]) -> str:
        return command("Docker inventory", arguments, record_output=False)

    try:
        kafka_config = cast(
            dict[str, Any],
            json.loads(
                command(
                    "Resolve real Kafka",
                    kafka_compose + ["config", "--format", "json"],
                    record_output=False,
                )
            ),
        )
        translator_config = cast(
            dict[str, Any],
            json.loads(
                command(
                    "Resolve real translator",
                    compose + ["config", "--format", "json"],
                    record_output=False,
                )
            ),
        )
        compute_config = cast(
            dict[str, Any],
            json.loads(
                command(
                    "Resolve opaque compute station",
                    compute + ["config", "--format", "json"],
                    record_output=False,
                )
            ),
        )
        check(
            "Kafka owns exactly one broker", set(kafka_config["services"]) == {"kafka"}
        )
        check(
            "Translator does not create a second broker",
            "kafka" not in translator_config["services"],
        )
        check(
            "Compute station has only provider and worker",
            set(compute_config["services"])
            == {"translation-provider", "translator-worker"},
        )
        provider = compute_config["services"]["translation-provider"]
        worker = compute_config["services"]["translator-worker"]
        check(
            "Provider and worker use distinct admitted images",
            provider["image"] != worker["image"]
            and "@sha256:" in provider["image"]
            and "@sha256:" in worker["image"],
        )
        check(
            "Provider is network-isolated behind the opaque port",
            provider.get("network_mode") == "none",
        )
        network = translator_config["networks"]["kafka"]
        check(
            "Translator uses the real Kafka network",
            network["external"]
            and network["name"] == kafka_config["networks"]["kafka"]["name"],
        )
        api_environment = translator_config["services"]["translator-api"]["environment"]
        database = translator_config["services"]["translator-database"]["environment"]
        check(
            "Requests can only reach this run's bundled PostgreSQL and Kafka",
            api_environment["AELEV_TRANSLATOR_DATABASE_HOST"] == "translator-database"
            and str(api_environment["AELEV_TRANSLATOR_DATABASE_PORT"]) == "5432"
            and api_environment["AELEV_TRANSLATOR_DATABASE_NAME"]
            == database["POSTGRES_DB"]
            and api_environment["AELEV_TRANSLATOR_DATABASE_USER"]
            == database["POSTGRES_USER"]
            and api_environment["AELEV_KAFKA_BOOTSTRAP_SERVERS"] == "kafka:9092",
        )
        report["kafka_project"] = kafka_config["name"]
        report["translator_project"] = translator_config["name"]
        report["compute_project"] = compute_config["name"]
        projects = {
            str(kafka_config["name"]),
            str(translator_config["name"]),
            str(compute_config["name"]),
        }
        before = snapshot(inventory_command)
        report["inventory_before"] = before
        volumes = {
            str(volume["name"])
            for config in (kafka_config, translator_config, compute_config)
            for volume in config["volumes"].values()
        }
        images = {
            str(service["image"])
            for config in (kafka_config, translator_config, compute_config)
            for service in config["services"].values()
        }
        preflight(before, projects, volumes, images)
        report["images"] = {
            reference: identity
            for identity, image in before["images"].items()
            for reference in image["references"].split(",")
            if reference in images
        }
        check("Clean storage and prepared real images", True)
        # A shared network may already exist, but not an active foreign endpoint.
        network_names = {
            str(item["name"])
            for config in (kafka_config, translator_config, compute_config)
            for item in config["networks"].values()
            if item.get("name")
        }
        for row in before["networks"].values():
            if row["name"] in network_names:
                endpoints = json.loads(
                    command(
                        "Check pre-existing network users",
                        [
                            "docker",
                            "network",
                            "inspect",
                            "--format",
                            "{{json .Containers}}",
                            row["name"],
                        ],
                        record_output=False,
                    )
                )
                check("No foreign network endpoint will be disrupted", not endpoints)

        kafka_started = True
        command("Start real infrastructure Kafka", kafka_compose + up)
        translator_started = True
        command(
            "Start Translator contracts, storage and dispatch roles", compose + up
        )
        compute_started = True
        command("Start opaque provider and separate worker", compute + up)
        rows = compose_rows(
            command(
                "Real translator states", compose + ["ps", "--all", "--format", "json"]
            )
        )
        check("Exactly seven Translator roles, no broker or provider", len(rows) == 7)
        for row in rows:
            role = row["Service"]
            if role in {"translator-migrator", "translator-topic-provisioner"}:
                check(
                    role + " completed",
                    row["State"] == "exited" and row["ExitCode"] == 0,
                )
            else:
                check(role + " running", row["State"] == "running")
                if role in {"translator-database", "translator-api"}:
                    check(role + " healthy", row["Health"] == "healthy")
        compute_rows = compose_rows(
            command(
                "Opaque compute states", compute + ["ps", "--all", "--format", "json"]
            )
        )
        check("Exactly provider and worker are running", len(compute_rows) == 2)
        for row in compute_rows:
            check(row["Service"] + " running", row["State"] == "running")
            check(row["Service"] + " healthy", row["Health"] == "healthy")
        broker = compose_rows(
            command(
                "Real Kafka state", kafka_compose + ["ps", "--all", "--format", "json"]
            )
        )
        check(
            "Real Kafka healthy", len(broker) == 1 and broker[0]["Health"] == "healthy"
        )
        ready(200)
        if fail_after_start:
            raise RuntimeError("Injected failure after all real roles became healthy")
        stamp = datetime.now(UTC).isoformat()
        priority = str(uuid.uuid4())
        kafka("send", priority, "translation.requests.priority.v2", stamp)
        original = kafka("receive", priority, "1", stamp)[0]["sha256"]
        kafka("send", priority, "translation.requests.priority.v2", stamp)
        replay = kafka("receive", priority, "2", stamp)
        check(
            "Duplicate republishes identical saved bytes",
            all(item["sha256"] == original for item in replay),
        )
        data = state(priority, "SUCCEEDED", stamp)
        check(
            "Priority computed once",
            data["attempt_count"] == 1 and data["priority"] == 0,
        )
        command(
            "Stop real worker",
            compute + ["stop", "--timeout", "20", "translator-worker"],
        )
        normal = str(uuid.uuid4())
        kafka("send", normal, "translation.requests.v2", stamp)
        data = state(normal, "PENDING", stamp)
        check("Normal queue persisted while worker stopped", data["priority"] == 100)
        command(
            "Stop compute preserving provider state",
            compute + ["stop", "--timeout", "20"],
        )
        command(
            "Stop real translator preserving data",
            compose + ["stop", "--timeout", "20"],
        )
        command(
            "Stop real Kafka preserving data",
            kafka_compose + ["stop", "--timeout", "20"],
        )
        command("Restart real Kafka", kafka_compose + up)
        command(
            "Restart Translator contracts, storage and dispatch roles", compose + up
        )
        command("Restart opaque provider and separate worker", compute + up)
        command(
            "Re-run real topic provisioning",
            compose + ["run", "--rm", "-T", "translator-topic-provisioner"],
        )
        ready(200)
        kafka("receive", normal, "1", stamp)
        persisted = kafka("receive", priority, "2", stamp)
        check(
            "Kafka results survive full restart",
            all(item["sha256"] == original for item in persisted),
        )
        command(
            "Stop worker before Kafka outage",
            compute + ["stop", "--timeout", "20", "translator-worker"],
        )
        outage = str(uuid.uuid4())
        kafka("send", outage, "translation.requests.v2", stamp)
        state(outage, "PENDING", stamp)
        command(
            "Stop real Kafka for outage test",
            kafka_compose + ["stop", "--timeout", "20"],
        )
        ready(503)
        command(
            "Restore worker with Kafka unavailable",
            compute + ["start", "translator-worker"],
        )
        data = state(outage, "SUCCEEDED", stamp)
        check(
            "Completed result retained in outbox during outage", data["pending_outbox"]
        )
        command("Restore real Kafka", kafka_compose + up)
        command("Verify Translator roles after broker recovery", compose + up)
        command("Verify compute roles after broker recovery", compute + up)
        ready(200)
        kafka("receive", outage, "1", stamp)
        report["requests"] = {
            "priority": priority,
            "normal": normal,
            "broker_outage": outage,
        }
        report["status"] = "PASS"
    except (Exception, KeyboardInterrupt) as error:
        report["failure"] = str(error)
        if translator_started:
            with suppress(Exception):
                command(
                    "Initializer diagnostics",
                    compose
                    + [
                        "logs",
                        "--no-color",
                        "--tail",
                        "40",
                        "translator-migrator",
                        "translator-topic-provisioner",
                    ],
                )
    finally:
        cleanup_errors: list[str] = []

        def cleanup_step(label: str, arguments: list[str]) -> None:
            try:
                command(label, arguments)
            except Exception as error:
                cleanup_errors.append(str(error))

        if kafka_started or translator_started or compute_started:
            if compute_started:
                cleanup_step(
                    "Stop compute before cleanup",
                    compute + ["stop", "--timeout", "20"],
                )
            if translator_started:
                cleanup_step(
                    "Stop translator before cleanup",
                    compose + ["stop", "--timeout", "20"],
                )
            if kafka_started:
                cleanup_step(
                    "Stop Kafka before cleanup",
                    kafka_compose + ["stop", "--timeout", "20"],
                )
            report["stopped"] = not cleanup_errors
            try:
                assert before is not None
                current = snapshot(inventory_command)
                containers = cleanup_plan(before, current, projects, set())[
                    "containers"
                ]
                mounted = mounted_volumes(inventory_command, containers)
                plan = cleanup_plan(before, current, projects, mounted)
                report["created_resources"] = plan
                for kind in ("containers", "volumes", "networks"):
                    for identity in plan[kind]:
                        noun = {
                            "containers": "container",
                            "volumes": "volume",
                            "networks": "network",
                        }[kind]
                        cleanup_step(
                            "Remove test-created " + noun,
                            ["docker", noun, "rm", identity],
                        )
                after = snapshot(inventory_command)
                report["inventory_after"] = after
                verify_cleanup(before, after, projects)
                for identity in mounted - before["volumes"].keys():
                    check(
                        "Anonymous/named test volume removed",
                        identity not in after["volumes"],
                    )
                check("All pre-existing resources unchanged", True)
                report["cleanup"] = "PASS" if not cleanup_errors else "FAILED"
            except Exception as error:
                cleanup_errors.append(str(error))
        else:
            report["cleanup"] = "NOT_REQUIRED"
        if cleanup_errors:
            report["status"] = "FAILED"
            report["cleanup"] = "FAILED"
            report["cleanup_failures"] = cleanup_errors
        report["finished_at"] = datetime.now(UTC).isoformat()
        if result_path is None:
            result_name = (
                "lifecycle-failure-cleanup-result"
                if fail_after_start
                else "lifecycle-result"
            )
            result_path = ROOT / f"test/{result_name}.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        write_readable_report(report, result_path)
        print(f"{report['status']}: {result_path}", flush=True)
    if report["status"] != "PASS":
        raise SystemExit(1)


def write_readable_report(report: dict[str, object], result_path: Path) -> None:
    started = datetime.fromisoformat(str(report["started_at"]))
    finished = datetime.fromisoformat(str(report["finished_at"]))
    local = started.astimezone(ZoneInfo("Europe/Belgrade"))
    created = cast(dict[str, list[str]], report.get("created_resources", {}))
    counts = {
        kind: len(created.get(kind, []))
        for kind in ("containers", "volumes", "networks")
    }
    phases = cast(list[dict[str, object]], report["phases"])
    suffix = " --fail-after-start" if report["mode"] == "failure_cleanup" else ""
    lines = [
        (
            "# Результат проверки очистки translator-service после ошибки"
            if report["mode"] == "failure_cleanup"
            else "# Результат проверки жизненного цикла translator-service"
        ),
        "",
        f"- Время: `{local:%Y-%m-%d %H:%M:%S %Z}` (`Europe/Belgrade`).",
        f"- Команда: `python3 test/lifecycle.py{suffix}`.",
        f"- Результат: `{report['status']}`; "
        f"очистка: `{report.get('cleanup', 'FAILED')}`.",
        f"- Длительность: `{(finished - started).total_seconds():.2f} с`.",
        f"- Успешные этапы: `{sum(phase['status'] == 'PASS' for phase in phases)}`.",
        "",
        "Использованы канонические поставки Kafka и Translator с рабочими `.env`, "
        "собственной PostgreSQL и отдельной вычислительной станцией. Translator "
        "передаёт только текст и языки непрозрачному provider port и не читает "
        "модель, её версию или способ исполнения. Отдельные тестовые broker и "
        "подменные provider не создаются. Образы подготовлены "
        "до теста; "
        "сборка, загрузка образов, файловые секреты и кеш не входят в его изменения.",
        "",
    ]
    if report["status"] == "PASS":
        lines += [
            "Проверены оба инициализатора, готовность всех ролей, обе очереди, "
            "повтор точного результата без вычисления, сохранность после полного "
            "перезапуска и доставка outbox после отказа Kafka.",
            "",
        ]
    elif report.get("failure"):
        lines += [
            f"Причина отказа: `{report['failure']}`. "
            "Полный жизненный цикл не засчитан успешным.",
            "",
        ]
    if report.get("cleanup") == "PASS":
        lines += [
            "Удалены только созданные этим прогоном ресурсы: "
            f"{counts['containers']} контейнеров, {counts['volumes']} volumes "
            f"(включая анонимные Kafka) и {counts['networks']} сети. "
            "Вместе с тестовыми volumes удалены созданные задания, результаты "
            "и Kafka topics. Исходно существовавшие ресурсы и их состояния "
            "сверены до и после теста и не изменились.",
            "",
        ]
    elif report.get("cleanup") == "FAILED":
        lines += [
            "Очистка не завершена; оставшиеся ресурсы и ошибки перечислены "
            "в машинном отчёте. Ошибка очистки делает весь прогон неуспешным.",
            "",
        ]
    else:
        lines += ["Запуск остановлен до создания ресурсов; очистка не требовалась.", ""]
    lines += [
        "Качество перевода, удалённые TLS/mTLS и ACL, географически разнесённые "
        "узлы, нагрузочные пределы и внешнее резервное восстановление этим тестом "
        "не подтверждаются. Проверка транспорта не заменяет оценку смысловой "
        "точности результата выбранного внешнего provider.",
        "",
        "Подробные этапы, исходная и конечная опись ресурсов — "
        f"[машинный отчёт]({result_path.name}).",
        "",
    ]
    result_path.with_suffix(".md").write_text("\n".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", nargs=4)
    parser.add_argument(
        "--fail-after-start",
        action="store_true",
        help="inject a failure after startup to verify cleanup on the error path",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "test/lifecycle-result.json",
    )
    arguments = parser.parse_args()
    if arguments.probe:
        probe(*arguments.probe)
    else:
        run(arguments.fail_after_start, arguments.report)
