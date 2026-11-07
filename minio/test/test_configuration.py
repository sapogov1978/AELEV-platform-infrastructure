"""Service-owned configuration checks for the MinIO delivery unit."""

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


def _resolved_compose() -> dict[str, object]:
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
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


class MinioConfigurationTest(unittest.TestCase):
    def test_operator_entrypoints_are_owned_by_the_component(self) -> None:
        for name in ("compose.yml", ".env.example", "README.md", "Dockerfile"):
            self.assertTrue((COMPONENT_ROOT / name).is_file(), name)

    def test_environment_has_one_documented_service_owned_namespace(self) -> None:
        compose = COMPOSE_PATH.read_text(encoding="utf-8")
        manual = (COMPONENT_ROOT / "USER_MANUAL.md").read_text(encoding="utf-8")
        variables = _declared_variables()
        self.assertTrue(variables)
        for variable in variables:
            self.assertTrue(
                variable.startswith("AELEV_INFRASTRUCTURE_MINIO_"), variable
            )
            self.assertIn(f"${{{variable}:?", compose, variable)
            self.assertIn(f"| `{variable}` |", manual, variable)
        self.assertNotIn("AELEV_INFRASTRUCTURE_MINIO_CONFIG_ROOT", compose)
        self.assertNotIn("AELEV_INFRASTRUCTURE_OBJECT_STORAGE_", compose)
        self.assertNotIn("object-storage", compose)

    def test_local_certificates_do_not_embed_consumer_names(self) -> None:
        generator = (COMPONENT_ROOT / "generate-local-configuration.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("zone-minio", generator)

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
        self.assertIn("AELEV_INFRASTRUCTURE_MINIO_COMPOSE_PROJECT", result.stderr)
        self.assertIn("MinIO .env file", result.stderr)

    def test_two_replicated_four_node_tls_clusters_are_required(self) -> None:
        compose = COMPOSE_PATH.read_text(encoding="utf-8")
        dockerfile = (COMPONENT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        healthcheck = (COMPONENT_ROOT / "healthcheck.sh").read_text(encoding="utf-8")
        provisioner = (COMPONENT_ROOT / "provision-bucket.sh").read_text(
            encoding="utf-8"
        )
        replication = (COMPONENT_ROOT / "configure-site-replication.sh").read_text(
            encoding="utf-8"
        )
        generator = (COMPONENT_ROOT / "generate-local-configuration.sh").read_text(
            encoding="utf-8"
        )

        for site in ("a", "b"):
            for node in range(1, 5):
                self.assertRegex(compose, rf"(?m)^  minio-site-{site}-{node}:")
                prefix = f"AELEV_INFRASTRUCTURE_MINIO_SITE_{site.upper()}_NODE_{node}"
                self.assertIn(f"{prefix}_ENDPOINT", compose)
                self.assertIn(f"{prefix}_VOLUME", compose)
        services = compose.split("services:\n", 1)[1].split("\nvolumes:\n", 1)[0]
        self.assertIsNone(re.search(r"(?m)^  minio:$", services))
        self.assertIn(
            "MINIO_STORAGE_CLASS_STANDARD: ${AELEV_INFRASTRUCTURE_MINIO_PARITY:?",
            compose,
        )
        self.assertIn("MINIO_ROOT_USER_FILE: /run/secrets/minio-root-user", compose)
        self.assertIn(
            "MINIO_ROOT_PASSWORD_FILE: /run/secrets/minio-root-password", compose
        )
        self.assertNotIn("MINIO_ROOT_USER:", compose)
        self.assertNotIn("MINIO_ROOT_PASSWORD:", compose)
        self.assertIn('MINIO_BROWSER: "off"', compose)
        self.assertIn('test: ["CMD", "minio-healthcheck"]', compose)
        self.assertIn("exec mc ready healthcheck", healthcheck)
        self.assertIn("github.com/minio/minio@${MINIO_COMMIT}", dockerfile)
        self.assertIn("github.com/minio/mc@${MC_COMMIT}", dockerfile)
        self.assertFalse((COMPONENT_ROOT / "healthcheck.go").exists())
        self.assertNotIn("http://minio:9000", compose)
        self.assertIn("minio-ingress: {}", compose)
        self.assertIn(
            'cp "$ca_file" "$MC_CONFIG_DIR/certs/CAs/aelev-minio-ca.crt"',
            provisioner,
        )
        self.assertIn("mc admin replicate add site-a site-b", replication)
        self.assertIn("mc admin replicate status site-a", replication)
        for site in ("a", "b"):
            local_ca = f'$secrets_dir/site-{site}/CAs/aelev-minio-sites.crt'
            self.assertIn(f'cp "$secrets_dir/sites-ca.crt" "{local_ca}"', generator)
            self.assertIn(f'"{local_ca}"', generator)
        self.assertRegex(
            compose,
            r"bucket-provisioner:[\s\S]*"
            r"site-replication-applier: \{condition: service_completed_successfully\}",
        )
        self.assertRegex(
            compose,
            r"site-replication-applier:[\s\S]*networks: \[minio, minio-ingress\]",
        )

        for source in (
            "./secrets/root-user",
            "./secrets/root-password",
            "./secrets/site-a",
            "./secrets/site-b",
            "./secrets/sites-ca.crt",
            "./configure-site-replication.sh",
            "./provision-bucket.sh",
        ):
            mount = compose.split(f"source: {source}", 1)
            self.assertEqual(len(mount), 2, source)
            self.assertRegex(
                mount[1].split("      - type: bind", 1)[0],
                r"create_host_path: false",
            )
        for name in (
            "generate-local-configuration.sh",
            "configure-site-replication.sh",
            "secrets/README.md",
            "test/multi-site-lifecycle.test.sh",
            "test/compose.test.yml",
        ):
            self.assertTrue((COMPONENT_ROOT / name).is_file(), name)

    def test_no_role_receives_an_unbounded_native_restart_budget(self) -> None:
        services = _resolved_compose()["services"]
        one_shot = {
            "site-replication-applier",
            "bucket-provisioner",
            "minio-status-provision",
        }
        self.assertTrue(one_shot.issubset(services))
        for name, service in services.items():
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
