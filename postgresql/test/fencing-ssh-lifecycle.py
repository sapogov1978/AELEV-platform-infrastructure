"""Native public fencing/launcher over SSH with real Patroni and three etcd.

Every long-running role uses its own Compose project/network; all connections
use published host endpoints. The runtime uses one Docker Engine; physical distribution remains unproven.
"""

from pathlib import Path
import copy
import argparse
import platform
import re
from typing import Any
import atexit
import hashlib
import http.client
import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import textwrap
import time
import uuid

NODES = frozenset(f"postgresql-{n}" for n in range(1, 4))
CODES = frozenset(
    (
        "FENCING_UNCONFIRMED",
        "DCS_UNAVAILABLE",
        "DCS_RESPONSE_INVALID",
        "DCS_QUORUM_UNCONFIRMED",
        "NO_LEADER_LEASE",
        "INVALID_LEADER_LEASE",
        "LEASE_CHANGED",
        "TARGET_IDENTITY_CHANGED",
        "PROCESS_INCARNATION_CHANGED",
        "TERMINATION_UNCONFIRMED",
        "RETURN_ATTEMPTS_EXHAUSTED",
        "RETURN_TARGET_IS_CLAIMANT",
        "RETURN_UNCONFIRMED",
        "SSH_OPERATION_UNCONFIRMED",
        "SSH_RETURN_UNCONFIRMED",
        "SSH_RECEIPT_INVALID",
        "SSH_NODE_NOT_CONFIGURED",
        "NODE_OPERATION_UNCONFIRMED",
        "NODE_STATE_NOT_ADMITTED",
        "NODE_DAEMON_CHANGED",
    )
)


def safe_intent(value: dict[str, Any]) -> dict[str, Any] | None:
    """Retain bounded phases/budgets without identities or arbitrary payloads."""
    if (
        not isinstance(value.get("node"), str)
        or value["node"] not in NODES
        or "phase" not in value
    ):
        return None
    phase = value["phase"]
    attempts = value.get("start_attempts")
    code = value.get("last_return_code")
    claim = value.get("claim")
    candidate = claim.get("candidate") if isinstance(claim, dict) else None
    return {
        "node": value["node"],
        "phase": (
            phase
            if isinstance(phase, str)
            and phase
            in {
                "STOP_REQUESTED",
                "STOP_CONFIRMED",
                "START_REQUESTED",
                "COMPLETED",
                "CANCELLED",
            }
            else "UNKNOWN"
        ),
        "start_attempts": (
            attempts if type(attempts) is int and 0 <= attempts <= 3 else None
        ),
        "last_return_code": (
            code
            if isinstance(code, str) and code in CODES
            else "UNRECOGNIZED" if code is not None else None
        ),
        "candidate": (
            candidate if isinstance(candidate, str) and candidate in NODES else None
        ),
        "restart_required": value.get("restart_required") is True,
    }


def safe_audit_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Keep only known event names and reason codes from technical journals."""
    counts: dict[str, int] = {}
    phases = {
        "intent",
        "terminated",
        "confirmed",
        "failed",
        "return_intent",
        "return_confirmed",
        "return_requested",
        "returned",
        "return_reconciled",
        "return_blocked",
        "return_cancelled",
        "remote_confirmed",
    }
    for row in rows:
        phase = row.get("phase")
        phase = phase if isinstance(phase, str) and phase in phases else "UNKNOWN"
        code = row.get("reason_code")
        code = (
            code
            if isinstance(code, str) and code in CODES
            else "UNRECOGNIZED" if code is not None else "NONE"
        )
        key = phase + ":" + code
        counts[key] = counts.get(key, 0) + 1
    return counts


def safe_runtime_state(value: dict[str, Any], logs: str) -> dict[str, Any]:
    """Publish bounded process health and known signals, never raw logs/config."""
    state = value.get("State", {})
    health = state.get("Health", {}).get("Status")
    tokens = re.findall(r"\b[A-Z][A-Z0-9_]*\b", logs)
    return {
        "running": state.get("Running") is True,
        "paused": state.get("Paused") is True,
        "oom_killed": state.get("OOMKilled") is True,
        "exit_code": (
            state.get("ExitCode") if type(state.get("ExitCode")) is int else None
        ),
        "restart_count": (
            value.get("RestartCount")
            if type(value.get("RestartCount")) is int
            else None
        ),
        "health": (
            health
            if isinstance(health, str)
            and health in {"healthy", "unhealthy", "starting"}
            else "UNKNOWN"
        ),
        "reason_code_counts": {
            code: tokens.count(code) for code in sorted(CODES) if code in tokens
        },
        "transport_signal_counts": {
            name: logs.count(marker)
            for name, marker in {
                "peer_connection_rejected": "rejected connection on peer endpoint",
                "peer_san_mismatch": "does not match any DNSNames",
                "tls_bad_certificate": "tls: bad certificate",
                "host_key_rejected": "Host key verification failed",
                "connection_refused": "Connection refused",
            }.items()
            if marker in logs
        },
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    os.umask(0o077)
    SOURCE = Path(__file__).resolve().parents[2]
    OUT = args.output.resolve()
    if OUT.is_relative_to(SOURCE):
        raise ValueError("EVIDENCE_MUST_BE_OUTSIDE_SOURCE")
    OUT.mkdir(mode=0o700)
    (OUT / "operator-script.py").write_bytes(Path(__file__).read_bytes())
    OWNER = OUT / "owner"
    shutil.copytree(
        SOURCE / "postgresql",
        OWNER,
        ignore=shutil.ignore_patterns(
            ".env",
            "secrets",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
        ),
    )
    ENV = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AELEV_", "COMPOSE_"))
        and key != "DOCKER_DEFAULT_PLATFORM"
    }
    PREFIX = "AELEV_INFRASTRUCTURE_POSTGRESQL_"
    SCOPE = "ssh-ha-" + uuid.uuid4().hex[:12]
    FENCER, SQL = args.fencing_image, args.sql_image
    observer_source = Path(__file__).with_name("ha_observation.py").read_text()
    observer_name = SCOPE + "-sql-observer"
    observer_volume = ""
    for reference in (FENCER, SQL, args.ssh_host_base_image):
        if not re.fullmatch(
            r"(?:[a-z0-9][a-z0-9._:/-]*@)?sha256:[a-f0-9]{64}", reference
        ):
            raise ValueError("IMMUTABLE_NATIVE_IMAGES_REQUIRED")
    recipes: dict[str, Path] = {}
    ids: dict[str, str] = {}
    volumes: list[str] = []
    networks: list[str] = []
    held: list[socket.socket] = []
    history: list[dict[str, Any]] = []
    proof: dict[str, Any] = {
        "status": "RUNNING",
        "physical_nodes": 1,
        "platform": "not_yet_verified",
        "native_dcs_simulated": False,
        "postgresql": "17.6",
        "patroni": "4.1.5",
        "etcd": "3.6.10",
        "source_worktree_sha256": {
            name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest()
            for name in (
                "postgresql/prepare-tls.py",
                "postgresql/fencing-node.py",
                "postgresql/compose.runtime.yml",
                "postgresql/compose.fencing-ssh.yml",
                "postgresql/test/fencing-ssh-lifecycle.py",
                "postgresql/test/ha_observation.py",
            )
        },
        "fencing_image": FENCER,
        "sql_image": SQL,
        "diagnostic_role_export_adapter": True,
        "physical_distribution_verified": False,
        "complete_public_installer_proven": False,
        "working_runtime_switched": False,
        "queue_resume_issued": False,
        "inference_requested": False,
    }

    def save() -> None:
        p = OUT / "progress.json"
        q = OUT / "progress.tmp"
        q.write_text(
            json.dumps(
                {
                    **proof,
                    "stages": history,
                    "owned_ids": ids,
                    "owned_volumes": volumes,
                },
                indent=2,
            )
            + "\n"
        )
        q.replace(p)

    def phase(name: str) -> None:
        history.append({"stage": name, "at": time.time()})
        save()
        print("PASS " + name, flush=True)

    def process(
        arguments: list[str],
        *,
        body: str | None = None,
        check: bool = True,
        timeout: int = 90,
        cwd: Path = OUT,
    ) -> subprocess.CompletedProcess[str]:
        r = subprocess.run(
            arguments,
            input=body,
            text=True,
            capture_output=True,
            env=ENV,
            cwd=cwd,
            timeout=timeout,
        )
        with (OUT / "native.private.log").open("a") as f:
            f.write(r.stdout + r.stderr)
        if check and r.returncode:
            raise RuntimeError(
                "NATIVE_"
                + Path(arguments[0]).name.upper()
                + "_FAILED_"
                + str(r.returncode)
            )
        return r

    def docker(*args: str, **kw: Any) -> str:
        return process(["docker", *args], **kw).stdout.strip()

    def inventory() -> dict[str, Any]:
        names = docker("ps", "-aq").split()
        if not names:
            return {}
        result = process(
            [
                "docker",
                "inspect",
                "--format",
                '{"Id":"{{.Id}}","Running":{{.State.Running}},"Paused":{{.State.Paused}},"RestartCount":{{.RestartCount}},"AutoRemove":{{.HostConfig.AutoRemove}}}',
                *names,
            ],
            check=False,
        )
        if result.returncode not in (0, 1) or not result.stdout.strip():
            raise RuntimeError("BASELINE_INVENTORY_UNAVAILABLE")
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        return {x.pop("Id"): x for x in rows if not x.pop("AutoRemove")}

    def port() -> int:
        s = socket.socket()
        s.bind((PUBLISHED_BIND, 0))
        held.append(s)
        return int(s.getsockname()[1])

    def volume(name: str, *, node: str | None = None, kind: str | None = None) -> str:
        assert name not in baseline_volumes
        label = ["--label", "io.aelev.evidence.scope=" + SCOPE]
        if node:
            label += [
                "--label",
                "io.aelev.postgresql.cluster=" + SCOPE,
                "--label",
                "io.aelev.postgresql.node=" + node,
            ]
        if kind:
            label += ["--label", "io.aelev.postgresql.fencing-resource=" + kind]
        docker("volume", "create", *label, name)
        assert (
            json.loads(docker("volume", "inspect", name))[0]["Labels"][
                "io.aelev.evidence.scope"
            ]
            == SCOPE
        )
        volumes.append(name)
        save()
        return name

    def materialize(folder: Path, target: str) -> None:
        program = """from pathlib import Path
    import os,shutil
    os.umask(0o077)
    destination=Path('/prepared')
    assert not any(destination.iterdir())
    for path in Path('/inputs').iterdir():
        assert path.is_file() and not path.is_symlink()
        shutil.copyfile(path,destination/path.name)
        (destination/path.name).chmod(0o600)
    destination.chmod(0o700)
    """
        program = textwrap.dedent("    " + program)
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--label",
            "io.aelev.evidence.scope=" + SCOPE,
            "--mount",
            "type=bind,source=" + str(folder) + ",target=/inputs,readonly",
            "--mount",
            "type=volume,source=" + target + ",target=/prepared",
            "--entrypoint",
            "python",
            FENCER,
            "-c",
            program,
        )

    def cert(
        folder: Path, name: str, cn: str, domain: str, *, server: bool = False
    ) -> None:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        extension = folder / (name + ".extensions")
        extension.write_text(
            "basicConstraints=critical,CA:FALSE\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage="
            + (
                "serverAuth,clientAuth\nsubjectAltName="
                + ("IP:" if re.fullmatch(r"[0-9.]+", NODE_ADDRESS) else "DNS:")
                + NODE_ADDRESS
                + ",DNS:localhost,IP:127.0.0.1\n"
                if server
                else "clientAuth\n"
            )
        )
        issuer = OWNER / "secrets/issuer"
        process(
            [
                "openssl",
                "req",
                "-new",
                "-newkey",
                "rsa:3072",
                "-nodes",
                "-subj",
                "/CN=" + cn,
                "-keyout",
                str(folder / (name + ".key")),
                "-out",
                str(folder / (name + ".csr")),
            ]
        )
        process(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(folder / (name + ".csr")),
                "-CA",
                str(issuer / (domain + ".crt")),
                "-CAkey",
                str(issuer / (domain + ".key")),
                "-set_serial",
                str(secrets.randbits(128)),
                "-days",
                "2",
                "-sha256",
                "-extfile",
                str(extension),
                "-out",
                str(folder / (name + ".crt")),
            ]
        )
        extension.unlink()
        (folder / (name + ".csr")).unlink()

    def export(
        role: str, service: dict[str, Any], model: dict[str, Any], index: int
    ) -> None:
        project = SCOPE + "-" + role
        network = project + "-network"
        if docker(
            "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project
        ):
            raise ValueError("TEST_PROJECT_ALREADY_EXISTS")
        definition = copy.deepcopy(service)
        definition.pop("build", None)
        definition.pop("depends_on", None)
        definition["networks"] = {"runtime": {}}
        definition.setdefault("labels", {})["io.aelev.evidence.scope"] = SCOPE
        names = {
            x["source"] for x in definition.get("volumes", []) if x["type"] == "volume"
        }
        declared = {k: copy.deepcopy(model.get("volumes", {})[k]) for k in names}
        for key, v in declared.items():
            if v.get("external"):
                assert v["name"] in volumes
            else:
                assert v["name"].startswith(SCOPE + "-")
                if v["name"] in baseline_volumes:
                    raise ValueError("TEST_VOLUME_ALREADY_EXISTS")
                v.setdefault("labels", {})["io.aelev.evidence.scope"] = SCOPE
                if v["name"] not in volumes:
                    volumes.append(v["name"])
        plan = {
            "name": project,
            "services": {role: definition},
            "volumes": declared,
            "networks": {
                "runtime": {
                    "name": network,
                    "ipam": {"config": [{"subnet": subnets[index]}]},
                    "labels": {"io.aelev.evidence.scope": SCOPE},
                }
            },
        }
        location = OUT / (role + ".compose.json")
        location.write_text(json.dumps(plan, indent=2) + "\n")
        recipes[role] = location
        networks.append(network)
        save()

    def compose(role: str, *arguments: str, **kw: Any) -> str:
        return docker(
            "compose",
            "--project-name",
            SCOPE + "-" + role,
            "-f",
            str(recipes[role]),
            *arguments,
            **kw,
        )

    def start(role: str) -> None:
        compose(role, "up", "-d", "--no-build", "--pull", "never")
        found = compose(role, "ps", "-aq", role).split()
        assert len(found) == 1
        ids[role] = found[0]
        save()

    def healthy(roles: list[str], seconds: int = 180) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rows = json.loads(docker("inspect", *(ids[role] for role in roles)))
            if all(
                x["State"]["Running"]
                and x["State"].get("Health", {}).get("Status", "healthy") == "healthy"
                for x in rows
            ):
                return
            time.sleep(2)
        raise TimeoutError("OWNED_NATIVE_READINESS_UNCONFIRMED")

    def sql(
        number: int, code: str, *, check: bool = True, timeout: int = 30
    ) -> subprocess.CompletedProcess[str]:
        prelude = "from aelev_postgresql.__main__ import connection,setting,secret,SECRETS\nimport json,hashlib,psycopg\n"
        return process(
            ["docker", "exec", "-i", ids["postgresql-" + str(number)], "python", "-"],
            body=prelude + code,
            check=check,
            timeout=timeout,
        )

    def roles() -> dict[int, bool]:
        r = process(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--name",
                observer_name,
                "--network",
                "bridge",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "128m",
                "--pids-limit",
                "64",
                "--label",
                "io.aelev.evidence.scope=" + SCOPE,
                "--env-file",
                str(OWNER / ".env"),
                "--mount",
                "type=volume,source="
                + observer_volume
                + ",target=/run/aelev/secrets,readonly,volume-nocopy",
                "--entrypoint",
                "python",
                SQL,
                "-",
                "observe",
            ],
            body=observer_source,
            timeout=30,
            check=False,
        )
        if r.returncode:
            code = (
                "MULTIPLE_SQL_WRITERS_ON_LIVE_CONNECTIONS"
                if "MULTIPLE_SQL_WRITERS_ON_LIVE_CONNECTIONS" in r.stderr
                else "SQL_OBSERVER_UNAVAILABLE"
            )
            proof["sql_observer_failure"] = code
            raise AssertionError(code)
        found = {
            int(node): recovery
            for node, recovery in json.loads(r.stdout)["roles"].items()
        }
        proof["last_sql_roles"] = {
            str(n): (
                "REPLICA"
                if found.get(n) is True
                else "PRIMARY" if found.get(n) is False else "UNKNOWN"
            )
            for n in range(1, 4)
        }
        return found

    def writer(*, exclude: int | None = None, seconds: int = 180) -> int:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            found = roles()
            primary = [n for n, recovery in found.items() if recovery is False]
            if len(found) == 3 and len(primary) == 1 and primary[0] != exclude:
                try:
                    healthy(["postgresql-" + str(n) for n in range(1, 4)], seconds=4)
                    return primary[0]
                except TimeoutError:
                    pass
            time.sleep(1)
        raise TimeoutError("SINGLE_HEALTHY_WRITER_NOT_RESTORED")

    def node_intent(n: int) -> Any:
        return json.loads(
            docker(
                "exec",
                ids["ssh-node-" + str(n)],
                "docker",
                "--host",
                "unix:///var/run/docker.sock",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--label",
                "io.aelev.evidence.scope=" + SCOPE,
                "--mount",
                "type=volume,source="
                + ledgers[n]
                + ",target=/var/lib/aelev-fencing,readonly,volume-nocopy",
                "--entrypoint",
                "python",
                FENCER,
                "-c",
                'import json;from aelev_postgresql.fencing_intent import IntentStore;print(json.dumps(IntentStore("postgresql-'
                + str(n)
                + '").load()))',
            )
        )

    engine = json.loads(docker("info", "--format", "{{json .}}"))
    native = {
        "aarch64": "arm64",
        "x86_64": "amd64",
        "arm64": "arm64",
        "amd64": "amd64",
    }.get(engine["Architecture"])
    if native is None or engine["OSType"] != "linux":
        raise ValueError("NATIVE_LINUX_ENGINE_REQUIRED")
    PLATFORM = "linux/" + native
    proof["platform"] = PLATFORM
    desktop = "docker desktop" in engine.get("OperatingSystem", "").lower()
    if desktop:
        NODE_ADDRESS, PUBLISHED_BIND, OPERATOR_HOST = (
            "host.docker.internal",
            "127.0.0.1",
            "127.0.0.1",
        )
    else:
        bridge = json.loads(docker("network", "inspect", "bridge"))[0]
        NODE_ADDRESS = next(
            row["Gateway"]
            for row in (bridge.get("IPAM", {}).get("Config") or [])
            if row.get("Gateway") and ipaddress.ip_address(row["Gateway"]).version == 4
        )
        PUBLISHED_BIND = OPERATOR_HOST = NODE_ADDRESS
    proof["published_node_address"] = NODE_ADDRESS
    proof["published_bind"] = PUBLISHED_BIND
    if platform.python_version_tuple()[:2] != ("3", "13"):
        raise ValueError("PYTHON_313_OPERATOR_REQUIRED")
    for reference in (FENCER, SQL, args.ssh_host_base_image):
        value = json.loads(docker("image", "inspect", reference))[0]
        if value["Architecture"] != native or value["Os"] != "linux":
            raise ValueError("NATIVE_LINUX_IMAGE_REQUIRED")
    runtime_program = "from pathlib import Path;import hashlib,json,sys;from aelev_postgresql import fencing;p=Path(fencing.__file__).parent;print(json.dumps({'python':sys.version.split()[0],'files':{q.name:hashlib.sha256(q.read_bytes()).hexdigest() for q in p.glob('*.py')}}))"
    runtime_sources = []
    for reference in (FENCER, SQL):
        runtime = json.loads(
            docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--entrypoint",
                "python",
                reference,
                "-c",
                runtime_program,
            )
        )
        if runtime["python"] != "3.13.7":
            raise ValueError("PINNED_PYTHON_RUNTIME_REQUIRED")
        for name, expected in runtime["files"].items():
            path = SOURCE / "postgresql/src/aelev_postgresql" / name
            if (
                not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise ValueError("RUNTIME_SOURCE_DRIFT")
        runtime_sources.append({"image": reference, **runtime})
    proof["verified_runtime_source"] = runtime_sources
    base_id = json.loads(docker("image", "inspect", args.ssh_host_base_image))[0]["Id"]
    ssh_base_tag = "aelev/postgresql-ssh-host-base:sha256-" + base_id.removeprefix(
        "sha256:"
    )
    previous_tag = process(
        ["docker", "image", "inspect", "--format", "{{.Id}}", ssh_base_tag], check=False
    )
    if previous_tag.returncode == 0:
        if previous_tag.stdout.strip() != base_id:
            raise ValueError("TEST_IMAGE_TAG_CONFLICT")
    else:
        docker("tag", base_id, ssh_base_tag)

    def retain_ledgers() -> None:
        """Read only the owned technical journals under shared filesystem locks."""
        import base64

        targets = list(ledgers.values()) + [
            name for name in volumes if name.endswith("-audit")
        ]
        retained = []
        summaries: list[dict[str, Any]] = []
        confirmed_nodes: set[str] = set()
        for name in targets:
            row = process(["docker", "volume", "inspect", name], check=False)
            if row.returncode:
                if proof["status"] == "PASS":
                    raise RuntimeError("TECHNICAL_JOURNAL_MISSING")
                continue
            metadata = json.loads(row.stdout)[0]
            if metadata.get("Labels", {}).get("io.aelev.evidence.scope") != SCOPE:
                raise ValueError("TECHNICAL_JOURNAL_NOT_OWNED")
            program = """from pathlib import Path
    import base64,json,fcntl,os
    root=Path('/journal'); descriptors=[]
    try:
     for name in ['node-operation.lock']+[p.name for p in root.glob('docker-intent-*.lock')]:
      path=root/name
      if path.exists():
       descriptor=os.open(path,os.O_RDONLY);descriptors.append(descriptor);fcntl.flock(descriptor,fcntl.LOCK_SH|fcntl.LOCK_NB)
     print(json.dumps({p.name:base64.b64encode(p.read_bytes()).decode() for p in root.iterdir() if p.is_file()}))
    finally:
     for descriptor in descriptors:os.close(descriptor)
    """
            program = textwrap.dedent("    " + program)
            deadline = time.monotonic() + 20
            while True:
                result = process(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--read-only",
                        "--cap-drop",
                        "ALL",
                        "--label",
                        "io.aelev.evidence.scope=" + SCOPE,
                        "--mount",
                        "type=volume,source="
                        + name
                        + ",target=/journal,readonly,volume-nocopy",
                        "--entrypoint",
                        "python",
                        FENCER,
                        "-c",
                        program,
                    ],
                    check=False,
                )
                if result.returncode == 0:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("TECHNICAL_JOURNAL_SNAPSHOT_UNAVAILABLE")
                time.sleep(0.1)
            files = json.loads(result.stdout)
            intents = []
            events = []
            for filename, value in files.items():
                data = base64.b64decode(value, validate=True)
                if filename.endswith(".json"):
                    intent = safe_intent(json.loads(data))
                    if intent is not None:
                        intents.append(intent)
                elif filename.endswith(".jsonl"):
                    for line in data.splitlines():
                        record = json.loads(line)
                        events.append(record)
                        if (
                            filename == "node-audit.jsonl"
                            and record.get("phase") == "confirmed"
                            and record.get("action") == "fence"
                            and record.get("claim", {}).get("candidate")
                            == "postgresql-" + str(proof.get("replacement_writer"))
                        ):
                            if record.get("identities") != [ids[record["node"]]]:
                                raise RuntimeError(
                                    "TERMINATION_RECEIPT_TARGET_MISMATCH"
                                )
                            confirmed_nodes.add(record["node"])
            destination = OUT / (name + ".journal.private.json")
            destination.write_text(json.dumps(files, indent=2) + "\n")
            retained.append(
                {
                    "name": name,
                    "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                    "files": len(files),
                }
            )
            summaries.append(
                {
                    "journal_number": len(summaries) + 1,
                    "kind": "node" if name in ledgers.values() else "controller",
                    "intents": intents,
                    "audit_counts": safe_audit_counts(events),
                }
            )
        proof["technical_journal_archives"] = retained
        proof["technical_journal_summaries"] = summaries
        if proof["status"] == "PASS":
            expected_nodes = {
                "postgresql-" + str(n)
                for n in range(1, 4)
                if n != proof["replacement_writer"]
            }
            if confirmed_nodes != expected_nodes or len(retained) != 5:
                raise RuntimeError("NATIVE_TERMINATION_AUDIT_INCOMPLETE")
            proof["both_native_peer_termination_audits_retained"] = True

    baseline = inventory()
    baseline_volumes = set(docker("volume", "ls", "-q").split())
    guard = (
        subprocess.Popen(["/usr/bin/caffeinate", "-i"])
        if sys.platform == "darwin"
        else None
    )
    atexit.register(
        lambda: (
            guard.terminate() if guard is not None and guard.poll() is None else None
        )
    )
    subnets: list[str] = []
    ledgers: dict[int, str] = {}
    try:
        existing_ids = docker("network", "ls", "-q").split()
        existing = [
            ipaddress.ip_network(c["Subnet"])
            for r in json.loads(docker("network", "inspect", *existing_ids))
            for c in (r.get("IPAM", {}).get("Config") or [])
            if c.get("Subnet")
        ]
        for n in range(256):
            subnet = ipaddress.ip_network(f"10.248.{n}.0/28")
            if not any(
                p.version == subnet.version and p.overlaps(subnet) for p in existing
            ):
                subnets.append(str(subnet))
            if len(subnets) == 11:
                break
        assert len(subnets) == 11
        values = dict(
            line.split("=", 1)
            for line in (OWNER / ".env.example").read_text().splitlines()
            if line and not line.startswith("#")
        )
        values.update(
            {
                PREFIX + "CLUSTER": SCOPE,
                PREFIX + "BIND": PUBLISHED_BIND,
                PREFIX + "COMPOSE_PROJECT": SCOPE,
                PREFIX + "NETWORK": SCOPE + "-template",
                PREFIX + "IMAGE": SQL,
                PREFIX + "FENCING_IMAGE": FENCER,
                PREFIX + "FENCING_SECRET_GID": str(os.getgid()),
                PREFIX + "FENCING_SSH_AUDIT_VOLUME": SCOPE + "-coordinator-audit",
            }
        )
        fencing_ports = {n: port() for n in (1, 2)}
        ssh_ports = {n: port() for n in (1, 2, 3)}
        values[PREFIX + "FENCING_PORT"] = str(fencing_ports[1])
        for n in range(1, 4):
            for field in ("PORT", "REST_PORT", "ETCD_CLIENT_PORT", "ETCD_PEER_PORT"):
                values[PREFIX + f"NODE_{n}_{field}"] = str(port())
            values[PREFIX + f"NODE_{n}_HOST"] = NODE_ADDRESS
            values[PREFIX + f"NODE_{n}_ETCD_HOST"] = NODE_ADDRESS
            values[PREFIX + f"NODE_{n}_ETCD_PEER_CLIENT_IPS"] = ",".join(
                str(ipaddress.ip_network(subnets[m]).network_address + 1)
                for m in range(3)
            )
            values[PREFIX + f"NODE_{n}_FENCING_URL"] = (
                "https://" + NODE_ADDRESS + ":" + str(fencing_ports[1])
            )
            values[PREFIX + f"NODE_{n}_DATA_VOLUME"] = SCOPE + f"-sql-{n}"
            values[PREFIX + f"NODE_{n}_ETCD_VOLUME"] = SCOPE + f"-dcs-{n}"
        (OWNER / ".env").write_text(
            "\n".join(k + "=" + v for k, v in values.items()) + "\n"
        )
        process([sys.executable, str(OWNER / "prepare-tls.py")], cwd=OWNER)
        observer_inputs = OUT / "sql-observer-inputs"
        observer_inputs.mkdir(mode=0o700)
        for name in ("admin.crt", "admin.key", "admin-password", "ca.crt"):
            shutil.copyfile(OWNER / "secrets/admin" / name, observer_inputs / name)
        observer_volume = volume(SCOPE + "-sql-observer-credentials")
        materialize(observer_inputs, observer_volume)
        phase("fresh public TLS preparation and published endpoint plan")
        for n in range(1, 4):
            for name, cn in (
                ("admin", values[PREFIX + "ADMIN_USER"]),
                ("replication", "aelev_replication"),
            ):
                cert(OWNER / f"secrets/node-{n}", name, cn, "postgresql")
        model = json.loads(
            docker(
                "compose",
                "--env-file",
                str(OWNER / ".env"),
                "-f",
                str(OWNER / "compose.runtime.yml"),
                "-f",
                str(OWNER / "compose.docker.yml"),
                "config",
                "--format",
                "json",
            )
        )
        etcd_image = json.loads(
            docker("image", "inspect", model["services"]["etcd-1"]["image"])
        )[0]
        if etcd_image["Architecture"] != native or etcd_image["Os"] != "linux":
            raise ValueError("NATIVE_ETCD_IMAGE_REQUIRED")
        for index, role in enumerate(
            [f"etcd-{n}" for n in range(1, 4)]
            + [f"postgresql-{n}" for n in range(1, 4)]
        ):
            export(role, model["services"][role], model, index)
        daemon = docker("info", "--format", "{{.ID}}")
        for n in range(1, 3):
            process(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(OUT / f"controller-{n}.key"),
                ]
            )
        context = OUT / "sshd-context"
        context.mkdir(mode=0o700)
        shutil.copy2(OWNER / "fencing-node.py", context / "fencing-node.py")
        keys = (
            "\n".join(
                'restrict,command="/usr/local/bin/python /operator/fencing-node.py --config /node-inputs/node.json" '
                + (OUT / f"controller-{n}.key.pub").read_text().strip()
                for n in (1, 2)
            )
            + "\n"
        )
        (context / "authorized_keys").write_text(keys)
        (context / "sshd_config").write_text(
            "Port 2222\nListenAddress 0.0.0.0\nHostKey /node-inputs/host.key\nPidFile /tmp/sshd.pid\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nPubkeyAuthentication yes\nAuthorizedKeysFile /etc/ssh/fencing-authorized-keys\nStrictModes yes\nPermitRootLogin forced-commands-only\nAllowUsers root\nAllowTcpForwarding no\nX11Forwarding no\nPermitTunnel no\nPermitUserEnvironment no\nPermitUserRC no\nLogLevel VERBOSE\n"
        )
        (context / "Dockerfile").write_text(
            "FROM "
            + ssh_base_tag
            + '\nUSER root\nRUN apk add --no-cache openssh-server=10.0_p1-r11 && mkdir -p /run/sshd && chmod 700 /root\nCOPY authorized_keys /etc/ssh/fencing-authorized-keys\nRUN chmod 600 /etc/ssh/fencing-authorized-keys\nCOPY fencing-node.py /operator/fencing-node.py\nCOPY sshd_config /etc/ssh/fencing-sshd-config\nENTRYPOINT ["/usr/sbin/sshd","-D","-e","-f","/etc/ssh/fencing-sshd-config"]\n'
        )
        process(
            [
                "docker",
                "build",
                "--platform",
                PLATFORM,
                "--iidfile",
                str(OUT / "sshd-image"),
                str(context),
            ],
            timeout=300,
        )
        sshd = (OUT / "sshd-image").read_text().strip()
        peer_hosts = ""
        for n in range(1, 4):
            node = f"postgresql-{n}"
            folder = OUT / f"node-{n}"
            folder.mkdir(mode=0o700)
            process(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(folder / "host.key"),
                ]
            )
            peer_hosts += (
                "["
                + NODE_ADDRESS
                + "]:"
                + str(ssh_ports[n])
                + " "
                + (folder / "host.key.pub").read_text()
            )
            credentials = folder / "credentials"
            credentials.mkdir(mode=0o700)
            cert(credentials, "etcd-client", f"fencing-node-{n}", "etcd")
            shutil.copy2(OWNER / "secrets/issuer/etcd.crt", credentials / "etcd-ca.crt")
            secret_volume = volume(
                SCOPE + f"-node-{n}-credentials", node=node, kind="credentials"
            )
            materialize(credentials, secret_volume)
            ledgers[n] = volume(SCOPE + f"-node-{n}-ledger", node=node, kind="state")
            configuration = {
                "schema": 1,
                "cluster": SCOPE,
                "node": node,
                "daemon_id": daemon,
                "image": FENCER,
                "state_volume": ledgers[n],
                "secrets_volume": secret_volume,
                "etcd": [
                    {
                        "host": NODE_ADDRESS,
                        "port": int(values[PREFIX + f"NODE_{m}_ETCD_CLIENT_PORT"]),
                    }
                    for m in range(1, 4)
                ],
                "timeout_seconds": 20,
            }
            (folder / "node.json").write_text(json.dumps(configuration))
            role = f"ssh-node-{n}"
            service = {
                "image": sshd,
                "read_only": True,
                "cap_drop": ["ALL"],
                "cap_add": ["SETUID", "SETGID", "SYS_CHROOT", "DAC_OVERRIDE"],
                "security_opt": ["no-new-privileges:true"],
                "tmpfs": ["/tmp:size=16m,mode=1777"],
                "mem_limit": "256m",
                "ports": [
                    {
                        "host_ip": PUBLISHED_BIND,
                        "published": str(ssh_ports[n]),
                        "target": 2222,
                        "protocol": "tcp",
                    }
                ],
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(folder),
                        "target": "/node-inputs",
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": "/var/run/docker.sock",
                        "target": "/var/run/docker.sock",
                    },
                ],
            }
            export(role, service, {}, 5 + n)
        base_fencer = json.loads(
            docker(
                "compose",
                "--env-file",
                str(OWNER / ".env"),
                "-f",
                str(OWNER / "compose.fencing-ssh.yml"),
                "config",
                "--format",
                "json",
            )
        )
        for n in (1, 2):
            folder = OUT / f"controller-{n}"
            folder.mkdir(mode=0o700)
            cert(folder, "server", "postgresql-fencer", "postgresql", server=True)
            cert(folder, "admin", values[PREFIX + "ADMIN_USER"], "postgresql")
            cert(folder, "etcd-client", f"fencing-controller-{n}", "etcd")
            shutil.copy2(OWNER / "secrets/issuer/postgresql.crt", folder / "ca.crt")
            shutil.copy2(OWNER / "secrets/issuer/etcd.crt", folder / "etcd-ca.crt")
            shutil.copy2(OUT / f"controller-{n}.key", folder / "ssh-client.key")
            (folder / "known_hosts").write_text(peer_hosts)
            bindings = {
                f"postgresql-{m}": {
                    "host": NODE_ADDRESS,
                    "port": ssh_ports[m],
                    "user": "root",
                    "identity_file": "/run/aelev/secrets/ssh-client.key",
                    "known_hosts_file": "/run/aelev/secrets/known_hosts",
                    "daemon_id": daemon,
                }
                for m in range(1, 4)
            }
            (folder / "ssh-bindings.json").write_text(json.dumps(bindings))
            credential_volume = volume(SCOPE + f"-controller-{n}-credentials")
            materialize(folder, credential_volume)
            audit_volume = volume(SCOPE + f"-controller-{n}-audit")
            role = f"controller-{n}"
            service = copy.deepcopy(base_fencer["services"]["postgresql-fencer"])
            service["restart"] = "no"
            service["environment"][PREFIX + "FENCING_PORT"] = str(fencing_ports[n])
            service["ports"] = [
                {
                    "host_ip": PUBLISHED_BIND,
                    "published": str(fencing_ports[n]),
                    "target": fencing_ports[n],
                    "protocol": "tcp",
                }
            ]
            service["volumes"] = [
                {
                    "type": "volume",
                    "source": "credentials",
                    "target": "/run/aelev/secrets",
                    "read_only": True,
                },
                {
                    "type": "volume",
                    "source": "audit",
                    "target": "/var/lib/aelev-fencing",
                },
            ]
            adapted = {
                "volumes": {
                    "credentials": {"name": credential_volume, "external": True},
                    "audit": {"name": audit_volume, "external": True},
                }
            }
            export(role, service, adapted, 8 + n)
        for s in held:
            s.close()
        held.clear()
        phase(
            "distinct role keys, private credential volumes and eleven isolated role recipes"
        )
        for n in range(1, 4):
            start(f"ssh-node-{n}")
            docker(
                "exec",
                ids[f"ssh-node-{n}"],
                "python",
                "/operator/fencing-node.py",
                "--config",
                "/node-inputs/node.json",
                "--initialize",
            )
        for n in range(1, 4):
            start(f"etcd-{n}")
        healthy([f"etcd-{n}" for n in range(1, 4)])
        phase("real three-member etcd quorum over published mTLS endpoints")
        for n in (1, 2):
            start(f"controller-{n}")
        healthy(["controller-1", "controller-2"])
        phase("two independent native fencers ready with ordinary cap_drop ALL")
        for n in range(1, 4):
            start(f"postgresql-{n}")
        first = writer(seconds=240)
        proof["initial_writer"] = first
        phase("three native SQL participants and one primary over published endpoints")
        table = "ssh_fencing_audit_" + uuid.uuid4().hex
        sql(
            first,
            f"with connection() as c:\n c.execute('set statement_timeout=15000')\n c.execute('create table {table} (id integer primary key, payload text not null)')\n c.execute(\"insert into {table} select n,md5(n::text) from generate_series(1,1000) n\")\n",
        )
        digest_code = f"with connection() as c:\n print(hashlib.sha256(json.dumps(c.execute('select * from {table} order by id').fetchall()).encode()).hexdigest())\n"
        digest = sql(first, digest_code).stdout.strip()
        original_start = json.loads(docker("inspect", ids[f"postgresql-{first}"]))[0][
            "State"
        ]["StartedAt"]
        phase("1000 rows synchronously committed before failure")
        fault = process(
            [
                "docker",
                "exec",
                "--user",
                "postgres",
                "-i",
                ids[f"postgresql-{first}"],
                "python",
                "-",
                "suspend",
                values[PREFIX + f"NODE_{first}_REST_PORT"],
            ],
            body=observer_source,
        )
        proof["patroni_fault"] = json.loads(fault.stdout)
        assert roles().get(first) is False
        phase("Patroni suspended while old SQL writer remains alive")
        second = writer(exclude=first, seconds=210)
        state = json.loads(docker("inspect", ids[f"postgresql-{first}"]))[0]["State"]
        assert state["StartedAt"] != original_start
        assert sql(second, digest_code).stdout.strip() == digest
        phase(
            "native failover fenced the old process, returned peers and preserved all committed rows"
        )
        proof.update(
            initial_writer=first,
            replacement_writer=second,
            committed_rows=1000,
            committed_rows_sha256=digest,
            real_ha_failover=True,
        )
        # Test an unknown RPC result and loss of one coordinator, without stopping
        # the current writer or changing its native lease.
        replica = next(n for n in range(1, 4) if n != second)
        docker("stop", "-t", "5", ids["controller-2"])
        claim = json.loads(
            docker(
                "exec",
                ids["controller-1"],
                "python",
                "-c",
                "from dataclasses import asdict;import json;from aelev_postgresql.fencing import Claim;print(json.dumps(asdict(Claim.current())))",
            )
        )
        tls = ssl.create_default_context(cafile=str(OWNER / "secrets/admin/ca.crt"))
        tls.load_cert_chain(
            OWNER / "secrets/admin/admin.crt", OWNER / "secrets/admin/admin.key"
        )
        c = http.client.HTTPSConnection(
            OPERATOR_HOST, fencing_ports[1], context=tls, timeout=20
        )
        request = {
            **claim,
            "node": f"postgresql-{replica}",
            "request_id": str(uuid.uuid4()),
        }
        proof["unknown_rpc_request_id"] = request["request_id"]
        c.request(
            "POST", "/fence", json.dumps(request), {"Content-Type": "application/json"}
        )
        c.close()  # The caller deliberately receives no acknowledgement.
        end = time.monotonic() + 20
        pending = None
        while time.monotonic() < end:
            pending = node_intent(replica)
            if pending and pending["phase"] == "STOP_CONFIRMED":
                break
            time.sleep(0.1)
        assert (
            pending
            and pending["phase"] == "STOP_CONFIRMED"
            and pending["start_attempts"] == 0
        ), "CRASH_FAULT_POINT_MISSED"
        docker("kill", "--signal", "KILL", ids["controller-1"])
        docker("start", ids["controller-2"])
        healthy(["controller-2"])
        end = time.monotonic() + 60
        completed = None
        while time.monotonic() < end:
            completed = node_intent(replica)
            if completed and completed["phase"] == "COMPLETED":
                break
            time.sleep(0.2)
        assert (
            completed
            and completed["phase"] == "COMPLETED"
            and completed["start_attempts"] == 1
        )
        assert sql(second, digest_code).stdout.strip() == digest
        phase(
            "second coordinator recovered unknown RPC result using the target ledger and one persisted start"
        )
        old_coordinator = ids["controller-1"]
        info = json.loads(docker("inspect", old_coordinator))[0]
        assert info["Config"]["Labels"]["io.aelev.evidence.scope"] == SCOPE
        docker("rm", old_coordinator)
        start("controller-1")
        assert ids["controller-1"] != old_coordinator
        healthy(["controller-1", "controller-2"])
        final_writer = writer(seconds=90)
        assert final_writer == second
        assert node_intent(replica)["start_attempts"] == 1
        assert sql(final_writer, digest_code).stdout.strip() == digest
        phase(
            "coordinator replacement preserved identities, target history, budget and SQL primary"
        )
        restored = sql(
            final_writer,
            "table="
            + repr(table)
            + "\nexpected="
            + repr(digest)
            + "\n"
            + textwrap.dedent(
                """
                import os,pathlib,subprocess,tempfile
                from psycopg.conninfo import make_conninfo
                with connection() as current:
                    host,port=current.info.host,current.info.port
                credentials=dict(host=host,port=port,user=setting('ADMIN_USER'),dbname='postgres',sslmode='verify-full',sslrootcert=str(SECRETS/'ca.crt'),sslcert=str(SECRETS/'admin.crt'),sslkey=str(SECRETS/'admin.key'))
                with tempfile.TemporaryDirectory(prefix='aelev-ssh-backup-restore-') as temporary:
                    directory=pathlib.Path(temporary)
                    data=directory/'data'
                    subprocess.run(['pg_basebackup','--dbname='+make_conninfo(**credentials),'--pgdata='+str(data),'--wal-method=stream','--checkpoint=fast','--manifest-checksums=SHA256'],env={**os.environ,'PGPASSWORD':secret('admin-password')},check=True,capture_output=True,timeout=90)
                    subprocess.run(['pg_verifybackup',str(data)],check=True,capture_output=True,timeout=30)
                    manifest=hashlib.sha256((data/'backup_manifest').read_bytes()).hexdigest()
                    for name in ('standby.signal','recovery.signal'):
                        (data/name).unlink(missing_ok=True)
                    subprocess.run(['chown','-R','postgres:postgres',str(directory)],check=True,capture_output=True)
                    options=f"-p 16432 -c listen_addresses=127.0.0.1 -c unix_socket_directories={directory} -c data_directory={data} -c hba_file={data}/pg_hba.conf -c primary_conninfo='' -c archive_mode=off -c synchronous_standby_names='' -c shared_buffers=32MB -c max_connections=30"
                    ctl=['su-exec','postgres','pg_ctl','-D',str(data),'-w']
                    try:
                        subprocess.run([*ctl,'-l',str(directory/'restore.log'),'-o',options,'start'],check=True,capture_output=True,timeout=40)
                        credentials.update(host='127.0.0.1',port=16432,password=secret('admin-password'))
                        with psycopg.connect(**credentials) as copy:
                            rows=copy.execute('select * from '+table+' order by id').fetchall()
                            actual=hashlib.sha256(json.dumps(rows).encode()).hexdigest()
                            assert len(rows)==1000 and actual==expected
                            assert copy.execute('select pg_is_in_recovery()').fetchone()[0] is False
                        print(json.dumps(dict(status='PASS',manifest_sha256=manifest,restored_rows=len(rows),restored_rows_sha256=actual,source_transport='verify-full mTLS published primary',restore_listener='127.0.0.1:16432',independent_backup_storage_proven=False,pitr_proven=False)))
                    finally:
                        subprocess.run([*ctl,'-m','fast','stop'],check=False,capture_output=True,timeout=30)
                """
            ),
            timeout=210,
        )
        proof["sql_physical_backup_restore"] = json.loads(restored.stdout)
        assert sql(final_writer, digest_code).stdout.strip() == digest
        assert writer(seconds=60) == final_writer
        phase(
            "native basebackup manifest verified and separate loopback SQL restored all committed rows"
        )
        proof.update(
            status="PASS",
            lost_rpc_ack_reconciled=True,
            coordinator_replica_recovery=True,
            independent_controller_credentials=True,
            coordinator_replacement_preserved_budget=True,
            persisted_start_attempts=1,
            dedicated_role_networks=11,
            inter_role_routes="published node endpoints: " + NODE_ADDRESS,
            three_etcd_members=True,
            sql_writers_observed_at_most_one=True,
        )
    except BaseException as error:
        proof.update(
            status="FAIL",
            failure_type=type(error).__name__,
            failure_stage=history[-1]["stage"] if history else "preparation",
        )
        (OUT / "failure.private.txt").write_text(__import__("traceback").format_exc())
        raise
    finally:
        for s in held:
            s.close()
        save()
        archive_error: Exception | None = None
        original_status = proof["status"]
        try:
            retain_ledgers()
        except (OSError, ValueError, RuntimeError) as error:
            archive_error = error
            proof["status"] = "FAIL"
            proof["journal_archive_error"] = type(error).__name__
        for role, identity in ids.items():
            state = json.loads(docker("inspect", identity))[0]
            assert state["Config"]["Labels"]["io.aelev.evidence.scope"] == SCOPE
            (OUT / (role + ".state.private.json")).write_text(
                json.dumps(state, indent=2)
            )
            logs = process(["docker", "logs", identity], check=False)
            proof.setdefault("native_role_states", {})[role] = safe_runtime_state(
                state, logs.stdout + logs.stderr
            )
            (OUT / (role + ".log.private")).write_text(logs.stdout + logs.stderr)
        for role in reversed(list(recipes)):
            compose(role, "down", "--remove-orphans", check=False)
        observer = process(["docker", "inspect", observer_name], check=False)
        if observer.returncode == 0:
            assert (
                json.loads(observer.stdout)[0]["Config"]["Labels"][
                    "io.aelev.evidence.scope"
                ]
                == SCOPE
            )
            docker("rm", "--force", observer_name)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            transient = docker(
                "ps",
                "-aq",
                "--filter",
                "label=io.aelev.postgresql.fencing-cluster=" + SCOPE,
            ).split()
            if not transient:
                break
            time.sleep(0.5)
        assert not transient, "OWNED_NODE_OPERATIONS_STILL_RUNNING"
        for name in reversed(volumes):
            row = process(["docker", "volume", "inspect", name], check=False)
            if row.returncode == 0:
                info = json.loads(row.stdout)[0]
                assert info["Labels"]["io.aelev.evidence.scope"] == SCOPE
                docker("volume", "rm", name)
        after = inventory()
        proof["baseline_containers_preserved"] = all(
            after.get(k) == value for k, value in baseline.items()
        )
        assert proof[
            "baseline_containers_preserved"
        ], "BASELINE_CONTAINER_STATE_CHANGED"
        proof["owned_resources_removed"] = True
        (OUT / "proof.json").write_text(
            json.dumps({**proof, "stages": history}, indent=2) + "\n"
        )
        if guard is not None:
            guard.terminate()
            guard.wait()
        if original_status == "PASS" and archive_error is not None:
            raise RuntimeError("TECHNICAL_JOURNAL_RETENTION_FAILED") from archive_error
    return proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sql-image", required=True)
    parser.add_argument("--fencing-image", required=True)
    parser.add_argument(
        "--ssh-host-base-image",
        required=True,
        help="Prebuilt owner checks image; only used as the isolated administrative SSH host fixture",
    )
    args = parser.parse_args()
    print(json.dumps(run(args)))


if __name__ == "__main__":
    main()
