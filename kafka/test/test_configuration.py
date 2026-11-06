"""Service-owned configuration checks for the Kafka delivery unit."""

from __future__ import annotations

import json
import os
import re
import subprocess
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
        compose = COMPOSE_PATH.read_text(encoding="utf-8")
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
