"""Opt-in native mTLS/ACL/restart/restore proof through pinned owner procedures.

Run only against a disposable Engine with no containers or volumes. Source and
private native output paths must also be visible at the same path to the daemon.
This component test grants no release, site, physical-distribution or HA admission.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import time
from typing import Any
import uuid

SOURCE: Path
OUT: Path
IMAGE: str
PUBLISHER_IMAGE: str
ENGINE: str
SCOPE: str
FILES: dict[str, str]
BEFORE: dict[str, set[str]] | None = None
NODE: Any
REPORT: dict[str, Any]
PREFIX = "AELEV_INFRASTRUCTURE_KAFKA_"
TOPIC = "aelev.infrastructure.kafka.network-proof"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(value)
    path.chmod(0o600)


def save(path: Path, value: Any) -> None:
    write(path, json.dumps(value, indent=2) + "\n")


def run(
    *args: str, payload: str | None = None, check: bool = True, timeout: int = 90
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args, input=payload, capture_output=True, text=True, timeout=timeout
    )
    with (OUT / "commands.private.jsonl").open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "argv": args,
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
            + "\n"
        )
    if check and result.returncode:
        raise RuntimeError(
            "KAFKA_NETWORK_COMMAND_FAILED:" + args[0] + ":" + str(result.returncode)
        )
    return result


def docker(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return run("docker", *args, **kwargs)


def inventory() -> dict[str, set[str]]:
    return {
        name: set(docker(*args).stdout.split())
        for name, args in {
            "containers": ("ps", "-aq", "--no-trunc"),
            "volumes": ("volume", "ls", "-q"),
            "networks": ("network", "ls", "-q", "--no-trunc"),
            "images": ("image", "ls", "-aq", "--no-trunc"),
        }.items()
    }


def owner_call(
    procedure: str, action: str, value: dict[str, Any], operation: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    operation = operation or str(uuid.uuid4())
    definition = OUT / "requests" / (operation + ".json")
    save(definition, value)
    request = {
        "schema_version": 1,
        "operation": operation,
        "source_root": str(SOURCE),
        "source_files": FILES,
        "state_root": str(OUT / "journal"),
        "procedure": procedure,
        "arguments": [
            action,
            "--request",
            str(definition),
            "--request-sha256",
            sha(definition),
        ],
        "inputs": {**value["inputs"], str(definition): sha(definition)},
        "environment": {
            "DOCKER_HOST": os.environ["DOCKER_HOST"],
            "DOCKER_CONFIG": os.environ["DOCKER_CONFIG"],
        },
        "timeout_seconds": 120,
    }
    receipt = NODE.invoke({"kind": "local"}, request, "execute")
    save(OUT / "receipts" / (operation + ".json"), receipt)
    assert receipt["status"] == "COMMAND_SUCCEEDED", "OWNER_PROCEDURE_NOT_CONFIRMED"
    return receipt["owner_result"], request


def issuer(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True)
    run(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "2",
        "-subj",
        "/CN=native-network-test-ca",
        "-keyout",
        str(directory / "ca.key"),
        "-out",
        str(directory / "ca.crt"),
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )


def identity(name: str, authority: Path, gateway: str | None = None) -> Path:
    directory = OUT / "identities" / name
    directory.mkdir(mode=0o700, parents=True)
    run(
        "openssl",
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        "/CN=network-" + name,
        "-keyout",
        str(directory / "key.pem"),
        "-out",
        str(directory / "request.csr"),
    )
    extensions = (
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage="
        + ("serverAuth,clientAuth" if gateway else "clientAuth")
        + "\n"
    )
    extensions += (
        "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always,issuer\n"
    )
    if gateway:
        extensions += "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:" + gateway + "\n"
    write(directory / "extensions", extensions)
    run(
        "openssl",
        "x509",
        "-req",
        "-in",
        str(directory / "request.csr"),
        "-CA",
        str(authority / "ca.crt"),
        "-CAkey",
        str(authority / "ca.key"),
        "-set_serial",
        str(secrets.randbits(120)),
        "-days",
        "2",
        "-extfile",
        str(directory / "extensions"),
        "-out",
        str(directory / "certificate.pem"),
    )
    shutil.copyfile(authority / "ca.crt", directory / "ca.crt")
    write(
        directory / "identity.pem",
        (directory / "key.pem").read_text()
        + (directory / "certificate.pem").read_text(),
    )
    write(
        directory / "client.properties",
        "security.protocol=SSL\nssl.keystore.type=PEM\nssl.keystore.location=/run/client/identity.pem\nssl.truststore.type=PEM\nssl.truststore.location=/run/client/ca.crt\nssl.endpoint.identification.algorithm=HTTPS\nrequest.timeout.ms=5000\ndefault.api.timeout.ms=8000\nmax.block.ms=8000\ndelivery.timeout.ms=10000\nretries=0\n",
    )
    for path in directory.iterdir():
        path.chmod(0o640)
    directory.chmod(0o750)
    return directory


def prepare(name: str, values: dict[str, str], broker: Path) -> dict[str, Any]:
    environment = OUT / "inputs" / name / "environment"
    write(environment, "".join(k + "=" + v + "\n" for k, v in values.items()))
    workspace = OUT / "workspaces" / name
    workspace.parent.mkdir(mode=0o700, exist_ok=True)
    common = {
        "schema_version": 1,
        "executor_root": str(SOURCE),
        "executor_files": FILES,
        "owner_root": str(SOURCE),
        "owner_files": FILES,
        "catalog": "deployment-catalog.json",
        "role": "platform-infrastructure/kafka",
    }
    definition = {
        **common,
        "workspace": str(workspace),
        "env_file": str(environment),
        "inputs": {
            str(environment): sha(environment),
            str(broker / "identity.pem"): sha(broker / "identity.pem"),
            str(broker / "ca.crt"): sha(broker / "ca.crt"),
        },
        "private_files": {
            "secrets/broker/broker.pem": {
                "source": str(broker / "identity.pem"),
                "mode": 0o640,
            },
            "secrets/broker/client-ca.crt": {
                "source": str(broker / "ca.crt"),
                "mode": 0o640,
            },
        },
    }
    # Private source identity files must be operator-only; the prepared target is
    # root-owned 0640 and readable by the broker's declared supplementary group.
    for filename in (broker / "identity.pem", broker / "ca.crt"):
        filename.chmod(0o600)
    prepared, _ = owner_call("owner-prepare", "prepare", definition)
    assert prepared["status"] == "OWNER_CONFIGURATION_PREPARED"
    return {
        **common,
        "recipes": ["kafka/compose.yml", "kafka/compose.network.yml"],
        "project_directory": str(workspace),
        "project": values[PREFIX + "COMPOSE_PROJECT"],
        "env_file": str(workspace / ".env"),
        "inputs": {str(workspace / ".env"): sha(environment)},
        "image": IMAGE,
        "configuration_sha256": None,
    }


def start(value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    planned, _ = owner_call("owner-compose", "plan", value)
    value["configuration_sha256"] = planned["configuration_sha256"]
    result, request = owner_call("owner-compose", "start", value)
    assert result["status"] == "OWNER_ROLE_RUNNING"
    identifier = result["container_id"]
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        row = json.loads(docker("inspect", identifier).stdout)[0]
        assert (
            row["Image"] == IMAGE
            and row["Config"]["Labels"]["com.docker.compose.project"]
            == value["project"]
        )
        if not row["State"]["Running"]:
            write(OUT / "broker-failure.private.log", docker("logs", identifier).stdout)
            raise RuntimeError("BROKER_EXITED")
        if row["State"].get("Health", {}).get("Status") == "healthy":
            return identifier, request
        time.sleep(1)
    raise TimeoutError("BROKER_HEALTH_NOT_CONFIRMED")


def client(
    network: str,
    address: str,
    principal: Path,
    tool: str,
    *arguments: str,
    payload: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return docker(
        "run",
        "--rm",
        "--pull",
        "never",
        "--interactive",
        "--network",
        network,
        "--user",
        "1000:1000",
        "--group-add",
        "0",
        "--read-only",
        "--tmpfs",
        "/tmp:mode=1777",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--label",
        "io.aelev.qualification=" + SCOPE,
        "--env",
        "LOG_DIR=/tmp/kafka-logs",
        "--env",
        "KAFKA_HEAP_OPTS=-Xms32m -Xmx128m",
        "--mount",
        "type=bind,src=" + str(principal) + ",dst=/run/client,readonly",
        "--entrypoint",
        "/opt/kafka/bin/" + tool,
        IMAGE,
        "--bootstrap-server",
        address,
        *arguments,
        payload=payload,
        check=check,
    )


def tls_rejected(
    address: str, authority: Path, principal: Path | None, hostname: str, expected: str
) -> None:
    host, port = address.rsplit(":", 1)
    context = ssl.create_default_context(cafile=str(authority / "ca.crt"))
    if principal:
        context.load_cert_chain(
            str(principal / "certificate.pem"), str(principal / "key.pem")
        )
    try:
        with socket.create_connection((host, int(port)), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=hostname) as secured:
                # ApiVersions v0, correlation 1, empty client id.
                secured.sendall(struct.pack(">ihhih", 10, 18, 0, 1, 0))
                assert not secured.recv(1), "UNAUTHORIZED_TLS_ACCEPTED"
    except (ssl.SSLError, ConnectionResetError) as error:
        if expected == "hostname":
            assert isinstance(error, ssl.SSLCertVerificationError)
        return
    if expected == "hostname":
        raise AssertionError("WRONG_HOSTNAME_ACCEPTED")


def volume_hash(name: str) -> str:
    result = docker(
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--user",
        "0:0",
        "--label",
        "io.aelev.qualification=" + SCOPE,
        "--mount",
        "type=volume,src=" + name + ",dst=/snapshot,readonly",
        "--entrypoint",
        "/bin/sh",
        IMAGE,
        "-c",
        "find /snapshot -type f -exec sha256sum '{}' '+' | sort",
    )
    return hashlib.sha256(result.stdout.encode()).hexdigest()


def publisher_probe(
    network: str, address: str, principal: Path, *, wrong_hostname: bool = False
) -> str:
    host, port = address.rsplit(":", 1)
    route = "wrong.example.invalid:" + port if wrong_hostname else address
    options = ["--add-host", "wrong.example.invalid:" + host] if wrong_hostname else []
    code = "import importlib.util,json;from pathlib import Path;from types import SimpleNamespace;spec=importlib.util.spec_from_file_location('owner','/opt/aelev/status_publisher.py');owner=importlib.util.module_from_spec(spec);spec.loader.exec_module(owner);print(json.dumps({'state':owner.probe(SimpleNamespace(timeout=5,secret_root=Path('/run/client')))[0]}))"
    result = docker(
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        network,
        "--user",
        "101:101",
        "--group-add",
        "0",
        "--read-only",
        "--tmpfs",
        "/tmp:mode=1777",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--label",
        "io.aelev.qualification=" + SCOPE,
        *options,
        "--env",
        PREFIX + "UI_BROKER_ENDPOINT=CLIENT://" + route,
        "--env",
        PREFIX + "UI_BROKER_PROTOCOL=SSL",
        "--mount",
        "type=bind,src=" + str(principal) + ",dst=/run/client,readonly",
        "--entrypoint",
        "python",
        PUBLISHER_IMAGE,
        "-c",
        code,
    )
    return json.loads(result.stdout)["state"]


def main() -> None:
    global BEFORE, FILES
    assert docker("info", "--format", "{{.ID}}").stdout.strip() == ENGINE
    BEFORE = inventory()
    assert (
        not BEFORE["containers"] and not BEFORE["volumes"]
    ), "EMPTY_DISPOSABLE_ENGINE_REQUIRED"
    FILES = {
        p.relative_to(SOURCE).as_posix(): sha(p)
        for p in SOURCE.rglob("*")
        if p.is_file()
    }
    metadata = json.loads(docker("image", "inspect", IMAGE).stdout)[0]
    assert (
        metadata["Architecture"]
        == {"aarch64": "arm64", "x86_64": "amd64"}[platform.machine()]
        and metadata["Os"] == "linux"
    )
    publisher_metadata = json.loads(docker("image", "inspect", PUBLISHER_IMAGE).stdout)[
        0
    ]
    assert (
        publisher_metadata["Architecture"] == metadata["Architecture"]
        and publisher_metadata["Config"]["User"] == "101:101"
    )
    network = SCOPE + "-clients"
    docker("network", "create", "--label", "io.aelev.qualification=" + SCOPE, network)
    gateway = json.loads(docker("network", "inspect", network).stdout)[0]["IPAM"][
        "Config"
    ][0]["Gateway"]
    authority = OUT / "issuer"
    issuer(authority)
    broker = identity("broker", authority, gateway)
    admin, runtime, foreign = [
        identity(name, authority) for name in ("admin", "client", "foreign")
    ]
    rogue_authority = OUT / "rogue-issuer"
    issuer(rogue_authority)
    rogue = identity("rogue", rogue_authority)
    publisher = identity("publisher", authority)
    for directory in (publisher, rogue):
        for source, destination in (
            ("ca.crt", "kafka-ca.crt"),
            ("certificate.pem", "kafka-client.crt"),
            ("key.pem", "kafka-client.key"),
        ):
            shutil.copyfile(directory / source, directory / destination)
            (directory / destination).chmod(0o640)
    # The rogue client trusts the real server; only its own issuer is untrusted.
    shutil.copyfile(authority / "ca.crt", rogue / "kafka-ca.crt")
    values = dict(
        line.split("=", 1)
        for line in (SOURCE / "kafka/.env.example").read_text().splitlines()
        if line and not line.startswith("#")
    )
    cluster = base64.urlsafe_b64encode(uuid.uuid4().bytes).rstrip(b"=").decode()
    address = gateway + ":19092"
    values.update(
        {
            PREFIX + "COMPOSE_PROJECT": SCOPE,
            PREFIX + "IMAGE": IMAGE,
            PREFIX + "NETWORK": SCOPE,
            PREFIX + "DATA_VOLUME": SCOPE + "-data",
            PREFIX + "DATA_VOLUME_EXTERNAL": "false",
            PREFIX + "BIND": gateway,
            PREFIX + "PORT": "19092",
            PREFIX + "ADVERTISED_LISTENERS": "CLIENT://" + address,
            PREFIX + "CLUSTER_ID": cluster,
            PREFIX + "TLS_SECRET_ROOT": "./secrets/broker",
            PREFIX + "TLS_SECRET_GID": "0",
            PREFIX + "TLS_SUPER_USERS": "User:CN=network-broker;User:CN=network-admin",
        }
    )
    value = prepare("broker", values, broker)
    container, request = start(value)
    print(json.dumps({"stage": "MTLS_BROKER_HEALTHY"}), flush=True)
    before_replay = {
        str(p): (sha(p), p.stat().st_mtime_ns)
        for p in (OUT / "journal").rglob("receipt.json")
    }
    receipt = NODE.invoke({"kind": "local"}, request, "execute")
    assert receipt["status"] == "COMMAND_SUCCEEDED" and before_replay == {
        str(p): (sha(p), p.stat().st_mtime_ns)
        for p in (OUT / "journal").rglob("receipt.json")
    }
    result, _ = owner_call("owner-compose", "status", value)
    assert result["health"] == "healthy"
    api = client(
        network,
        address,
        admin,
        "kafka-broker-api-versions.sh",
        "--command-config",
        "/run/client/client.properties",
    )
    assert address in api.stdout and "127.0.0.1:9094" not in api.stdout
    assert (
        publisher_probe(network, address, publisher) == "ONLINE"
    ), "PUBLISHER_MTLS_PROBE_NOT_ONLINE"
    assert (
        publisher_probe(network, address, publisher, wrong_hostname=True) == "UNKNOWN"
    ), "PUBLISHER_FALSE_ONLINE_FOR_WRONG_HOSTNAME"
    assert (
        publisher_probe(network, address, rogue) == "UNKNOWN"
    ), "PUBLISHER_FALSE_ONLINE_FOR_UNTRUSTED_CLIENT"
    client(
        network,
        address,
        admin,
        "kafka-topics.sh",
        "--command-config",
        "/run/client/client.properties",
        "--create",
        "--topic",
        TOPIC,
        "--partitions",
        "1",
        "--replication-factor",
        "1",
    )
    client(
        network,
        address,
        admin,
        "kafka-acls.sh",
        "--command-config",
        "/run/client/client.properties",
        "--add",
        "--allow-principal",
        "User:CN=network-client",
        "--operation",
        "Read",
        "--operation",
        "Write",
        "--operation",
        "Describe",
        "--topic",
        TOPIC,
    )
    client(
        network,
        address,
        admin,
        "kafka-acls.sh",
        "--command-config",
        "/run/client/client.properties",
        "--add",
        "--allow-principal",
        "User:CN=network-client",
        "--operation",
        "Read",
        "--group",
        "network-proof-",
        "--resource-pattern-type",
        "prefixed",
    )

    def produce(
        principal: Path, body: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return client(
            network,
            address,
            principal,
            "kafka-console-producer.sh",
            "--producer.config",
            "/run/client/client.properties",
            "--topic",
            TOPIC,
            "--sync",
            payload=body,
            check=check,
        )

    def consume(phase: str) -> None:
        result = client(
            network,
            address,
            runtime,
            "kafka-console-consumer.sh",
            "--consumer.config",
            "/run/client/client.properties",
            "--topic",
            TOPIC,
            "--group",
            "network-proof-" + phase,
            "--from-beginning",
            "--max-messages",
            "2",
            "--timeout-ms",
            "15000",
        )
        assert result.stdout.splitlines() == ["before-restart", "after-restart"]

    produce(runtime, "before-restart\n")
    denied = produce(foreign, "must-not-exist\n", check=False)
    assert (
        "TopicAuthorizationException" in denied.stdout + denied.stderr
    ), "FOREIGN_WRITE_NOT_REJECTED"
    denied = client(
        network,
        address,
        runtime,
        "kafka-topics.sh",
        "--command-config",
        "/run/client/client.properties",
        "--create",
        "--topic",
        TOPIC + ".forbidden",
        "--partitions",
        "1",
        "--replication-factor",
        "1",
        check=False,
    )
    assert (
        "AuthorizationException" in denied.stdout + denied.stderr
    ), "RUNTIME_CREATE_NOT_REJECTED"
    tls_rejected(address, authority, None, gateway, "anonymous")
    tls_rejected(address, authority, rogue, gateway, "rogue")
    tls_rejected(address, authority, runtime, "wrong.example.invalid", "hostname")
    # No anonymous grant is needed for the loopback metadata probe, and this
    # technical listener must not authorize a mutation from inside the broker.
    denied = docker(
        "exec",
        "--interactive",
        container,
        "/opt/kafka/bin/kafka-console-producer.sh",
        "--bootstrap-server",
        "127.0.0.1:9094",
        "--topic",
        TOPIC,
        "--sync",
        "--producer-property",
        "max.block.ms=5000",
        "--producer-property",
        "request.timeout.ms=3000",
        "--producer-property",
        "delivery.timeout.ms=6000",
        payload="must-not-exist\n",
        check=False,
    )
    assert (
        "TopicAuthorizationException" in denied.stdout + denied.stderr
    ), "HEALTH_LISTENER_ALLOWED_DATA_WRITE"
    print(json.dumps({"stage": "TLS_AND_ACL_NEGATIVES_PASSED"}), flush=True)
    docker("stop", "--time", "20", container)
    docker("start", container)
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        observed, _ = owner_call("owner-compose", "status", value)
        if observed.get("health") == "healthy":
            break
        time.sleep(1)
    else:
        raise TimeoutError("RESTART_NOT_HEALTHY")
    produce(runtime, "after-restart\n")
    consume("restart")
    docker("stop", "--time", "20", container)
    backup = SCOPE + "-backup"
    docker("volume", "create", "--label", "io.aelev.qualification=" + SCOPE, backup)
    data = values[PREFIX + "DATA_VOLUME"]
    docker(
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--user",
        "0:0",
        "--label",
        "io.aelev.qualification=" + SCOPE,
        "--mount",
        "type=volume,src=" + data + ",dst=/source,readonly",
        "--mount",
        "type=volume,src=" + backup + ",dst=/backup",
        "--entrypoint",
        "/bin/cp",
        IMAGE,
        "-a",
        "/source/.",
        "/backup/",
    )
    snapshot = volume_hash(data)
    assert snapshot == volume_hash(backup), "COLD_COPY_BYTES_CHANGED"
    # Native Formatter verifies existing cluster/node IDs before ignoring a
    # formatted store. Test the refusal on the copy, never on canonical data.
    config = OUT / "wrong-cluster.properties"
    write(
        config,
        "process.roles=broker,controller\nnode.id=1\ncontroller.listener.names=CONTROLLER\nlisteners=PLAINTEXT://127.0.0.1:9092,CONTROLLER://127.0.0.1:9093\nadvertised.listeners=PLAINTEXT://localhost:9092\nlistener.security.protocol.map=PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT\ninter.broker.listener.name=PLAINTEXT\ncontroller.quorum.voters=1@localhost:9093\nlog.dirs=/var/lib/kafka/data\n",
    )
    config.chmod(0o640)
    wrong = base64.urlsafe_b64encode(uuid.uuid4().bytes).rstrip(b"=").decode()
    denied = docker(
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--user",
        "1000:1000",
        "--group-add",
        "0",
        "--label",
        "io.aelev.qualification=" + SCOPE,
        "--mount",
        "type=volume,src=" + backup + ",dst=/var/lib/kafka/data",
        "--mount",
        "type=bind,src=" + str(config) + ",dst=/run/cluster.properties,readonly",
        "--entrypoint",
        "/opt/kafka/bin/kafka-storage.sh",
        IMAGE,
        "format",
        "--cluster-id",
        wrong,
        "--config",
        "/run/cluster.properties",
        "--ignore-formatted",
        check=False,
    )
    assert (
        denied.returncode
        and "cluster.id" in denied.stdout + denied.stderr
        and snapshot == volume_hash(backup)
    ), "WRONG_CLUSTER_NOT_REFUSED_WITHOUT_WRITES"
    restored = dict(values)
    restored.update(
        {
            PREFIX + "COMPOSE_PROJECT": SCOPE + "-restore",
            PREFIX + "NETWORK": SCOPE + "-restore",
            PREFIX + "DATA_VOLUME": backup,
            PREFIX + "DATA_VOLUME_EXTERNAL": "true",
        }
    )
    restored_value = prepare("restore", restored, broker)
    restored_container, _ = start(restored_value)
    assert restored_container != container
    consume("restored")
    denied = produce(foreign, "must-not-exist\n", check=False)
    assert (
        "TopicAuthorizationException" in denied.stdout + denied.stderr
    ), "RESTORE_LOST_ACL"
    REPORT.update(
        status="PASS",
        publisher_image=PUBLISHER_IMAGE,
        publisher_mtls_probe_verified=True,
        publisher_tls_failures_unknown=True,
        publisher_runtime_full_started=False,
        broker_image=IMAGE,
        native_architecture=metadata["Architecture"],
        owner_prepare_start_replay_status=True,
        advertised_endpoint_verified=True,
        authorized_produce_consume=True,
        foreign_write_rejected=True,
        runtime_create_rejected=True,
        anonymous_tls_rejected=True,
        unknown_issuer_rejected=True,
        wrong_hostname_rejected=True,
        health_listener_mutation_rejected=True,
        restart_retained_messages_and_acl=True,
        cold_copy_sha256=snapshot,
        wrong_cluster_refused_without_writes=True,
        restored_messages_and_acl=True,
        probe_source_sha256=sha(SOURCE / "kafka/health.c"),
        recipe_sha256={
            name: sha(SOURCE / "kafka" / name)
            for name in ("compose.yml", "compose.network.yml", "Dockerfile")
        },
    )


def cleanup() -> bool:
    if BEFORE is None:
        return True
    assert docker("info", "--format", "{{.ID}}").stdout.strip() == ENGINE
    current = inventory()
    owned_volumes: set[str] = set()
    removals: list[tuple[tuple[str, ...], str]] = []
    for kind, inspect, remove in (
        ("containers", ("inspect",), ("rm", "--force")),
        ("volumes", ("volume", "inspect"), ("volume", "rm")),
        ("networks", ("network", "inspect"), ("network", "rm")),
    ):
        for identifier in sorted(current[kind] - BEFORE[kind]):
            row = json.loads(docker(*inspect, identifier).stdout)[0]
            labels = (
                row.get("Config", {}).get("Labels")
                if kind == "containers"
                else row.get("Labels")
            ) or {}
            owned = labels.get("io.aelev.qualification") == SCOPE or labels.get(
                "com.docker.compose.project"
            ) in {SCOPE, SCOPE + "-restore"}
            assert (
                owned or kind == "volumes" and identifier in owned_volumes
            ), "FOREIGN_RESOURCE_PRESERVED"
            if kind == "containers":
                owned_volumes.update(
                    m["Name"]
                    for m in row.get("Mounts", [])
                    if m.get("Type") == "volume"
                )
            removals.append((remove, identifier))
    for remove, identifier in removals:
        docker(*remove, identifier)
    return inventory() == BEFORE


def cli() -> int:
    global SOURCE, OUT, IMAGE, PUBLISHER_IMAGE, ENGINE, SCOPE, NODE, REPORT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--publisher-image", required=True)
    parser.add_argument("--expected-engine-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    assert (
        os.getuid() == 0 and platform.python_version() == "3.13.7"
    ), "PINNED_NATIVE_ROOT_OPERATOR_REQUIRED"
    os.umask(0o077)
    SOURCE, OUT = arguments.source.resolve(), arguments.output.resolve()
    assert not OUT.is_relative_to(SOURCE) and not SOURCE.is_relative_to(OUT)
    IMAGE, ENGINE = arguments.image, arguments.expected_engine_id
    PUBLISHER_IMAGE = arguments.publisher_image
    assert IMAGE.startswith("sha256:") and len(IMAGE) == 71
    OUT.mkdir(mode=0o700)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SOURCE))
    from tools import node_execution

    NODE = node_execution
    SCOPE = "aelev-kafka-network-" + secrets.token_hex(4)
    REPORT = {
        "status": "FAIL",
        "scope": SCOPE,
        "qualifier_sha256": sha(Path(__file__)),
        "source_release_admission_exercised": False,
        "full_platform_e2e_verified": False,
        "physical_distribution_verified": False,
        "inference_requested": False,
        "queue_resume_issued": False,
    }
    started = time.monotonic()
    try:
        main()
    except BaseException as error:
        save(
            OUT / "failure.private.json",
            {"type": type(error).__name__, "reason": str(error)},
        )
        REPORT.update(status="FAIL", error_code="NETWORK_QUALIFICATION_FAILED")
    finally:
        try:
            REPORT["baseline_preserved"] = cleanup()
            assert REPORT["baseline_preserved"]
        except BaseException as error:
            save(
                OUT / "cleanup-failure.private.json",
                {"type": type(error).__name__, "reason": str(error)},
            )
            REPORT.update(status="FAIL", baseline_preserved=False)
        REPORT["seconds"] = round(time.monotonic() - started, 3)
        save(OUT / "result.json", REPORT)
    print(
        json.dumps(
            {
                "status": REPORT["status"],
                "baseline_preserved": REPORT["baseline_preserved"],
                "release_admission": False,
            }
        ),
        flush=True,
    )
    return 0 if REPORT["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(cli())
