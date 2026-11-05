"""Opt-in real broker lifecycle; delete only this invocation's labelled fixtures."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime
from typing import Any, cast


class Sandbox:
    def __init__(self, endpoint: str, image: str) -> None:
        if not endpoint.startswith("unix:///"):
            raise ValueError("LOCAL_DAEMON_REQUIRED")
        self.prefix = ["docker", "--host", endpoint]
        self.image = self.inspect("image", image)["Id"]
        self.identity = uuid.uuid4().hex
        self.name = "aelev-kafka-native-test-" + self.identity[:12]
        self.volume = self.name + "-data"
        self.labels = {"io.aelev.owner": "kafka", "io.aelev.test": self.identity}
        self.created_container = False
        self.created_volume = False

    def run(
        self, *args: str, payload: bytes | None = None, timeout: float = 90
    ) -> bytes:
        result = subprocess.run(
            [*self.prefix, *args],
            input=payload,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        if result.returncode:
            # Broker/client output can contain payloads; keep it private.
            raise RuntimeError("KAFKA_NATIVE_SANDBOX_COMMAND_FAILED:" + args[0])
        return result.stdout

    def inspect(self, kind: str, name: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            json.loads(self.run(kind, "inspect", "--format", "{{json .}}", name)),
        )

    def owned_container(self) -> dict[str, Any]:
        row = self.inspect("container", self.name)
        if (
            row["Image"] != self.image
            or row["Config"]["Labels"].get("io.aelev.test") != self.identity
        ):
            raise RuntimeError("KAFKA_NATIVE_SANDBOX_FOREIGN_CONTAINER")
        return row

    def prepare(self) -> None:
        labels = [
            item for k, v in self.labels.items() for item in ("--label", k + "=" + v)
        ]
        self.run("volume", "create", *labels, self.volume)
        self.created_volume = True
        env = {
            "KAFKA_NODE_ID": "1",
            "KAFKA_PROCESS_ROLES": "broker,controller",
            "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@localhost:9093",
            "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
            "KAFKA_LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
            "KAFKA_ADVERTISED_LISTENERS": "PLAINTEXT://localhost:9092",
            "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
            "KAFKA_INTER_BROKER_LISTENER_NAME": "PLAINTEXT",
            "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
            "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
            "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1",
            "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "0",
            "KAFKA_AUTO_CREATE_TOPICS_ENABLE": "false",
            "KAFKA_LOG_DIRS": "/var/lib/kafka/data",
            "KAFKA_HEAP_OPTS": "-Xms256m -Xmx512m",
        }
        environment = [item for k, v in env.items() for item in ("--env", k + "=" + v)]
        # Publish no port; the isolated broker is reachable only from its own exec.
        self.created_container = True
        self.run(
            "run",
            "--detach",
            "--name",
            self.name,
            "--hostname",
            "localhost",
            "--init",
            "--network",
            "none",
            "--restart",
            "no",
            "--memory",
            "896m",
            "--cpus",
            "1",
            "--pids-limit",
            "512",
            "--mount",
            "type=volume,source=" + self.volume + ",target=/var/lib/kafka/data",
            "--tmpfs",
            "/etc/kafka/secrets:uid=1000,gid=1000,mode=0755",
            "--tmpfs",
            "/mnt/shared/config:uid=1000,gid=1000,mode=0755",
            "--health-cmd",
            "/opt/aelev/kafka-health",
            "--health-interval",
            "10s",
            "--health-timeout",
            "5s",
            "--health-retries",
            "20",
            "--health-start-period",
            "20s",
            *labels,
            *environment,
            self.image,
        )

    def wait_healthy(self) -> dict[str, Any]:
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            row = self.owned_container()
            if row["State"]["Running"] is not True:
                # All env/payloads in this fixture are controlled and public;
                # retain a bounded native startup diagnostic before cleanup.
                diagnostic = {
                    "code": "KAFKA_NATIVE_SANDBOX_BROKER_EXITED",
                    "exit_code": row["State"]["ExitCode"],
                    "oom": row["State"]["OOMKilled"],
                    "startup_tail": self.run("logs", "--tail", "15", self.name).decode(
                        errors="replace"
                    )[-3000:],
                }
                sys.stderr.write(json.dumps(diagnostic) + "\n")
                raise RuntimeError("KAFKA_NATIVE_SANDBOX_BROKER_EXITED")
            if row["State"].get("Health", {}).get("Status") == "healthy":
                return row
            time.sleep(2)
        raise RuntimeError("KAFKA_NATIVE_SANDBOX_HEALTH_DEADLINE")

    def cli(self, command: str, *args: str, payload: bytes | None = None) -> bytes:
        self.owned_container()
        return self.run(
            "exec",
            "-i",
            self.name,
            "/opt/kafka/bin/" + command,
            "--bootstrap-server",
            "localhost:9092",
            *args,
            payload=payload,
            timeout=120,
        )

    def cleanup(self) -> None:
        if self.created_container:
            self.owned_container()
            self.run("stop", "--time", "30", self.name, timeout=50)
            self.owned_container()
            self.run("rm", self.name)
        if self.created_volume:
            row = self.inspect("volume", self.volume)
            if row.get("Labels") != self.labels:
                raise RuntimeError("KAFKA_NATIVE_SANDBOX_FOREIGN_VOLUME")
            self.run("volume", "rm", self.volume)

    def verify(self) -> dict[str, Any]:
        self.prepare()
        first = self.wait_healthy()
        for _ in range(8):
            self.run("exec", self.name, "/opt/aelev/kafka-health")
        topic = "aelev.infrastructure.kafka.native-probe-test"
        self.cli(
            "kafka-topics.sh",
            "--create",
            "--topic",
            topic,
            "--partitions",
            "1",
            "--replication-factor",
            "1",
            "--config",
            "retention.ms=-1",
            "--config",
            "retention.bytes=-1",
        )
        self.cli(
            "kafka-console-producer.sh", "--topic", topic, payload=b"before-restart\n"
        )
        self.run("stop", "--time", "30", self.name, timeout=50)
        stopped = self.owned_container()
        if (
            stopped["State"]["Running"]
            or stopped["State"]["ExitCode"] not in {0, 143}
            or stopped["State"]["OOMKilled"]
        ):
            raise RuntimeError("KAFKA_NATIVE_SANDBOX_UNCLEAN_STOP")
        self.run("start", self.name)
        second = self.wait_healthy()
        if second["Id"] != first["Id"] or second["Mounts"] != first["Mounts"]:
            raise RuntimeError("KAFKA_NATIVE_SANDBOX_DATA_MOUNT_CHANGED")
        self.cli(
            "kafka-console-producer.sh", "--topic", topic, payload=b"after-restart\n"
        )
        messages = self.cli(
            "kafka-console-consumer.sh",
            "--topic",
            topic,
            "--from-beginning",
            "--max-messages",
            "2",
            "--timeout-ms",
            "15000",
        )
        if messages.splitlines() != [b"before-restart", b"after-restart"]:
            raise RuntimeError("KAFKA_NATIVE_SANDBOX_MESSAGES_CHANGED")
        # Orphaned short children exercise the actual PID 1 reaper, not just an
        # assertion that Compose contains init:true. They belong to this fixture.
        self.run(
            "exec",
            self.name,
            "/bin/sh",
            "-c",
            "for i in 1 2 3 4 5 6 7 8; do (sleep 0.05 &) ; done",
        )
        for _ in range(8):
            self.run("exec", self.name, "/opt/aelev/kafka-health")
        states = self.run("exec", self.name, "ps", "-eo", "stat").decode().splitlines()
        zombies = sum(value.strip().startswith("Z") for value in states)
        if zombies:
            raise RuntimeError("KAFKA_NATIVE_SANDBOX_ZOMBIES_REMAIN")
        row = self.wait_healthy()
        durations = [
            (
                datetime.fromisoformat(value["End"])
                - datetime.fromisoformat(value["Start"])
            ).total_seconds()
            * 1000
            for value in row["State"]["Health"]["Log"]
            if value["ExitCode"] == 0
        ]
        return {
            "code": "KAFKA_NATIVE_HEALTH_LIFECYCLE_VERIFIED",
            "image_id": self.image,
            "protocol_probes": 16,
            "messages_preserved": 2,
            "zombies": zombies,
            "init": row["HostConfig"].get("Init"),
            "health_milliseconds": durations,
            "working_runtime_changed": False,
            "data_volume": self.volume,
            "test_container": self.name,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-host", required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    sandbox = Sandbox(args.docker_host, args.image)
    try:
        result = sandbox.verify()
    finally:
        sandbox.cleanup()
    result["test_resources_removed"] = True
    print(json.dumps(result))


if __name__ == "__main__":
    main()
