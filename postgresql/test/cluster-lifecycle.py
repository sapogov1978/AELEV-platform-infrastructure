#!/usr/bin/env python3
"""Exercise real PostgreSQL/Patroni/etcd, then remove only owned new resources."""

from __future__ import annotations

import argparse
import datetime
import json
import platform
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "AELEV_INFRASTRUCTURE_POSTGRESQL_"
COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    str(ROOT / ".env"),
    "-f",
    str(ROOT / "compose.yml"),
]
IMPORT = "from aelev_postgresql.__main__ import connection, setting, secret, SECRETS\n"


def classify_command_failure(output: str) -> str:
    normalized = output.lower()
    classifications = (
        (
            "PERSISTED_SAFETY_CONFIG_MISMATCH",
            ("persisted cluster safety configuration does not match",),
        ),
        (
            "PATRONI_CONFIG_UNREACHABLE",
            ("no reachable patroni configuration endpoint",),
        ),
        (
            "PATRONI_AUTHORIZATION_FAILED",
            ("http error 401", "unauthorized"),
        ),
        (
            "TLS_VERIFICATION_FAILED",
            ("certificate verify failed", "tlsv1 alert unknown ca"),
        ),
        ("CONNECTION_REFUSED", ("connection refused",)),
        ("ASSERTION_FAILED", ("assertionerror",)),
    )
    for code, markers in classifications:
        if any(marker in normalized for marker in markers):
            return code
    return "PROCESS_FAILED"


def command(
    *args: str,
    source: str | None = None,
    timeout: int = 240,
    check: bool = True,
    operation: str = "DOCKER_COMMAND",
) -> subprocess.CompletedProcess[str]:
    """Capture commands privately; never include credentials in reports."""
    result = subprocess.run(
        args, input=source, text=True, capture_output=True, cwd=ROOT, timeout=timeout
    )
    if check and result.returncode:
        # Compose startup diagnostics contain role names and states, not secrets.
        if "up" in args:
            (ROOT / "test" / "startup-diagnostics.log").write_text(result.stderr)
        reason = classify_command_failure(result.stdout + "\n" + result.stderr)
        raise RuntimeError(
            f"{operation} failed (exit {result.returncode}; reason={reason})"
        )
    return result


def compose(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Use the canonical service delivery and its explicitly selected fencing."""
    arguments = list(args)
    wait_seconds = 0
    if "--wait" in arguments:
        arguments.remove("--wait")
        index = arguments.index("--wait-timeout")
        wait_seconds = int(arguments[index + 1])
        del arguments[index : index + 2]
    result = command(*COMPOSE, *arguments, **kwargs)
    if wait_seconds:
        model = json.loads(command(*COMPOSE, "config", "--format", "json").stdout)
        expected = {
            name for name, row in model["services"].items() if "healthcheck" in row
        }
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            rows = command(*COMPOSE, "ps", "-a", "--format", "json").stdout.splitlines()
            states = {row["Service"]: row for row in map(json.loads, rows)}
            if all(
                states.get(name, {}).get("Health") == "healthy" for name in expected
            ):
                break
            time.sleep(2)
        else:
            raise TimeoutError("All native roles did not become healthy after fencing")
    return result


def inventory(kind: str) -> set[str]:
    """Capture resource identities without dumping environment variables."""
    subcommand = {
        "container": ["ps", "-aq"],
        "volume": ["volume", "ls", "-q"],
        "network": ["network", "ls", "-q"],
        "image": ["image", "ls", "-q", "--no-trunc"],
    }[kind]
    return set(command("docker", *subcommand).stdout.split())


def execute(
    node: int,
    source: str,
    *,
    check: bool = True,
    operation: str = "POSTGRESQL_SQL_CHECK",
) -> subprocess.CompletedProcess[str]:
    """Execute test SQL over verified multi-host TLS from a real cluster member."""
    return compose(
        "exec",
        "-T",
        f"postgresql-{node}",
        "python",
        "-",
        source=IMPORT + source,
        check=check,
        timeout=30,
        operation=operation,
    )


def roles() -> dict[int, bool]:
    """Inspect each member independently; True means it is a replica."""
    result: dict[int, bool] = {}
    for node in range(1, 4):
        response = execute(
            node,
            f"import psycopg\nwith psycopg.connect(host=setting('NODE_{node}_HOST'), port=setting('NODE_{node}_PORT'), user=setting('ADMIN_USER'), password=secret('admin-password'), dbname='postgres', sslmode='verify-full', sslrootcert=str(SECRETS/'ca.crt'), sslcert=str(SECRETS/'admin.crt'), sslkey=str(SECRETS/'admin.key'), connect_timeout=2) as c:\n print(c.execute('SELECT pg_is_in_recovery()').fetchone()[0])",
            check=False,
        )
        if response.returncode == 0:
            result[node] = response.stdout.strip() == "True"
    return result


def wait_writer(excluded: int | None = None) -> int:
    """Wait for exactly one reachable writable member."""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        writers = [node for node, replica in roles().items() if not replica]
        if len(writers) == 1 and writers[0] != excluded:
            return writers[0]
        time.sleep(2)
    raise TimeoutError("A unique new writer did not become available")


def synchronous_quorum(node: int) -> dict[str, Any]:
    """Read actual SQL readiness independently of process health and DCS config."""
    response = execute(
        node,
        "import json\nwith connection() as c:\n"
        " print(json.dumps({'commit_on':c.execute('SHOW synchronous_commit').fetchone()[0]=='on',"
        "'quorum_rule':c.execute('SHOW synchronous_standby_names').fetchone()[0].startswith('ANY 1'),"
        "'sync_replicas':c.execute(\"SELECT count(*) FROM pg_stat_replication WHERE state='streaming' AND sync_state IN ('sync','quorum')\").fetchone()[0]}))",
        operation="READ_SYNCHRONOUS_QUORUM",
    )
    result: dict[str, Any] = json.loads(response.stdout)
    return result


def wait_synchronous_quorum(node: int) -> None:
    """Wait for the configured strict quorum to acquire a real streaming peer."""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = synchronous_quorum(node)
        if state.get("commit_on") is not True:
            raise AssertionError("SYNCHRONOUS_COMMIT_DISABLED")
        if state.get("quorum_rule") is True and state.get("sync_replicas", 0) >= 1:
            return
        time.sleep(2)
    raise TimeoutError("SYNCHRONOUS_QUORUM_NOT_READY")


def select_fault_target(writer: int, target: int) -> int:
    """Ask native Patroni to make a specified member the future failed primary."""
    if writer != target:
        execute(
            writer,
            "import base64,json,urllib.request\n"
            "from aelev_postgresql.configuration import tls_context\n"
            "authorization=base64.b64encode((setting('ADMIN_USER')+':'+secret('admin-password')).encode()).decode()\n"
            f"url='https://'+setting('NODE_{writer}_HOST')+':'+setting('NODE_{writer}_REST_PORT')+'/switchover'\n"
            f"request=urllib.request.Request(url,data=json.dumps({{'leader':'postgresql-{writer}','candidate':'postgresql-{target}'}}).encode(),headers={{'Authorization':'Basic '+authorization,'Content-Type':'application/json'}})\n"
            "with urllib.request.urlopen(request,context=tls_context(),timeout=15) as response:\n"
            " assert response.status in (200,202)\n",
            operation="SELECT_NATIVE_FAULT_TARGET",
        )
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = roles()
        if (
            len(state) == 3
            and state.get(target) is False
            and sum(not value for value in state.values()) == 1
        ):
            wait_synchronous_quorum(target)
            return target
        time.sleep(2)
    raise TimeoutError("REQUESTED_FAULT_TARGET_NOT_READY")


def failure_signals(output: str) -> dict[str, int]:
    """Reduce private native output to fixed diagnostic counters."""
    lowered = output.lower()
    markers = {
        "PRE_PROMOTE_FAILED": "pre_promote script failed",
        "PRE_PROMOTE_COMMAND": "pre_promote",
        "DCS_COMMUNICATION_FAILURE": "error communicating with dcs",
        "DCS_CONNECTION_FAILURE": "failed to get list of machines",
        "DCS_UNAVAILABLE": "DCS_UNAVAILABLE",
        "NO_LEADER_LEASE": "NO_LEADER_LEASE",
        "LEASE_CHANGED": "LEASE_CHANGED",
        "DCS_QUORUM_UNCONFIRMED": "DCS_QUORUM_UNCONFIRMED",
        "PROCESS_INCARNATION_CHANGED": "PROCESS_INCARNATION_CHANGED",
        "TERMINATION_UNCONFIRMED": "TERMINATION_UNCONFIRMED",
        "RETURN_ATTEMPTS_EXHAUSTED": "RETURN_ATTEMPTS_EXHAUSTED",
        "RETURN_UNCONFIRMED": "RETURN_UNCONFIRMED",
        "FENCING_UNCONFIRMED": "FENCING_UNCONFIRMED",
        "TLS_VERIFICATION_FAILED": "certificate verify failed",
        "PERMISSION_DENIED": "permission denied",
        "CONNECTION_REFUSED": "connection refused",
        "CANDIDATE_NOT_HEALTHY": "not healthy enough for leader race",
        "POSTGRESQL_STARTING": "postgresql is still starting up",
        "HTTP_CONFLICT": "http error 409",
    }
    signals = {
        code: min(lowered.count(marker.lower()), 1000)
        for code, marker in markers.items()
        if marker.lower() in lowered
    }
    # Return/request error codes are generated by this owner, never raw messages.
    for code in set(re.findall(r"reason_code=([A-Z_]{1,64})\b", output)):
        if code in markers:
            signals[code] = min(output.count("reason_code=" + code), 1000)
    return signals


def write_failure_diagnostic(
    report: Path, scenario: str, fault_target: int | None
) -> None:
    """Persist state and fixed signals before destroying the isolated cluster."""
    services = (
        *(f"postgresql-{node}" for node in range(1, 4)),
        *(f"etcd-{node}" for node in range(1, 4)),
        "postgresql-fencer",
    )
    snapshot: list[dict[str, Any]] = []
    sql_roles = roles()
    for service in services:
        identities = compose("ps", "-a", "-q", service, check=False).stdout.split()
        row: dict[str, Any] = {"service": service, "present": len(identities) == 1}
        if service.startswith("postgresql-") and service != "postgresql-fencer":
            node = int(service.rsplit("-", 1)[1])
            row["sql_role"] = (
                ("REPLICA" if sql_roles[node] else "PRIMARY")
                if node in sql_roles
                else "UNREACHABLE"
            )
        if len(identities) == 1:
            response = command(
                "docker",
                "inspect",
                "--format",
                "{{json .State}}",
                identities[0],
                check=False,
            )
            if not response.returncode:
                state = json.loads(response.stdout)
                row.update(
                    running=state.get("Running") is True,
                    restarting=state.get("Restarting") is True,
                    oom_killed=state.get("OOMKilled") is True,
                    exit_code=state.get("ExitCode"),
                    health=state.get("Health", {}).get("Status", "absent"),
                )
            logs = command(
                "docker", "logs", "--tail", "200", identities[0], check=False
            )
            row["signals"] = failure_signals(logs.stdout + "\n" + logs.stderr)
        snapshot.append(row)
    destination = report.with_suffix(".diagnostic.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scenario": scenario,
                "fault_target": fault_target,
                "services": snapshot,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def verify_role_isolation(node: int) -> None:
    """Authenticate an ordinary owner and reject access to another owned DB."""
    compose(
        "--profile",
        "provision",
        "run",
        "--rm",
        "--no-deps",
        "-e",
        PREFIX + "PROVISION_DATABASE=lifecycle_other_database",
        "-e",
        PREFIX + "PROVISION_ROLE=lifecycle_other_owner",
        "database-provisioner",
    )
    with tempfile.TemporaryDirectory(prefix="aelev-postgresql-client-") as temporary:
        directory = Path(temporary)
        key, csr, cert = (
            directory / name for name in ("client.key", "client.csr", "client.crt")
        )
        extensions = directory / "client.extensions"
        extensions.write_text(
            "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n"
        )
        command(
            "openssl",
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=lifecycle_owner",
            "-keyout",
            str(key),
            "-out",
            str(csr),
        )
        command(
            "openssl",
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(ROOT / "secrets/issuer/postgresql.crt"),
            "-CAkey",
            str(ROOT / "secrets/issuer/postgresql.key"),
            "-set_serial",
            "100001",
            "-days",
            "1",
            "-extfile",
            str(extensions),
            "-out",
            str(cert),
        )
        password = (ROOT / "secrets/admin/provision-password").read_text().strip()
        source = (
            f"certificate={cert.read_text()!r}\nprivate_key={key.read_text()!r}\npassword={password!r}\n"
            + """
import pathlib
import tempfile
import psycopg
with tempfile.TemporaryDirectory(prefix='aelev-sql-client-') as temporary:
    directory=pathlib.Path(temporary)
    cert,key=directory/'client.crt',directory/'client.key'
    cert.write_text(certificate)
    key.write_text(private_key)
    key.chmod(0o600)
    parameters={'host':','.join(setting(f'NODE_{n}_HOST') for n in range(1,4)), 'port':','.join(setting(f'NODE_{n}_PORT') for n in range(1,4)), 'user':'lifecycle_owner', 'password':password, 'dbname':'lifecycle_database', 'target_session_attrs':'read-write', 'sslmode':'verify-full', 'sslrootcert':str(SECRETS/'ca.crt'), 'sslcert':str(cert), 'sslkey':str(key), 'connect_timeout':2}
    with psycopg.connect(**parameters) as owner:
        assert owner.execute('SELECT current_user').fetchone()[0]=='lifecycle_owner'
        owner.execute('CREATE TABLE lifecycle_role_probe (id integer PRIMARY KEY)')
        owner.execute('INSERT INTO lifecycle_role_probe VALUES (1)')
        flags=owner.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
        assert not any(flags)
    parameters['dbname']='lifecycle_other_database'
    try:
        psycopg.connect(**parameters)
    except psycopg.OperationalError:
        pass
    else:
        raise AssertionError('Owner could access another owned database')
"""
        )
        execute(node, source)


def verify_physical_restore(node: int) -> None:
    """Restore a native basebackup into a loopback-only temporary PostgreSQL."""
    execute(
        node,
        """
import os
import pathlib
import shutil
import subprocess
import tempfile
import psycopg
from psycopg.conninfo import make_conninfo
with connection('lifecycle_database') as c:
    expected = c.execute("SELECT md5(string_agg(id::text || payload, ',' ORDER BY id)) FROM lifecycle_data").fetchone()[0]
    host, port = c.info.host, c.info.port
with tempfile.TemporaryDirectory(prefix='aelev-postgresql-restore-') as temporary:
    directory = pathlib.Path(temporary)
    data = directory / 'data'
    credentials = {'host':host, 'port':port, 'user':setting('ADMIN_USER'), 'dbname':'postgres', 'sslmode':'verify-full', 'sslrootcert':str(SECRETS/'ca.crt'), 'sslcert':str(SECRETS/'admin.crt'), 'sslkey':str(SECRETS/'admin.key')}
    environment = {**os.environ, 'PGPASSWORD':secret('admin-password')}
    subprocess.run(['pg_basebackup', '--dbname='+make_conninfo(**credentials), '--pgdata='+str(data), '--wal-method=stream', '--checkpoint=fast', '--manifest-checksums=SHA256'], env=environment, check=True, capture_output=True)
    subprocess.run(['pg_verifybackup', str(data)], check=True, capture_output=True)
    for name in ('standby.signal', 'recovery.signal'):
        (data/name).unlink(missing_ok=True)
    subprocess.run(['chown', '-R', 'postgres:postgres', str(directory)], check=True, capture_output=True)
    options = f"-p 16432 -c listen_addresses=127.0.0.1 -c data_directory={data} -c hba_file={data}/pg_hba.conf -c primary_conninfo='' -c archive_mode=off -c synchronous_standby_names='' -c shared_buffers=32MB -c max_connections=30"
    ctl = ['su-exec', 'postgres', 'pg_ctl', '-D', str(data), '-w']
    try:
        subprocess.run([*ctl, '-l', str(directory/'restore.log'), '-o', options, 'start'], check=True, capture_output=True)
        credentials.update(host='127.0.0.1', port=16432, dbname='lifecycle_database', password=secret('admin-password'))
        with psycopg.connect(**credentials) as restored:
            assert restored.execute('SELECT count(*) FROM lifecycle_data').fetchone()[0]==1002
            actual = restored.execute("SELECT md5(string_agg(id::text || payload, ',' ORDER BY id)) FROM lifecycle_data").fetchone()[0]
            assert actual == expected
    finally:
        subprocess.run([*ctl, '-m', 'fast', 'stop'], check=False, capture_output=True)
""",
    )


def main(report: Path, requested_fault_target: int | None = None) -> None:
    """Run failure scenarios and always inventory/clean newly owned resources."""
    started = datetime.datetime.now(datetime.UTC)
    steps: list[tuple[str, str]] = []
    before = {
        kind: inventory(kind) for kind in ("container", "volume", "network", "image")
    }
    config = json.loads(compose("--profile", "*", "config", "--format", "json").stdout)
    project = config["name"]
    started_resources = False
    failure: str | None = None
    scenario = "startup"
    fault_target: int | None = None
    pending_commit: subprocess.Popen[str] | None = None

    def phase(name: str) -> None:
        steps.append((name, "PASS"))

    def interrupted(signum: int, frame: object) -> None:
        raise KeyboardInterrupt(f"signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        occupied = command(
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ).stdout.strip()
        volumes = {value["name"] for value in config["volumes"].values()}
        networks = {value["name"] for value in config["networks"].values()}
        if (
            occupied
            or volumes & before["volume"]
            or networks
            & {
                json.loads(command("docker", "network", "inspect", item).stdout)[0][
                    "Name"
                ]
                for item in before["network"]
            }
        ):
            raise ValueError(
                "Existing cluster resources are preserved; destructive lifecycle refused"
            )
        phase("Исходное состояние зафиксировано; существующий кластер отсутствует")
        command(
            "docker", "image", "inspect", config["services"]["postgresql-1"]["image"]
        )
        started_resources = True
        compose(
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "180",
        )
        phase("Запущены и healthy все три PostgreSQL/Patroni и все три etcd")
        scenario = "initial-validation"
        state = roles()
        assert len(state) == 3 and sum(not replica for replica in state.values()) == 1
        writer = wait_writer()
        compose(
            "exec",
            "-T",
            f"postgresql-{writer}",
            "python",
            "-m",
            "aelev_postgresql",
            "verify",
            operation="VERIFY_CLUSTER_SAFETY",
        )
        wait_synchronous_quorum(writer)
        phase("Один ведущий, две реплики; строгий синхронный кворум ANY 1 и TLS")
        for _ in range(2):
            compose(
                "--profile",
                "provision",
                "run",
                "--rm",
                "--no-deps",
                "database-provisioner",
                "configure",
            )
        execute(
            writer,
            "with connection() as c:\n assert int(c.execute('SHOW max_connections').fetchone()[0])==int(setting('MAX_CONNECTIONS'))\n assert c.execute('SHOW shared_buffers').fetchone()[0]==setting('SHARED_BUFFERS')",
        )
        phase(
            "Канонические параметры .env применены через стандартный Patroni API дважды и сверены с SQL"
        )
        for _ in range(2):
            compose(
                "--profile",
                "provision",
                "run",
                "--rm",
                "--no-deps",
                "-e",
                PREFIX + "PROVISION_DATABASE=lifecycle_database",
                "-e",
                PREFIX + "PROVISION_ROLE=lifecycle_owner",
                "database-provisioner",
            )
        phase("Одноразовая подготовка базы и роли выполнена дважды идемпотентно")
        execute(
            writer,
            "with connection('lifecycle_database') as c:\n c.execute('CREATE TABLE lifecycle_data (id integer PRIMARY KEY, payload text NOT NULL)')\n c.execute(\"INSERT INTO lifecycle_data SELECT n, md5(n::text) FROM generate_series(1,1000) n\")",
        )
        phase("1000 строк подтверждены и сохранены в выделенной базе")
        verify_role_isolation(writer)
        phase(
            "Обычный владелец вошёл с собственным CN/паролем, создал данные своей базы, не получил привилегий или доступ к другой базе"
        )
        execute(
            writer,
            "import psycopg\ntry:\n connection(tls=False)\nexcept psycopg.OperationalError:\n pass\nelse:\n raise AssertionError('Plaintext connection accepted')\np={'host':setting('NODE_1_HOST'),'port':setting('NODE_1_PORT'),'user':setting('ADMIN_USER'),'password':secret('admin-password'),'dbname':'postgres','sslmode':'verify-full','sslrootcert':str(SECRETS/'ca.crt'),'connect_timeout':2}\ntry:\n psycopg.connect(**p)\nexcept psycopg.OperationalError:\n pass\nelse:\n raise AssertionError('Client without certificate accepted')\np.update(host='wrong-host.invalid',hostaddr='127.0.0.1',port=setting('NODE_'+setting('NODE_ID')+'_PORT'),sslcert=str(SECRETS/'admin.crt'),sslkey=str(SECRETS/'admin.key'))\ntry:\n psycopg.connect(**p)\nexcept psycopg.OperationalError:\n pass\nelse:\n raise AssertionError('Wrong server identity accepted')",
        )
        phase(
            "Запрещены plaintext, клиент без сертификата и неверная серверная identity"
        )
        execute(
            writer,
            "with connection() as c:\n c.autocommit=True\n c.execute('CREATE DATABASE lifecycle_foreign')",
        )
        rejected = compose(
            "--profile",
            "provision",
            "run",
            "--rm",
            "--no-deps",
            "-e",
            PREFIX + "PROVISION_DATABASE=lifecycle_foreign",
            "-e",
            PREFIX + "PROVISION_ROLE=lifecycle_owner",
            "database-provisioner",
            check=False,
        )
        assert rejected.returncode != 0
        execute(
            writer,
            "with connection() as c:\n assert c.execute(\"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='lifecycle_foreign'\").fetchone()[0]==setting('ADMIN_USER')",
        )
        phase("Provisioner отказался менять владельца существующей чужой базы")
        if requested_fault_target is not None:
            scenario, fault_target = "fault-target-selection", requested_fault_target
            writer = select_fault_target(writer, requested_fault_target)
            phase(
                f"Для сценария отказа штатным Patroni выбран ведущий {writer}; три SQL-роли и sync quorum подтверждены"
            )
        lost = writer
        scenario, fault_target = "pair-loss", lost
        compose("stop", f"postgresql-{lost}", f"etcd-{lost}")
        writer = wait_writer(excluded=lost)
        execute(
            writer,
            "with connection('lifecycle_database') as c:\n assert c.execute('SELECT count(*) FROM lifecycle_data').fetchone()[0]==1000\n c.execute(\"INSERT INTO lifecycle_data VALUES (1001,'after-failover')\")",
        )
        phase(
            f"Отключена целая пара узла {lost}; ведущим стал {writer}; подтверждённые строки сохранены, новая запись принята"
        )
        compose("start", f"etcd-{lost}", f"postgresql-{lost}")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            state = roles()
            if len(state) == 3 and state.get(lost) is True:
                break
            time.sleep(2)
        else:
            raise TimeoutError("Former primary did not rejoin as replica")
        phase("Прежний ведущий вернулся только как реплика; второго писателя нет")
        replicas = [node for node, replica in roles().items() if replica]
        scenario, fault_target = "replica-loss", None
        compose("stop", *(f"postgresql-{node}" for node in replicas))
        pending_commit = subprocess.Popen(
            [*COMPOSE, "exec", "-T", f"postgresql-{writer}", "python", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=ROOT,
        )
        blocked_source = (
            IMPORT
            + "with connection('lifecycle_database') as c:\n c.execute(\"INSERT INTO lifecycle_data VALUES (2000,'blocked-commit')\")\nprint('COMMIT_ACKNOWLEDGED')\n"
        )
        try:
            pending_commit.communicate(blocked_source, timeout=6)
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("COMMIT did not wait for a synchronous replica")
        execute(
            writer,
            "with connection() as c:\n assert c.execute(\"SELECT count(*) FROM pg_stat_activity WHERE wait_event='SyncRep'\").fetchone()[0]>=1",
        )
        phase(
            "Без обеих реплик COMMIT остаётся в SyncRep и не подтверждается; клиент не отменял ожидание"
        )
        compose("start", *(f"postgresql-{node}" for node in replicas))
        compose(
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "120",
        )
        acknowledgement, _ = pending_commit.communicate(timeout=60)
        assert (
            pending_commit.returncode == 0 and "COMMIT_ACKNOWLEDGED" in acknowledgement
        )
        pending_commit = None
        writer = wait_writer()
        execute(
            writer,
            "with connection('lifecycle_database') as c:\n c.execute('DELETE FROM lifecycle_data WHERE id=2000')\n c.execute(\"INSERT INTO lifecycle_data VALUES (1002,'replication-restored')\")",
        )
        phase(
            "После возврата реплик прежний ожидавший COMMIT подтверждён; запись вновь доступна"
        )
        stopped_etcd = [1, 2]
        scenario = "dcs-quorum-loss"
        compose("stop", *(f"etcd-{node}" for node in stopped_etcd))
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            state = roles()
            if len(state) == 3 and all(state.values()):
                break
            time.sleep(2)
        else:
            raise TimeoutError("Writer was not demoted after DCS quorum loss")
        phase(
            "Потеря большинства etcd оградила живой PostgreSQL: ни один узел не остался ведущим"
        )
        compose("start", *(f"etcd-{node}" for node in stopped_etcd))
        compose(
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "120",
        )
        writer = wait_writer()
        execute(
            writer,
            "with connection('lifecycle_database') as c:\n assert c.execute('SELECT count(*) FROM lifecycle_data').fetchone()[0]==1002",
        )
        scenario = "full-restart"
        compose("stop")
        compose("start")
        compose(
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "120",
        )
        writer = wait_writer()
        execute(
            writer,
            "with connection('lifecycle_database') as c:\n assert c.execute('SELECT count(*) FROM lifecycle_data').fetchone()[0]==1002",
        )
        phase(
            "Полный stop/start всех шести процессов сохранил 1002 подтверждённые строки"
        )
        scenario = "physical-restore"
        verify_physical_restore(writer)
        phase(
            "Нативный pg_basebackup + pg_verifybackup; изолированное восстановление сохранило 1002 строки и хеш содержимого"
        )
    except (Exception, KeyboardInterrupt) as error:
        failure = type(error).__name__ + ": " + str(error)
        steps.append((failure, "FAIL"))
    finally:
        if pending_commit is not None:
            pending_commit.kill()
            pending_commit.communicate(timeout=10)
        if started_resources:
            if failure:
                try:
                    write_failure_diagnostic(report, scenario, fault_target)
                except (OSError, ValueError, subprocess.SubprocessError):
                    steps.append(("Bounded failure diagnostic unavailable", "FAIL"))
                logs = compose("logs", "--no-color", "--tail", "200", check=False)
                diagnostic = logs.stdout + logs.stderr
                for path in (ROOT / "secrets").rglob("*-password"):
                    value = path.read_text().strip()
                    if value:
                        diagnostic = diagnostic.replace(value, "[REDACTED]")
                (ROOT / "test" / "failure-diagnostics.log").write_text(diagnostic)
            # Preflight proved none of these exact declared resources preexisted.
            result = compose("down", "--volumes", "--timeout", "15", check=False)
            if result.returncode:
                failure = "Cleanup failed"
                steps.append((failure, "FAIL"))
        after = {kind: inventory(kind) for kind in before}
        missing = {kind: len(before[kind] - after[kind]) for kind in before}
        remaining = command(
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ).stdout.strip()
        leftover_volumes = command(
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ).stdout.strip()
        leftover_networks = command(
            "docker",
            "network",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ).stdout.strip()
        clean = not (
            set(remaining.split()) - before["container"]
            or set(leftover_volumes.split()) - before["volume"]
            or set(leftover_networks.split()) - before["network"]
            or any(missing.values())
        )
        steps.append(
            (
                (
                    "Созданные контейнеры, 6 томов и сеть удалены; все исходные Docker-ресурсы сохранены"
                    if started_resources
                    else "Запуск не выполнялся; существующие ресурсы сохранены"
                ),
                "PASS" if clean else "FAIL",
            )
        )
        if not clean:
            failure = "Resource cleanup or preservation failed"
        ended = datetime.datetime.now(datetime.UTC)
        lines = [
            "# Проверка жизненного цикла PostgreSQL",
            "",
            f"Результат: {'PASS' if failure is None else 'FAIL'}. Начало: {started.isoformat()}; окончание: {ended.isoformat()}.",
            "",
            f"Использованы настоящие PostgreSQL 17.6, Patroni 4.1.5 и etcd 3.6.10, собственный `.env` и TLS этой поставки. Python проверяющего: {platform.python_version()}. Сборка образов выполнена до теста; тест не загружает и не удаляет образы.",
            "",
            "Три контейнерных узла находятся на одной Docker VM. Тест использует механизм ограждения из собственного env, не отключает его. Прогон не доказывает географическую независимость, работу аппаратного watchdog, WAN-задержки, независимое архивирование WAL или восстановление с удалённой резервной копии.",
            "",
            "| Проверка | Результат |",
            "|---|---|",
        ]
        lines.extend(
            f"| {name.replace('|','/')} | {status} |" for name, status in steps
        )
        lines += [
            "",
            "Сохранённые исходные ресурсы: "
            + ", ".join(f"{kind}: {len(before[kind])}" for kind in before)
            + ".",
            "",
            "Потеря или timeout ответа COMMIT не доказывает отсутствие записи: клиент обязан сверить операцию перед повтором. Очистка ограничена точно объявленными ресурсами, отсутствие которых проверено до запуска; глобальная очистка и перезапуск Docker не выполнялись.",
            "",
        ]
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("\n".join(lines))
    if failure:
        raise SystemExit(failure)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "test" / "cluster-lifecycle-result.md",
    )
    parser.add_argument("--fault-target", type=int, choices=(1, 2, 3))
    arguments = parser.parse_args()
    main(arguments.report, arguments.fault_target)
