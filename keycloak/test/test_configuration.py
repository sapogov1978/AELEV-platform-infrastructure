"""Service-owned configuration checks for the Keycloak delivery unit."""

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


def _resolved_compose(*profiles: str) -> dict[str, object]:
    environment = _clean_environment()
    command = [
        "docker",
        "compose",
        "--env-file",
        str(ENV_PATH),
        "-f",
        str(COMPOSE_PATH),
    ]
    for profile in profiles:
        command.extend(("--profile", profile))
    command.extend(("config", "--format", "json"))
    result = subprocess.run(
        command,
        cwd=COMPONENT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


class KeycloakConfigurationTest(unittest.TestCase):
    def test_operator_entrypoints_are_owned_by_the_component(self) -> None:
        for name in (
            "compose.yml",
            ".env.example",
            "README.md",
            "Dockerfile",
            "keycloak-entrypoint.sh",
        ):
            self.assertTrue((COMPONENT_ROOT / name).is_file(), name)

    def test_environment_has_one_service_owned_namespace(self) -> None:
        compose = COMPOSE_PATH.read_text(encoding="utf-8")
        variables = _declared_variables()
        self.assertTrue(variables)
        for variable in variables:
            self.assertTrue(
                variable.startswith("AELEV_INFRASTRUCTURE_KEYCLOAK_"), variable
            )
            self.assertIn(f"${{{variable}:?", compose, variable)
        self.assertNotRegex(compose, r"\$\{AELEV_INFRASTRUCTURE_KEYCLOAK_[A-Z0-9_]+:-")

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
        self.assertIn("AELEV_INFRASTRUCTURE_KEYCLOAK_COMPOSE_PROJECT", result.stderr)
        self.assertIn("Keycloak .env file", result.stderr)

    def test_production_mode_owns_its_postgresql(self) -> None:
        compose = COMPOSE_PATH.read_text(encoding="utf-8")
        dockerfile = (COMPONENT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('command: ["start", "--optimized", "--import-realm"]', compose)
        self.assertNotIn("start-dev", compose)
        self.assertNotIn("dev-file", compose)
        self.assertIn("keycloak-database:", compose)
        self.assertIn("KC_DB: postgres", compose)
        self.assertIn("jdbc:postgresql://keycloak-database:5432/", compose)
        self.assertIn('KC_HTTP_ENABLED: "false"', compose)
        self.assertIn("KC_HTTPS_CERTIFICATE_FILE:", compose)
        self.assertIn("KC_HTTPS_CERTIFICATE_KEY_FILE:", compose)
        self.assertIn('KC_HEALTH_ENABLED: "true"', compose)
        self.assertIn('KC_METRICS_ENABLED: "true"', compose)
        self.assertNotIn("KC_HTTP_MANAGEMENT_SCHEME", compose)
        self.assertIn("/dev/tcp/127.0.0.1/8443", compose)
        self.assertNotIn("configuration-applier:", compose)
        self.assertNotIn("/configuration/apply.sh", compose)
        self.assertNotIn("JAVA_OPTS_APPEND:", compose)
        self.assertNotIn("KC_HOSTNAME_BACKCHANNEL_DYNAMIC", compose)
        self.assertNotIn("/Users/", compose)
        database_section = compose.split("\n  keycloak:\n", 1)[0]
        self.assertNotIn("\n    ports:", database_section)
        self.assertNotIn("./secrets/tls.crt:/opt/keycloak/conf/tls/tls.crt:ro", compose)
        self.assertNotIn(
            "./secrets/admin-truststore.p12:/configuration/truststore.p12:ro",
            compose,
        )
        self.assertNotIn("AELEV_INFRASTRUCTURE_KEYCLOAK_CONFIGURATION_", compose)
        self.assertIn("AELEV_INFRASTRUCTURE_KEYCLOAK_NETWORK_SUBNET", compose)
        self.assertIn("AELEV_INFRASTRUCTURE_KEYCLOAK_INGRESS_NETWORK_SUBNET", compose)
        self.assertEqual(compose.count("ipam:"), 2)
        self.assertRegex(
            dockerfile, r"FROM quay\.io/keycloak/keycloak:26\.3\.2 AS builder"
        )
        self.assertIn("/opt/keycloak/bin/kc.sh build", dockerfile)
        self.assertIn("aelev-entrypoint.sh", dockerfile)

        model = _resolved_compose("*")
        services = model["services"]
        keycloak_environment = services["keycloak"]["environment"]
        database_environment = services["keycloak-database"]["environment"]
        provision_environment = services["keycloak-status-provision"]["environment"]
        self.assertNotIn("KC_BOOTSTRAP_ADMIN_PASSWORD", keycloak_environment)
        self.assertNotIn("KC_DB_PASSWORD", keycloak_environment)
        self.assertNotIn("POSTGRES_PASSWORD", database_environment)
        self.assertNotIn(
            "AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_PASSWORD",
            provision_environment,
        )
        self.assertEqual(
            keycloak_environment["AELEV_KEYCLOAK_ADMIN_PASSWORD_FILE"],
            "/run/secrets/keycloak-admin-password",
        )
        self.assertEqual(
            keycloak_environment["AELEV_KEYCLOAK_DATABASE_PASSWORD_FILE"],
            "/run/secrets/keycloak-database-password",
        )
        self.assertEqual(
            database_environment["POSTGRES_PASSWORD_FILE"],
            "/run/secrets/keycloak-database-password",
        )
        self.assertEqual(
            provision_environment["AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_PASSWORD_FILE"],
            "/run/secrets/keycloak-admin-password",
        )
        self.assertEqual(
            keycloak_environment["KC_HTTPS_CERTIFICATE_FILE"],
            "/run/secrets/keycloak-https/tls.crt",
        )
        self.assertEqual(
            keycloak_environment["KC_HTTPS_CERTIFICATE_KEY_FILE"],
            "/run/secrets/keycloak-https/tls.key",
        )

        for source in (
            "./secrets/https",
            "./realm.json",
        ):
            mount = compose.split(f"source: {source}", 1)
            self.assertEqual(len(mount), 2, source)
            self.assertRegex(
                mount[1].split("      - type: bind", 1)[0],
                r"create_host_path: false",
            )

    def test_provisioning_is_profile_only_and_restart_policy_is_explicit(self) -> None:
        normal_services = _resolved_compose()["services"]
        all_services = _resolved_compose("*")["services"]
        self.assertNotIn("configuration-applier", normal_services)
        self.assertNotIn("configuration-applier", all_services)
        self.assertNotIn("keycloak-status-provision", normal_services)
        self.assertIn("keycloak-status-provision", all_services)
        for name, service in all_services.items():
            self.assertEqual(service.get("restart"), "no", name)

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
