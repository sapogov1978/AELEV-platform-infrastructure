#!/usr/bin/env python3
"""Test actual Docker fencing, preserving existing data and foreign runtimes."""

from __future__ import annotations

import argparse
import datetime
import json
import platform
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "test/fencing-lifecycle-result.md"
IMPORT = "from aelev_postgresql.__main__ import connection, setting, secret, SECRETS\n"
ROLES = (
    {f"postgresql-{n}" for n in range(1, 4)}
    | {f"etcd-{n}" for n in range(1, 4)}
    | {"postgresql-fencer"}
)


def command(
    *args: str, source: str | None = None, check: bool = True, cwd: Path = ROOT
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args, input=source, text=True, capture_output=True, timeout=45, cwd=cwd
    )
    if check and result.returncode:
        raise RuntimeError(f"Command {args[0]} failed: exit {result.returncode}")
    return result


def inventory() -> dict[str, tuple[bool, bool]]:
    identities = command("docker", "ps", "-aq").stdout.split()
    if not identities:
        return {}
    rows = [
        json.loads(line)
        for line in command(
            "docker",
            "inspect",
            "--format",
            '{"Id":"{{.Id}}","State":{{json .State}}}',
            *identities,
        ).stdout.splitlines()
    ]
    return {
        row["Id"]: (row["State"]["Running"], row["State"]["Paused"]) for row in rows
    }


def run(compose_file: Path) -> int:
    report = (
        REPORT
        if compose_file.parent == ROOT
        else ROOT / "test/fencing-orchestration-result.md"
    )
    prefix = [
        "docker",
        "compose",
        "--env-file",
        str(compose_file.parent / ".env"),
        "-f",
        str(compose_file),
    ]
    stages: list[str] = []
    failed: BaseException | None = None
    table = "fencing_test_" + uuid.uuid4().hex
    created_table = False
    suspended: str | None = None
    executor_stopped = False
    foreign: dict[str, tuple[bool, bool]] = {}
    own: dict[str, str] = {}

    def phase(text: str) -> None:
        stages.append(text)
        print("PASS " + text, flush=True)

    def compose(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return command(*prefix, *args, cwd=compose_file.parent, **kwargs)

    def execute(
        source: str, *, check: bool = True, sender: int = 1
    ) -> subprocess.CompletedProcess[str]:
        return compose(
            "exec",
            "-T",
            f"postgresql-{sender}",
            "python",
            "-",
            source=IMPORT + source,
            check=check,
        )

    def roles() -> dict[int, bool]:
        result = {}
        # Read each real SQL server; Patroni's heartbeat can deliberately be frozen.
        for number in range(1, 4):
            response = execute(
                f"import psycopg\nwith psycopg.connect(host=setting('NODE_{number}_HOST'), port=setting('NODE_{number}_PORT'), user=setting('ADMIN_USER'), password=secret('admin-password'), dbname='postgres', sslmode='verify-full', sslrootcert=str(SECRETS/'ca.crt'), sslcert=str(SECRETS/'admin.crt'), sslkey=str(SECRETS/'admin.key'), connect_timeout=2) as c:\n print(c.execute('SELECT pg_is_in_recovery()').fetchone()[0])",
                check=False,
                sender=number,
            )
            if response.returncode == 0:
                result[number] = response.stdout.strip() == "True"
        return result

    def healthy_writer(*, excluded: int | None = None) -> int:
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            current = roles()
            rows = [
                json.loads(line)
                for line in compose("ps", "-a", "--format", "json").stdout.splitlines()
            ]
            healthy = {row["Service"] for row in rows if row.get("Health") == "healthy"}
            writers = [number for number, replica in current.items() if not replica]
            if (
                len(current) == 3
                and len(writers) == 1
                and writers[0] != excluded
                and ROLES <= healthy
            ):
                return writers[0]
            time.sleep(2)
        raise TimeoutError("One healthy writer and all native roles were not restored")

    def audit() -> list[dict[str, Any]]:
        response = compose(
            "exec",
            "-T",
            "postgresql-fencer",
            "python",
            "-c",
            "from pathlib import Path; p=Path('/var/lib/aelev-fencing/audit.jsonl'); print(p.read_text() if p.exists() else '')",
        )
        return [json.loads(line) for line in response.stdout.splitlines() if line]

    def checksum(sender: int) -> str:
        return execute(
            f"import hashlib,json\nwith connection() as c:\n rows=c.execute('SELECT * FROM {table} ORDER BY id').fetchall()\n print(hashlib.sha256(json.dumps(rows).encode()).hexdigest())",
            sender=sender,
        ).stdout.strip()

    def signal_patroni(identity: str, name: str) -> None:
        # Patroni's PID 1 init process forks its real HA process. Suspending
        # just the init process leaves HA/DCS running and is not a valid fault.
        source = (
            "import os,signal\nfrom pathlib import Path\n"
            "pids=[int(p.name) for p in Path('/proc').iterdir() if p.name.isdigit() "
            "and p.name!='1' and (p/'comm').exists() "
            "and (p/'comm').read_text().strip()=='patroni']\n"
            "assert pids, 'Patroni HA child process not found'\n"
            f"for pid in pids: os.kill(pid, signal.SIG{name})\n"
        )
        command("docker", "exec", identity, "python", "-c", source)

    try:
        if platform.python_version_tuple()[:2] != ("3", "13"):
            raise ValueError("Operator test requires the pinned Python 3.13")
        model = json.loads(compose("config", "--format", "json").stdout)
        if (
            model["services"]["postgresql-1"]["environment"][
                "AELEV_INFRASTRUCTURE_POSTGRESQL_FENCING_MODE"
            ]
            != "docker"
        ):
            raise ValueError("This live exercise requires explicit Docker fencing")
        for name in ROLES:
            identities = compose("ps", "-a", "-q", name).stdout.split()
            if len(identities) != 1:
                raise ValueError(
                    "All canonical PostgreSQL runtime roles must already exist"
                )
            own[name] = identities[0]
        before = inventory()
        foreign = {
            key: value for key, value in before.items() if key not in set(own.values())
        }
        writer = healthy_writer()
        phase(
            "Исходные процессы зафиксированы; healthy три PostgreSQL, три etcd и исполнитель; ровно один primary"
        )
        execute(
            f"with connection() as c:\n c.execute('SET statement_timeout=15000')\n c.execute('CREATE TABLE {table} (id integer PRIMARY KEY, payload text NOT NULL)')\n c.execute(\"INSERT INTO {table} SELECT n,md5(n::text) FROM generate_series(1,1000) n\")",
            sender=writer,
        )
        created_table = True
        expected = checksum(writer)
        phase(
            "В точно названной временной таблице синхронно подтверждены 1000 строк; хеш сохранён"
        )

        old_identity = own[f"postgresql-{writer}"]
        prior_start = command(
            "docker", "inspect", "--format", "{{.State.StartedAt}}", old_identity
        ).stdout.strip()
        prior_rows = len(audit())
        suspended = old_identity
        signal_patroni(old_identity, "STOP")
        current = roles()
        if current.get(writer) is not False:
            raise AssertionError(
                "The old SQL primary was not alive while Patroni was suspended"
            )
        phase(
            "SIGSTOP остановил рабочий процесс Patroni; SQL прежнего primary ещё отвечает как писатель"
        )
        new_writer = healthy_writer(excluded=writer)
        suspended = None
        changed_start = command(
            "docker", "inspect", "--format", "{{.State.StartedAt}}", old_identity
        ).stdout.strip()
        if changed_start == prior_start:
            raise AssertionError("Old container process was not restarted")
        rows = audit()[prior_rows:]
        confirmed = [
            row
            for row in rows
            if row["phase"] == "confirmed"
            and row["candidate"] == f"postgresql-{new_writer}"
        ]
        if {row["node"] for row in confirmed} != {
            f"postgresql-{n}" for n in range(1, 4) if n != new_writer
        }:
            raise AssertionError("Both peer termination receipts were not recorded")
        for row in confirmed:
            phases = [
                entry["phase"]
                for entry in rows
                if entry["node"] == row["node"]
                and entry["request_id"] == row["request_id"]
            ]
            if (
                phases.index("terminated") > phases.index("confirmed")
                or not row["identities"]
            ):
                raise AssertionError(
                    "Termination was not recorded before acknowledgement"
                )
        if checksum(new_writer) != expected:
            raise AssertionError("Confirmed data changed during fencing")
        phase(
            "До продвижения подтверждено завершение обоих peers; прежний процесс заменён, один новый primary, хеш совпадает"
        )

        compose("stop", "postgresql-fencer")
        executor_stopped = True
        suspended = own[f"postgresql-{new_writer}"]
        signal_patroni(suspended, "STOP")
        deadline = time.monotonic() + 42
        while time.monotonic() < deadline:
            current = roles()
            if any(
                not replica
                for number, replica in current.items()
                if number != new_writer
            ):
                raise AssertionError(
                    "A peer became primary without an available fencing executor"
                )
            time.sleep(2)
        phase(
            "Без исполнителя после истечения 30-секундной аренды ни одна реплика не продвинулась; обхода нет"
        )
        signal_patroni(suspended, "CONT")
        suspended = None
        compose("start", "postgresql-fencer")
        executor_stopped = False
        final_writer = healthy_writer()
        if checksum(final_writer) != expected:
            raise AssertionError("Confirmed data changed during the blocked promotion")
        execute(
            f"with connection() as c:\n c.execute('SET statement_timeout=15000')\n c.execute(\"INSERT INTO {table} VALUES (1001,'after-recovery')\")\n assert c.execute('SELECT count(*) FROM {table}').fetchone()[0] == 1001",
            sender=final_writer,
        )
        phase(
            "После возврата связи восстановлен один primary, прежние данные сохранены, новая синхронная запись подтверждена"
        )
    except BaseException as error:
        failed = error
    finally:
        try:
            if suspended is not None:
                signal_patroni(suspended, "CONT")
            if executor_stopped:
                compose("start", "postgresql-fencer")
            if own:
                final_writer = healthy_writer()
                if created_table:
                    execute(
                        f"with connection() as c:\n c.execute('SET statement_timeout=15000')\n c.execute('DROP TABLE {table}')",
                        sender=final_writer,
                    )
                    phase(
                        "Удалена только таблица, созданная этим тестом; существующие базы, тома и удостоверения сохранены"
                    )
            after = inventory()
            if foreign and any(
                after.get(key) != value for key, value in foreign.items()
            ):
                raise AssertionError("An unrelated container's state changed")
            if own and foreign:
                phase(
                    "Исходные контейнеры других проектов не изменены; PostgreSQL оставлен healthy для работы"
                )
        except BaseException as error:
            failed = failed or error
        result = "FAIL" if failed else "PASS"
        report.write_text(
            "# Проверка контейнерного ограждения PostgreSQL\n\n"
            f"Дата UTC: {datetime.datetime.now(datetime.timezone.utc).isoformat()}. Итог: {result}. Python: {platform.python_version()}.\n\n"
            f"Точка запуска: `{compose_file.name}` из `{compose_file.parent.name}`. Механизм: `docker`, без watchdog и перезагрузки host. Использованы реальные PostgreSQL/Patroni/etcd и существующие данные; тестовый кластер не создавался.\n\n"
            + "\n".join("- PASS: " + stage for stage in stages)
            + (
                f"\n\nОшибка: `{type(failed).__name__}: {failed}`. Невыполненные этапы не объявлены проверенными.\n"
                if failed
                else "\n"
            )
            + "\nKubernetes/kubelet и аппаратный watchdog здесь не запускались. Одна Docker VM не подтверждает WAN и независимость площадок. Подробности реализации и оригинальные документы: [FENCING.md](../FENCING.md).\n"
        )
    print("RESULT " + result, flush=True)
    return int(failed is not None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", type=Path, default=ROOT / "compose.yml")
    raise SystemExit(run(parser.parse_args().compose_file.resolve()))
