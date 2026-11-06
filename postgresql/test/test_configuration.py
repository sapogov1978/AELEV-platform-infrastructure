"""Service-owned configuration checks for the PostgreSQL delivery unit."""

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


class PostgresqlConfigurationTest(unittest.TestCase):
    def test_operator_entrypoints_are_owned_by_the_component(self) -> None:
        for name in (
            "compose.yml",
            ".env.example",
            "README.md",
            "SPEC.md",
            "TASKS.md",
            "CHANGELOG.md",
            "Dockerfile",
            "poetry.lock",
        ):
            self.assertTrue((COMPONENT_ROOT / name).is_file(), name)

    def test_configuration_is_component_owned(self) -> None:
        compose = "\n".join(
            path.read_text() for path in COMPONENT_ROOT.glob("compose*.yml")
        )
        variables = [
            line.split("=", 1)[0]
            for line in ENV_PATH.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        self.assertTrue(variables)
        for variable in variables:
            self.assertTrue(
                variable.startswith("AELEV_INFRASTRUCTURE_POSTGRESQL_"), variable
            )
            self.assertIn(variable, compose, variable)

    def test_restart_policy_distinguishes_provisioning(self) -> None:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("AELEV_", "COMPOSE_", "VITE_"))
        }
        environment["AELEV_INFRASTRUCTURE_POSTGRESQL_ADMIN_PASSWORD"] = "test"
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ENV_PATH),
                "-f",
                str(COMPOSE_PATH),
                "--profile",
                "*",
                "config",
                "--format",
                "json",
            ],
            cwd=COMPONENT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        services = json.loads(result.stdout)["services"]
        for node in range(1, 4):
            self.assertEqual(services[f"postgresql-{node}"]["restart"], "no")
            self.assertEqual(services[f"etcd-{node}"]["restart"], "no")
        self.assertEqual(services["database-provisioner"]["restart"], "no")
        self.assertEqual(
            services["postgresql-status-publisher"]["restart"], "unless-stopped"
        )

    def test_publisher_update_can_preserve_remaining_restart_budget(self) -> None:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("AELEV_", "COMPOSE_", "VITE_"))
        }
        environment["AELEV_INFRASTRUCTURE_POSTGRESQL_UI_RESTART_POLICY"] = (
            "on-failure:2"
        )
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ENV_PATH),
                "-f",
                str(COMPOSE_PATH),
                "--profile",
                "*",
                "config",
                "--format",
                "json",
            ],
            cwd=COMPONENT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            json.loads(result.stdout)["services"]["postgresql-status-publisher"][
                "restart"
            ],
            "on-failure:2",
        )

    def test_compose_has_no_second_configuration_source(self) -> None:
        compose = "\n".join(
            path.read_text() for path in COMPONENT_ROOT.glob("compose*.yml")
        )
        self.assertNotRegex(compose, r"\$\{[^}]+:-")
        self.assertNotIn("POSTGRES_PASSWORD", compose)
        self.assertNotIn("AELEV_POSTGRESQL_TEST_WITHOUT_WATCHDOG", compose)

    def test_failure_domains_have_independent_data(self) -> None:
        compose = (COMPONENT_ROOT / "compose.runtime.yml").read_text()
        for node in range(1, 4):
            self.assertIn(f"postgresql-{node}-data:/var/lib/postgresql/data", compose)
            self.assertIn(f"etcd-{node}-data:/etcd-data", compose)
        self.assertNotIn("depends_on", compose)

    def test_all_three_fencing_recipes(self) -> None:
        for mode in ("docker", "kubernetes", "watchdog"):
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
                env={
                    **os.environ,
                    "AELEV_INFRASTRUCTURE_POSTGRESQL_FENCING_MODE": mode,
                    "AELEV_INFRASTRUCTURE_POSTGRESQL_WATCHDOG_DEVICE": "/dev/watchdog",
                },
                capture_output=True,
                text=True,
                check=True,
            )
            model = json.loads(result.stdout)["services"]
            for number in range(1, 4):
                row = model[f"postgresql-{number}"]
                self.assertEqual(row["cpu_shares"], 4096)
                self.assertEqual(model[f"etcd-{number}"]["cpu_shares"], 4096)
                self.assertEqual(bool(row.get("devices")), mode == "watchdog")
                self.assertEqual(
                    row["environment"]["AELEV_INFRASTRUCTURE_POSTGRESQL_FENCING_MODE"],
                    mode,
                )
                self.assertFalse(
                    any(
                        mount.get("target") == "/run/docker.sock"
                        for mount in row.get("volumes", [])
                    )
                )
            self.assertEqual("postgresql-fencer" in model, mode == "docker")
            if mode == "docker":
                executor = model["postgresql-fencer"]
                self.assertEqual(executor["cpu_shares"], 4096)
                self.assertEqual(
                    executor["healthcheck"]["test"],
                    ["CMD", "python", "-S", "-m", "aelev_postgresql.fencing_probe"],
                )

    def test_test_and_secrets_are_excluded_from_build(self) -> None:
        ignored = [
            pattern.rstrip("/")
            for pattern in (COMPONENT_ROOT / ".dockerignore").read_text().splitlines()
        ]
        self.assertIn("test", ignored)
        self.assertIn("secrets", ignored)

    def test_missing_required_value_is_an_error(self) -> None:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AELEV_")
        }
        values = dict(
            line.split("=", 1)
            for line in ENV_PATH.read_text().splitlines()
            if line and not line.startswith("#")
        )
        values["AELEV_INFRASTRUCTURE_POSTGRESQL_CLUSTER"] = ""
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ENV_PATH),
                "-f",
                str(COMPOSE_PATH),
                "config",
                "--quiet",
            ],
            env={**environment, **values},
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AELEV_INFRASTRUCTURE_POSTGRESQL_CLUSTER", result.stderr)

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


if __name__ == "__main__":
    unittest.main()
