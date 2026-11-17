"""Qualify a fresh Kafka/SQL/HTTPS Translator chain; never select working state."""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from provider_tls_fixtures import CLIENT, SERVER, Authority, openssl

SOURCE = Path(__file__).resolve().parents[1]
LABEL = "io.aelev.translator-queue-qualification"
SQL_HOST, BROKER_HOST, PROVIDER_HOST = (
    "database-node.test",
    "broker-node.test",
    "provider-node.test",
)
TLS_ROOT = "/run/secrets/compute-provider"
PIN = re.compile(r"(?:sha256:[a-f0-9]{64}|[^\s@]+@sha256:[a-f0-9]{64})")


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def private_write(path: Path, value: bytes) -> None:
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def port() -> int:
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return int(stream.getsockname()[1])


class Harness:
    def __init__(self, root: Path, initializer: str) -> None:
        self.root, self.initializer = root, initializer
        self.scope = "tq-" + uuid.uuid4().hex[:12]
        self.networks: list[str] = []
        self.volumes: list[str] = []
        self.names: list[str] = []
        self.sequence = 0
        self.baseline = self.snapshot()

    def run(
        self,
        args: list[str],
        *,
        data: str | None = None,
        check: bool = True,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        self.sequence += 1
        result = subprocess.run(
            ["docker", *args],
            input=data,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        stem = self.root / f"command-{self.sequence:04}"
        private_write(stem.with_suffix(".stdout"), result.stdout.encode())
        private_write(stem.with_suffix(".stderr"), result.stderr.encode())
        private_write(
            stem.with_suffix(".json"),
            canonical({"arguments": args, "exit_code": result.returncode}),
        )
        if check and result.returncode:
            raise RuntimeError("TRANSLATOR_QUEUE_COMMAND_FAILED")
        return result

    def snapshot(self) -> dict[str, Any]:
        listed = self.run(["ps", "--all", "--quiet", "--no-trunc"]).stdout.split()
        if not listed:
            return {}
        format = (
            "{{.Id}}|{{.Name}}|{{.Image}}|{{.State.Status}}|{{.State.StartedAt}}|"
            "{{.RestartCount}}|{{.HostConfig.AutoRemove}}|{{json .Mounts}}|"
            "{{json .NetworkSettings.Networks}}"
        )
        result = self.run(["inspect", "--format", format, *listed], check=False)
        if result.returncode:
            assert result.returncode == 1 and all(
                "no such object:" in line.lower()
                for line in result.stderr.splitlines()
                if line
            )
        records = {}
        for line in result.stdout.splitlines():
            fields = line.split("|")
            if fields[6] == "true" or fields[1].startswith("/" + self.scope):
                continue
            mounts = sorted(
                json.loads(fields[7]), key=lambda row: json.dumps(row, sort_keys=True)
            )
            records[fields[0]] = [*fields[1:6], mounts, json.loads(fields[8])]
        return records

    def volume(self, name: str, files: dict[str, bytes], uid: int = 10001) -> str:
        target = self.scope + "-" + name
        self.run(["volume", "create", "--label", LABEL + "=" + self.scope, target])
        self.volumes.append(target)
        initializer = """import base64,json,os,pathlib,sys
root=pathlib.Path('/fixture');assert not list(root.iterdir())
value=json.load(sys.stdin);uid=value['uid']
for name,body in value['files'].items():
 assert '/' not in name and name not in {'.','..'}
 path=root/name;path.write_bytes(base64.b64decode(body,validate=True));path.chmod(0o600);os.chown(path,uid,uid)
root.chmod(0o700);os.chown(root,uid,uid)
"""
        self.run(
            [
                "run",
                "--rm",
                "--interactive",
                "--network",
                "none",
                "--label",
                LABEL + "=" + self.scope,
                "--user",
                "0:0",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--security-opt",
                "no-new-privileges:true",
                "--mount",
                "type=volume,src=" + target + ",dst=/fixture",
                "--entrypoint",
                "python",
                self.initializer,
                "-c",
                initializer,
            ],
            data=json.dumps(
                {
                    "uid": uid,
                    "files": {
                        key: base64.b64encode(body).decode()
                        for key, body in files.items()
                    },
                }
            ),
        )
        return target

    def network(self, name: str, subnet: str) -> str:
        target = self.scope + "-" + name
        self.run(
            [
                "network",
                "create",
                "--label",
                LABEL + "=" + self.scope,
                "--subnet",
                subnet,
                target,
            ]
        )
        self.networks.append(target)
        return target

    def container(
        self,
        name: str,
        image: str,
        network: str,
        *,
        environment: dict[str, str] | None = None,
        mounts: list[str] | None = None,
        arguments: list[str] | None = None,
        options: list[str] | None = None,
        readonly: bool = True,
    ) -> str:
        target = self.scope + "-" + name
        command = [
            "run",
            "--detach",
            "--name",
            target,
            "--label",
            LABEL + "=" + self.scope,
            "--network",
            network,
            "--restart",
            "no",
            "--memory",
            "1g",
            "--cpus",
            "1",
        ]
        if readonly:
            command += [
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
            ]
        for host in (SQL_HOST, BROKER_HOST, PROVIDER_HOST):
            command += ["--add-host", host + ":host-gateway"]
        if environment:
            filename = self.root / (name + ".env")
            assert all(
                "\n" not in value and "\r" not in value
                for value in environment.values()
            )
            private_write(
                filename,
                (
                    "\n".join(
                        key + "=" + value for key, value in sorted(environment.items())
                    )
                    + "\n"
                ).encode(),
            )
            command += ["--env-file", str(filename)]
        for mount in mounts or []:
            command += ["--mount", mount]
        command += options or []
        command += [image, *(arguments or [])]
        self.run(command)
        self.names.append(target)
        return target

    def probe(self, name: str, action: str) -> dict[str, Any]:
        code = (SOURCE / "test/network_queue_probe.py").read_text()
        result = self.run(
            ["exec", "--interactive", name, "python", "-", action],
            data=code,
            timeout=75,
        )
        value = json.loads(result.stdout.splitlines()[-1])
        assert isinstance(value, dict)
        return value

    def cleanup(self) -> None:
        own = self.run(
            ["ps", "--all", "--quiet", "--filter", "label=" + LABEL + "=" + self.scope]
        ).stdout.split()
        for identifier in own:
            labels = json.loads(
                self.run(
                    ["inspect", "--format", "{{json .Config.Labels}}", identifier]
                ).stdout
            )
            assert labels.get(LABEL) == self.scope
            self.run(["stop", "--timeout", "10", identifier], check=False)
            self.run(["rm", identifier])
        for name in reversed(self.volumes):
            value = json.loads(self.run(["volume", "inspect", name]).stdout)[0]
            assert value["Labels"].get(LABEL) == self.scope
            self.run(["volume", "rm", name])
        for name in reversed(self.networks):
            value = json.loads(self.run(["network", "inspect", name]).stdout)[0]
            assert value["Labels"].get(LABEL) == self.scope and not value["Containers"]
            self.run(["network", "rm", name])
        after = self.snapshot()
        assert all(after.get(key) == value for key, value in self.baseline.items())


def subnets(harness: Harness, count: int) -> list[str]:
    identifiers = harness.run(["network", "ls", "--quiet"]).stdout.split()
    used = []
    for row in json.loads(harness.run(["network", "inspect", *identifiers]).stdout):
        for item in (row.get("IPAM") or {}).get("Config") or []:
            if item.get("Subnet"):
                used.append(ipaddress.ip_network(item["Subnet"], strict=False))
    result = []
    for network in ipaddress.ip_network("10.251.0.0/16").subnets(new_prefix=28):
        if not any(old.version == 4 and old.overlaps(network) for old in used):
            result.append(str(network))
        if len(result) == count:
            return result
    raise RuntimeError("TRANSLATOR_QUEUE_SUBNETS_UNAVAILABLE")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("core-image", "worker-image", "database-image", "kafka-image"):
        parser.add_argument("--" + argument, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--provider-image")
    parser.add_argument("--provider-credential", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--priority", action="store_true")
    args = parser.parse_args()
    images = {
        role: getattr(args, role + "_image")
        for role in ("core", "worker", "database", "kafka")
    }
    if args.live:
        if not all((args.provider_image, args.provider_credential, args.request)):
            raise ValueError("TRANSLATOR_QUEUE_LIVE_INPUTS_REQUIRED")
        images["provider"] = args.provider_image
    if any(PIN.fullmatch(image) is None for image in images.values()):
        raise ValueError("TRANSLATOR_QUEUE_IMMUTABLE_IMAGES_REQUIRED")
    os.umask(0o077)
    base = args.output.resolve()
    if base.is_relative_to(SOURCE.parent):
        raise ValueError("TRANSLATOR_QUEUE_PRIVATE_OUTPUT_OUTSIDE_SOURCE_REQUIRED")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="translator-network-queue-", dir=base))
    record: dict[str, Any] = {
        "schema_version": 1,
        "status": "FAIL",
        "images": images,
        "live_provider_selected": args.live,
        "test_pki": True,
        "production_identity_admitted": False,
        "physical_distribution_verified": False,
        "release_admitted": False,
        "working_runtime_switched": False,
        "queue_resume_issued": False,
        "inference_requested": False,
        "request_topic": (
            "translation.requests.priority.v2"
            if args.priority
            else "translation.requests.v2"
        ),
    }
    harness = Harness(root, images["worker"])
    try:
        test_sources = (
            "network_queue_lifecycle.py",
            "network_queue_probe.py",
            "network_queue_fixture.py",
            "provider_tls_fixtures.py",
        )
        test_hashes = {
            name: hashlib.sha256((SOURCE / "test" / name).read_bytes()).hexdigest()
            for name in test_sources
        }
        record["test_source_sha256"] = test_hashes
        engine = (
            harness.run(["info", "--format", "{{.OSType}}|{{.Architecture}}"])
            .stdout.strip()
            .split("|")
        )
        architectures = {"aarch64": "arm64", "x86_64": "amd64"}
        native = [engine[0], architectures.get(engine[1], engine[1])]
        record["native_platform"] = "/".join(native)
        record["local_images"] = {}
        for role, image in images.items():
            inspected = (
                harness.run(
                    [
                        "image",
                        "inspect",
                        "--format",
                        "{{.Id}}|{{.Os}}|{{.Architecture}}",
                        image,
                    ]
                )
                .stdout.strip()
                .split("|")
            )
            assert inspected[1:] == native
            record["local_images"][role] = inspected[0]
        expected_modules = (
            "runtime.py",
            "repository.py",
            "contracts.py",
            "environment.py",
            "provider_transport.py",
            "provider_identity.py",
        )
        expected = {
            name: hashlib.sha256(
                (SOURCE / "src/translator_service" / name).read_bytes()
            ).hexdigest()
            for name in expected_modules
        }
        for role in ("core", "worker"):
            program = (
                "import hashlib,json,platform,sqlite3,psycopg;from pathlib import Path;"
                "print(json.dumps({'python':platform.python_version(),"
                "'sqlite':sqlite3.sqlite_version,'psycopg':psycopg.__version__,"
                "'files':{name:hashlib.sha256((Path('/opt/aelev/src/translator_service')"
                "/name).read_bytes()).hexdigest() for name in "
                + repr(expected_modules)
                + "}}))"
            )
            value = json.loads(
                harness.run(
                    [
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--label",
                        LABEL + "=" + harness.scope,
                        "--entrypoint",
                        "python",
                        images[role],
                        "-c",
                        program,
                    ]
                ).stdout
            )
            assert value == {
                "python": "3.13.7",
                "sqlite": "3.53.4",
                "psycopg": "3.3.9",
                "files": expected,
            }
        record["runtime_source_sha256"] = expected
        networks = {
            role: harness.network(role, network)
            for role, network in zip(
                ("sql", "broker", "provider", "worker", "consumer", "outbox", "probe"),
                subnets(harness, 7),
                strict=True,
            )
        }
        sql_port, broker_port, provider_port = port(), port(), port()
        assert len({sql_port, broker_port, provider_port}) == 3
        record["published_ports"] = {
            "sql": sql_port,
            "broker": broker_port,
            "provider": provider_port,
        }
        sql_ca, broker_ca, provider_ca = (
            Authority(root / (name + "-ca")) for name in ("sql", "broker", "provider")
        )
        password, bootstrap_password = secrets.token_hex(24), secrets.token_hex(24)
        server_crt, server_key = sql_ca.leaf(
            [], dns_names=(SQL_HOST,), common_name=SQL_HOST
        )
        sql_identity = harness.volume(
            "sql-server",
            {
                "server.crt": server_crt.read_bytes(),
                "server.key": server_key.read_bytes(),
                "ca.crt": sql_ca.cert.read_bytes(),
                "hba.conf": (
                    b"local all all trust\n"
                    b"hostssl all all 0.0.0.0/0 scram-sha-256 clientcert=verify-full\n"
                    b"hostnossl all all 0.0.0.0/0 reject\n"
                ),
                "bootstrap-password": bootstrap_password.encode(),
            },
            999,
        )
        sql_data = harness.volume("sql-data", {}, 999)
        database = harness.container(
            "database",
            images["database"],
            networks["sql"],
            environment={"POSTGRES_PASSWORD_FILE": "/run/sql-test/bootstrap-password"},
            mounts=[
                "type=volume,src=" + sql_identity + ",dst=/run/sql-test,readonly",
                "type=volume,src=" + sql_data + ",dst=/var/lib/postgresql/data",
            ],
            options=["--publish", f"0.0.0.0:{sql_port}:5432"],
            arguments=[
                "postgres",
                "-c",
                "ssl=on",
                "-c",
                "ssl_cert_file=/run/sql-test/server.crt",
                "-c",
                "ssl_key_file=/run/sql-test/server.key",
                "-c",
                "ssl_ca_file=/run/sql-test/ca.crt",
                "-c",
                "hba_file=/run/sql-test/hba.conf",
            ],
            readonly=False,
        )
        deadline = time.monotonic() + 60
        while harness.run(
            ["exec", database, "pg_isready", "-U", "postgres"], check=False
        ).returncode:
            if time.monotonic() > deadline:
                raise RuntimeError("TRANSLATOR_QUEUE_DATABASE_NOT_READY")
            time.sleep(1)
        harness.run(
            [
                "exec",
                "--interactive",
                database,
                "psql",
                "-U",
                "postgres",
                "--set",
                "ON_ERROR_STOP=1",
            ],
            data='CREATE ROLE "queue-owner" LOGIN PASSWORD \''
            + password
            + '\';\nCREATE DATABASE queue_fixture OWNER "queue-owner";\n',
        )
        broker_crt, broker_key = broker_ca.leaf(
            [], dns_names=(BROKER_HOST, "localhost"), common_name=BROKER_HOST
        )
        pkcs8 = openssl("pkcs8", "-topk8", "-nocrypt", "-in", str(broker_key)).encode()
        properties = {
            "process.roles": "broker,controller",
            "node.id": "1",
            "controller.quorum.voters": "1@localhost:29093",
            "listeners": "CLIENT://0.0.0.0:9092,CONTROLLER://127.0.0.1:29093",
            "advertised.listeners": f"CLIENT://{BROKER_HOST}:{broker_port}",
            "listener.security.protocol.map": "CLIENT:SSL,CONTROLLER:SSL",
            "controller.listener.names": "CONTROLLER",
            "inter.broker.listener.name": "CLIENT",
            "ssl.keystore.type": "PEM",
            "ssl.keystore.location": "/run/broker-test/broker.pem",
            "ssl.truststore.type": "PEM",
            "ssl.truststore.location": "/run/broker-test/ca.crt",
            "ssl.client.auth": "required",
            "ssl.endpoint.identification.algorithm": "HTTPS",
            "authorizer.class.name": (
                "org.apache.kafka.metadata.authorizer.StandardAuthorizer"
            ),
            "super.users": "User:CN=queue-admin;User:CN=" + BROKER_HOST,
            "allow.everyone.if.no.acl.found": "false",
            "auto.create.topics.enable": "false",
            "offsets.topic.replication.factor": "1",
            "offsets.topic.num.partitions": "1",
            "transaction.state.log.replication.factor": "1",
            "transaction.state.log.min.isr": "1",
            "group.initial.rebalance.delay.ms": "0",
            "log.dirs": "/var/lib/kafka/data/logs",
            "metadata.log.dir": "/var/lib/kafka/data/meta",
        }
        kafka_identity = harness.volume(
            "broker-server",
            {
                "broker.pem": pkcs8 + b"\n" + broker_crt.read_bytes(),
                "ca.crt": broker_ca.cert.read_bytes(),
                "server.properties": (
                    "\n".join(key + "=" + value for key, value in properties.items())
                    + "\n"
                ).encode(),
            },
            1000,
        )
        kafka_data = harness.volume("broker-data", {}, 1000)
        broker_mounts = [
            "type=volume,src=" + kafka_identity + ",dst=/run/broker-test,readonly",
            "type=volume,src=" + kafka_data + ",dst=/var/lib/kafka/data",
        ]
        cluster = base64.urlsafe_b64encode(uuid.uuid4().bytes).rstrip(b"=").decode()
        harness.run(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--label",
                LABEL + "=" + harness.scope,
                "--mount",
                broker_mounts[0],
                "--mount",
                broker_mounts[1],
                "--entrypoint",
                "/opt/kafka/bin/kafka-storage.sh",
                images["kafka"],
                "format",
                "--cluster-id",
                cluster,
                "--config",
                "/run/broker-test/server.properties",
            ]
        )
        broker = harness.container(
            "broker",
            images["kafka"],
            networks["broker"],
            mounts=broker_mounts,
            options=[
                "--publish",
                f"0.0.0.0:{broker_port}:9092",
                "--entrypoint",
                "/opt/kafka/bin/kafka-server-start.sh",
            ],
            arguments=["/run/broker-test/server.properties"],
            readonly=False,
        )
        identity_policy = {
            "schema_version": 1,
            "environment": "local",
            "delivery": "pem-files-v1",
            "server_spiffe_id": SERVER,
            "client_spiffe_id": CLIENT,
        }
        provider_crt, provider_key = provider_ca.leaf(
            [SERVER], dns_names=(PROVIDER_HOST,), common_name=PROVIDER_HOST
        )
        provider_identity = harness.volume(
            "provider-svid",
            {
                "identity.json": canonical(identity_policy),
                "transport.json": canonical(
                    {
                        "schema_version": 1,
                        "transport": "mtls",
                        "listen_host": "0.0.0.0",
                        "listen_port": 18487,
                    }
                ),
                "server.crt": provider_crt.read_bytes(),
                "server.key": provider_key.read_bytes(),
                "client-ca.crt": provider_ca.cert.read_bytes(),
            },
        )
        provider_options = ["--publish", f"0.0.0.0:{provider_port}:18487"]
        provider_mounts = [
            "type=volume,src=" + provider_identity + ",dst=" + TLS_ROOT + ",readonly"
        ]
        if args.live:
            assert not args.provider_credential.is_symlink()
            credential = args.provider_credential.resolve()
            assert credential.is_file() and credential.stat().st_mode & 0o007 == 0
            provider_mounts += [
                "type=bind,src="
                + str(credential)
                + ",dst=/run/secrets/provider/credential.env,readonly"
            ]
            provider_options += ["--group-add", str(credential.stat().st_gid)]
            provider = harness.container(
                "provider",
                images["provider"],
                networks["provider"],
                mounts=provider_mounts,
                options=provider_options,
            )
            assert not args.request.is_symlink() and args.request.is_file()
            assert args.request.stat().st_uid == os.getuid()
            assert args.request.stat().st_mode & 0o077 == 0
            request_raw = args.request.read_bytes()
            record["request_file_sha256"] = hashlib.sha256(request_raw).hexdigest()
            request_data = json.loads(request_raw)
            assert set(request_data) == {"source_language", "target_language", "text"}
        else:
            fixture = (SOURCE / "test/network_queue_fixture.py").read_text()
            provider = harness.container(
                "provider",
                images["core"],
                networks["provider"],
                mounts=provider_mounts,
                options=provider_options,
                environment={"QUEUE_FIXTURE_ALLOWED_CLIENT": CLIENT},
                arguments=["python", "-c", fixture],
            )
            request_data = {
                "source_language": "en",
                "target_language": "de",
                "text": "Controlled transport fixture with 27 observations.",
            }
        assert (
            isinstance(request_data["text"], str)
            and 0 < len(request_data["text"]) <= 800
            and not any(value in request_data["text"] for value in "\r\n")
        )
        now, request_id = datetime.now(UTC).isoformat(), str(uuid.uuid4())
        event = {
            "specversion": "1.0",
            "id": request_id,
            "source": "urn:aelev:isolated-queue-qualification",
            "type": "com.aelev.translation-requested.v2",
            "time": now,
            "subject": "translation/" + request_id,
            "datacontenttype": "application/json",
            "dataschema": "https://schemas.aelev.dev/events/v1#/$defs/TranslationRequestedV2",
            "correlationid": request_id,
            "causationid": request_id,
            "traceparent": "00-"
            + uuid.uuid4().hex
            + "-"
            + secrets.token_hex(8)
            + "-01",
            "data": {
                "schema_version": "2.0.0",
                "recorded_at": now,
                "provenance": {
                    "owner_context": "ISOLATED_QUALIFICATION",
                    "producer_id": "queue-qualification",
                    "producer_version": "1.0.0",
                    "input_refs": [],
                },
                "request_id": request_id,
                "source_digest": "sha256:"
                + hashlib.sha256(request_data["text"].encode()).hexdigest(),
                **request_data,
            },
        }
        body = canonical(event)
        private_write(root / "request.bin", body)
        request_volume = harness.volume("request", {"request.bin": body})
        base_environment = {
            "AELEV_ENVIRONMENT": "local",
            "AELEV_TRANSLATOR_PRESENCE_INTERVAL_SECONDS": "1",
            "AELEV_TRANSLATOR_PRESENCE_TTL_SECONDS": "30",
            "AELEV_TRANSLATOR_DATABASE_HOST": SQL_HOST,
            "AELEV_TRANSLATOR_DATABASE_PORT": str(sql_port),
            "AELEV_TRANSLATOR_DATABASE_NAME": "queue_fixture",
            "AELEV_TRANSLATOR_DATABASE_USER": "queue-owner",
            "AELEV_TRANSLATOR_DATABASE_PASSWORD_FILE": (
                "/run/secrets/database/postgres-password"
            ),
            "AELEV_TRANSLATOR_DATABASE_SSLMODE": "verify-full",
            "AELEV_TRANSLATOR_DATABASE_CA_FILE": "ca.crt",
            "AELEV_TRANSLATOR_DATABASE_CLIENT_CERT_FILE": "client.crt",
            "AELEV_TRANSLATOR_DATABASE_CLIENT_KEY_FILE": "client.key",
            "AELEV_TRANSLATOR_DATABASE_SECRET_ROOT": "/run/secrets/database",
            "AELEV_KAFKA_BOOTSTRAP_SERVERS": BROKER_HOST + ":" + str(broker_port),
            "AELEV_KAFKA_SECURITY_PROTOCOL": "SSL",
            "AELEV_KAFKA_CA_FILE": "ca.crt",
            "AELEV_KAFKA_CLIENT_CERT_FILE": "client.crt",
            "AELEV_KAFKA_CLIENT_KEY_FILE": "client.key",
            "AELEV_TRANSLATOR_KAFKA_SECRET_ROOT": "/run/secrets/kafka",
            "AELEV_TRANSLATOR_PROVIDER_TRANSPORT": "mtls",
            "AELEV_TRANSLATOR_PROVIDER_URL": f"https://{PROVIDER_HOST}:{provider_port}",
            "AELEV_TRANSLATOR_PROVIDER_TLS_ROOT": TLS_ROOT,
            "AELEV_TRANSLATOR_HIGH_PRIORITY_BURST": "4",
            "QUEUE_GROUP_PREFIX": harness.scope,
            "QUEUE_REQUEST_TOPIC": record["request_topic"],
            "AELEV_TRANSLATOR_TOPIC_PARTITIONS": "1",
            "AELEV_TRANSLATOR_TOPIC_REPLICATION_FACTOR": "1",
            "AELEV_TRANSLATOR_TOPIC_RETENTION_MS": "3600000",
            "AELEV_TRANSLATOR_TOPIC_RETENTION_BYTES": "10485760",
            "AELEV_TRANSLATOR_TOPIC_MAX_MESSAGE_BYTES": "1048576",
            "AELEV_TRANSLATOR_TOPIC_MIN_INSYNC_REPLICAS": "1",
        }
        clients: dict[str, tuple[dict[str, str], list[str]]] = {}
        for role in ("admin", "probe", "normal", "priority", "outbox", "worker"):
            sql_crt, sql_key = sql_ca.leaf([], common_name="queue-owner")
            sql_volume = harness.volume(
                role + "-sql",
                {
                    "client.crt": sql_crt.read_bytes(),
                    "client.key": sql_key.read_bytes(),
                    "ca.crt": sql_ca.cert.read_bytes(),
                    "postgres-password": password.encode(),
                },
            )
            kafka_crt, kafka_key = broker_ca.leaf([], common_name="queue-" + role)
            kafka_volume = harness.volume(
                role + "-kafka",
                {
                    "client.crt": kafka_crt.read_bytes(),
                    "client.key": kafka_key.read_bytes(),
                    "ca.crt": broker_ca.cert.read_bytes(),
                },
            )
            provider_client, provider_client_key = provider_ca.leaf(
                [CLIENT], common_name="queue-" + role
            )
            svid = harness.volume(
                role + "-provider",
                {
                    "identity.json": canonical(identity_policy),
                    "client.crt": provider_client.read_bytes(),
                    "client.key": provider_client_key.read_bytes(),
                    "ca.crt": provider_ca.cert.read_bytes(),
                },
            )
            environment = {
                **base_environment,
                "AELEV_TRANSLATOR_KAFKA_GROUP": harness.scope + "-" + role,
            }
            mounts = [
                "type=volume,src=" + sql_volume + ",dst=/run/secrets/database,readonly",
                "type=volume,src=" + kafka_volume + ",dst=/run/secrets/kafka,readonly",
                "type=volume,src=" + svid + ",dst=" + TLS_ROOT + ",readonly",
                "type=volume,src="
                + request_volume
                + ",dst=/run/queue-fixture,readonly",
            ]
            clients[role] = environment, mounts
        admin_environment, admin_mounts = clients["admin"]
        idle = ["python", "-c", "import time;time.sleep(900)"]
        admin = harness.container(
            "admin",
            images["core"],
            networks["probe"],
            environment=admin_environment,
            mounts=admin_mounts,
            arguments=idle,
        )
        deadline = time.monotonic() + 75
        while True:
            ready = harness.run(
                [
                    "exec",
                    admin,
                    "python",
                    "-c",
                    "from translator_service.runtime import kafka_configuration;"
                    "from confluent_kafka.admin import AdminClient;"
                    "AdminClient(kafka_configuration()).list_topics(timeout=5)",
                ],
                check=False,
                timeout=15,
            )
            if ready.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("TRANSLATOR_QUEUE_BROKER_NOT_READY")
            time.sleep(1)
        harness.run(
            [
                "exec",
                admin,
                "python",
                "-m",
                "alembic",
                "-c",
                "/opt/aelev/alembic.ini",
                "upgrade",
                "head",
            ]
        )
        harness.run(
            ["exec", admin, "python", "-m", "translator_service.topic_provisioner"]
        )
        harness.probe(admin, "acls")
        probe_environment, probe_mounts = clients["probe"]
        probe = harness.container(
            "probe",
            images["core"],
            networks["probe"],
            environment=probe_environment,
            mounts=probe_mounts,
            arguments=idle,
        )
        harness.run(
            ["exec", probe, "python", "-m", "translator_service.provider_healthcheck"]
        )
        for role in ("normal", "priority"):
            environment, mounts = clients[role]
            environment.update(
                AELEV_TRANSLATOR_REQUEST_TOPIC="translation.requests"
                + (".priority" if role == "priority" else "")
                + ".v1",
                AELEV_TRANSLATOR_REQUEST_PRIORITY="0" if role == "priority" else "100",
            )
            harness.container(
                role,
                images["core"],
                networks["consumer"],
                environment=environment,
                mounts=mounts,
                arguments=["python", "-m", "translator_service.runtime", "consumer"],
            )
        worker_environment, worker_mounts = clients["worker"]
        worker = harness.container(
            "worker",
            images["worker"],
            networks["worker"],
            environment=worker_environment,
            mounts=worker_mounts,
        )
        record["inference_requested"] = args.live
        harness.probe(probe, "send")
        deadline = time.monotonic() + 260
        while True:
            state = harness.probe(probe, "bound")
            if state.get("state") == "SUCCEEDED":
                assert (
                    state["attempt_count"] == 1
                    and state["segment_count"] == 1
                    and state["outbox_state"] == "PENDING"
                )
                break
            if state.get("attempt_count", 0) >= 1 and state.get("state") in {
                "PENDING",
                "FAILED",
            }:
                raise RuntimeError("TRANSLATOR_QUEUE_FIRST_ATTEMPT_FAILED_NO_RETRY")
            if time.monotonic() >= deadline:
                raise RuntimeError("TRANSLATOR_QUEUE_EXECUTION_NOT_CONFIRMED")
            time.sleep(0.5)
        record["completed_state_before_outbox"] = state
        crash_environment, crash_mounts = clients["outbox"]
        crash = harness.container(
            "crash-outbox",
            images["core"],
            networks["outbox"],
            environment=crash_environment,
            mounts=crash_mounts,
            arguments=idle,
        )
        crashed = harness.run(
            ["exec", "--interactive", crash, "python", "-", "crash-outbox"],
            data=(SOURCE / "test/network_queue_probe.py").read_text(),
            check=False,
            timeout=40,
        )
        assert crashed.returncode == 73
        first = harness.probe(probe, "receive-1")
        assert harness.probe(probe, "state")["outbox_state"] == "PENDING"
        private_write(root / "result-first.bin", base64.b64decode(first["records"][0]))
        outbox = harness.container(
            "outbox",
            images["core"],
            networks["outbox"],
            environment=crash_environment,
            mounts=crash_mounts,
            arguments=["python", "-m", "translator_service.runtime", "outbox"],
        )
        harness.probe(probe, "receive-2")
        deadline = time.monotonic() + 20
        while harness.probe(probe, "state")["outbox_state"] != "PUBLISHED":
            assert time.monotonic() < deadline
            time.sleep(0.5)
        harness.probe(probe, "send")
        replay = harness.probe(probe, "receive-3")
        private_write(
            root / "result-replayed.bin", base64.b64decode(replay["records"][-1])
        )
        assert first["sha256"] == replay["sha256"]
        state = harness.probe(probe, "state")
        assert (
            state["state"] == "SUCCEEDED"
            and state["attempt_count"] == 1
            and state["control"]["desired_state"] == "PAUSED"
        )
        if not args.live:
            assert harness.probe(probe, "provider-calls") == {"calls": 1}
        for name in (worker, outbox, broker, database, provider):
            value = harness.run(
                ["inspect", "--format", "{{.State.Running}} {{.RestartCount}}", name]
            ).stdout.strip()
            assert value == "true 0"
        assert test_hashes == {
            name: hashlib.sha256((SOURCE / "test" / name).read_bytes()).hexdigest()
            for name in test_sources
        }
        record.update(
            status="PASS",
            kafka_tls_and_runtime_acls=True,
            postgres_verify_full_and_client_identity=True,
            provider_worker_shared_volume=False,
            published_endpoints_used=True,
            control_paused_after_first_test_lease=True,
            compute_attempts=1,
            checkpoint_segments=1,
            result_wire_records=3,
            result_bytes_preserved=True,
            crash_after_broker_ack_exit=73,
            post_crash_result_republished=True,
            duplicate_input_did_not_recompute=True,
            real_translation_quality_reviewed=False,
        )
    except (
        OSError,
        ValueError,
        AssertionError,
        RuntimeError,
        subprocess.SubprocessError,
    ):
        record["blocking_code"] = "TRANSLATOR_QUEUE_QUALIFICATION_FAILED"
        raise
    finally:
        for name in harness.names:
            harness.run(["logs", "--tail", "100", name], check=False)
        try:
            harness.cleanup()
        except Exception:
            record["cleanup_preserved"] = False
            record["status"] = "FAIL"
            raise
        else:
            record["cleanup_preserved"] = True
            record["baseline_persistent_containers"] = len(harness.baseline)
        finally:
            private_write(root / "verification.json", canonical(record))
            print(
                json.dumps(
                    {
                        "status": record["status"],
                        "evidence": str(root),
                        "inference_requested": record["inference_requested"],
                        "cleanup_preserved": record.get("cleanup_preserved"),
                    }
                ),
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
