"""Service-owned configuration checks for the Kafka delivery unit."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

COMPONENT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = COMPONENT_ROOT / "compose.yml"
ENV_PATH = COMPONENT_ROOT / ".env.example"


def _declared_variables() -> list[str]:
    return [
        line.split("=", 1)[0]
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]


def _clean_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AELEV_", "COMPOSE_", "VITE_"))
    }


class KafkaConfigurationTest(unittest.TestCase):
    def network_model(self, missing: str | None = None) -> dict:
        values = dict(
            line.split("=", 1)
            for line in ENV_PATH.read_text().splitlines()
            if line and not line.startswith("#")
        )
        prefix = "AELEV_INFRASTRUCTURE_KAFKA_"
        values.update(
            {
                prefix + "CLUSTER_ID": "MkU3OEVBNTcwNTJENDM2Qk",
                prefix + "TLS_SECRET_ROOT": "./secrets/broker",
                prefix + "TLS_SECRET_GID": "1000",
                prefix + "TLS_SUPER_USERS": "User:CN=kafka-broker;User:CN=kafka-admin",
                prefix
                + "ADVERTISED_LISTENERS": "CLIENT://broker.example.invalid:19092",
            }
        )
        if missing:
            values.pop(prefix + missing)
        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / "configuration"
            environment.write_text(
                "".join(k + "=" + v + "\n" for k, v in values.items())
            )
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    str(environment),
                    "--profile",
                    "*",
                    "-f",
                    str(COMPOSE_PATH),
                    "-f",
                    str(COMPONENT_ROOT / "compose.network.yml"),
                    "config",
                    "--format",
                    "json",
                ],
                cwd=COMPONENT_ROOT,
                env=_clean_environment(),
                capture_output=True,
                text=True,
                check=True,
            )
        return json.loads(result.stdout)

    def test_network_recipe_has_verified_mtls_and_default_deny_acl(self) -> None:
        broker = self.network_model()["services"]["kafka"]
        environment = broker["environment"]
        self.assertEqual(environment["KAFKA_SSL_CLIENT_AUTH"], "required")
        self.assertEqual(
            environment["KAFKA_AUTHORIZER_CLASS_NAME"],
            "org.apache.kafka.metadata.authorizer.StandardAuthorizer",
        )
        self.assertEqual(environment["KAFKA_ALLOW_EVERYONE_IF_NO_ACL_FOUND"], "false")
        self.assertEqual(
            environment["KAFKA_SSL_ENDPOINT_IDENTIFICATION_ALGORITHM"], "HTTPS"
        )
        self.assertEqual(
            environment["KAFKA_LISTENER_SECURITY_PROTOCOL_MAP"],
            "CLIENT:SSL,CONTROLLER:SSL,HEALTH:PLAINTEXT",
        )
        self.assertEqual(
            environment["KAFKA_LISTENERS"],
            "CLIENT://:9092,CONTROLLER://127.0.0.1:9093,HEALTH://127.0.0.1:9094",
        )
        self.assertEqual(
            environment["KAFKA_ADVERTISED_LISTENERS"],
            "CLIENT://broker.example.invalid:19092,HEALTH://127.0.0.1:9094",
        )
        self.assertEqual([v["target"] for v in broker["ports"]], [9092])
        self.assertEqual(
            broker["healthcheck"]["test"], ["CMD", "/opt/aelev/kafka-health", "9094"]
        )
        self.assertEqual(broker["restart"], "no")
        self.assertTrue(broker["volumes"][-1]["read_only"])
        self.assertFalse(broker["volumes"][-1]["bind"].get("create_host_path", False))
        self.assertIs(
            self.network_model()["networks"]["kafka"].get("internal", False), False
        )

    def test_network_required_inputs_have_no_implicit_fallback(self) -> None:
        for name in (
            "CLUSTER_ID",
            "TLS_SECRET_ROOT",
            "TLS_SECRET_GID",
            "TLS_SUPER_USERS",
        ):
            with self.subTest(name=name), self.assertRaises(
                subprocess.CalledProcessError
            ):
                self.network_model(missing=name)

    def test_network_recipe_preserves_unrelated_publisher_roles(self) -> None:
        network = self.network_model()
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ENV_PATH),
                "--profile",
                "*",
                "-f",
                str(COMPOSE_PATH),
                "config",
                "--format",
                "json",
            ],
            cwd=COMPONENT_ROOT,
            env=_clean_environment(),
            capture_output=True,
            text=True,
            check=True,
        )
        local = json.loads(result.stdout)
        for name in ("kafka-status-publisher", "kafka-status-provision"):
            if name == "kafka-status-publisher":
                self.assertEqual(
                    network["services"][name]["environment"].pop(
                        "AELEV_INFRASTRUCTURE_KAFKA_UI_BROKER_ENDPOINT"
                    ),
                    "CLIENT://broker.example.invalid:19092",
                )
                self.assertEqual(
                    network["services"][name]["environment"].pop(
                        "AELEV_INFRASTRUCTURE_KAFKA_UI_BROKER_PROTOCOL"
                    ),
                    "SSL",
                )
            self.assertEqual(network["services"][name], local["services"][name])

    def test_native_probe_is_direct_bounded_and_reaps_children(self) -> None:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ENV_PATH),
                "-f",
                str(COMPOSE_PATH),
                "config",
                "--format",
                "json",
            ],
            cwd=COMPONENT_ROOT,
            env=_clean_environment(),
            capture_output=True,
            text=True,
            check=True,
        )
        broker = json.loads(result.stdout)["services"]["kafka"]
        self.assertIs(broker["init"], True)
        self.assertEqual(broker["restart"], "no")
        self.assertEqual(
            broker["healthcheck"]["test"], ["CMD", "/opt/aelev/kafka-health"]
        )
        self.assertEqual(broker["healthcheck"]["timeout"], "5s")
        self.assertEqual(broker["healthcheck"]["retries"], 20)
        dockerfile = (COMPONENT_ROOT / "Dockerfile").read_text()
        self.assertIn("apache/kafka:3.9.1@sha256:", dockerfile)
        self.assertIn("-static", dockerfile)
        self.assertNotIn("RUN apk", dockerfile.split("AS runtime", 1)[1])

    def test_operator_entrypoints_are_owned_by_the_component(self) -> None:
        for name in ("compose.yml", ".env.example", "README.md"):
            self.assertTrue((COMPONENT_ROOT / name).is_file(), name)

    def test_environment_has_one_service_owned_namespace(self) -> None:
        compose = COMPOSE_PATH.read_text(encoding="utf-8") + (
            COMPONENT_ROOT / "compose.network.yml"
        ).read_text(encoding="utf-8")
        variables = _declared_variables()
        self.assertTrue(variables)
        for variable in variables:
            self.assertTrue(
                variable.startswith("AELEV_INFRASTRUCTURE_KAFKA_"), variable
            )
            self.assertIn(f"${{{variable}:?", compose, variable)
        self.assertNotIn("AELEV_INFRASTRUCTURE_KAFKA_REPLICA_FETCH_MAX_BYTES", compose)
        self.assertNotIn(
            "AELEV_INFRASTRUCTURE_KAFKA_REPLICA_FETCH_MAX_BYTES",
            ENV_PATH.read_text(encoding="utf-8"),
        )
        self.assertNotRegex(compose, r"\$\{AELEV_INFRASTRUCTURE_KAFKA_[A-Z0-9_]+:-")

    def test_missing_environment_fails_closed(self) -> None:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                os.devnull,
                "-f",
                str(COMPOSE_PATH),
                "config",
            ],
            cwd=COMPONENT_ROOT,
            env=_clean_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AELEV_INFRASTRUCTURE_KAFKA_COMPOSE_PROJECT", result.stderr)
        self.assertIn("Kafka .env file", result.stderr)

    def test_broker_does_not_own_application_topics(self) -> None:
        compose = COMPOSE_PATH.read_text(encoding="utf-8")
        combined = compose + ENV_PATH.read_text(encoding="utf-8")
        self.assertIn('KAFKA_AUTO_CREATE_TOPICS_ENABLE: "false"', compose)
        self.assertFalse((COMPONENT_ROOT / "provision-topics.sh").exists())
        for forbidden in (
            "topic-provisioner",
            "kafka-topics-init",
            "AELEV_INFRASTRUCTURE_KAFKA_CONFIG_ROOT",
            "AELEV_INFRASTRUCTURE_KAFKA_TOPICS",
            "AELEV_INFRASTRUCTURE_KAFKA_TOPIC_",
        ):
            self.assertNotIn(forbidden, combined, forbidden)

    def test_standalone_compose_contains_only_kafka(self) -> None:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ENV_PATH),
                "-f",
                str(COMPOSE_PATH),
                "config",
                "--services",
            ],
            cwd=COMPONENT_ROOT,
            env=_clean_environment(),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.splitlines(), ["kafka"])

    def test_component_contains_no_consumer_identities(self) -> None:
        consumer = re.compile(
            r"zone[-_ ]?ingestion|base[-_ ]?screening|crawler|"
            r"source[-_ ]?research|candidate[-_ ]?screening|translator",
            re.IGNORECASE,
        )
        for path in COMPONENT_ROOT.rglob("*"):
            if not path.is_file() or {"test", "obsolete", "secrets"} & set(path.parts):
                continue
            if path.name == ".env" or (
                path.suffix not in {".md", ".yml", ".yaml", ".json", ".sh"}
                and path.name not in {"Dockerfile", ".env.example"}
            ):
                continue
            self.assertNotRegex(path.read_text(encoding="utf-8"), consumer, str(path))

    def test_persistent_process_waits_for_recovery_budget_registration(self) -> None:
        compose = COMPOSE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("restart: unless-stopped", compose)
        self.assertNotIn("restart: always", compose)
        self.assertIn("profiles: [ui]", compose)
        self.assertIn("command: [provision]", compose)
        self.assertIn('restart: "no"', compose)


if __name__ == "__main__":
    unittest.main()
