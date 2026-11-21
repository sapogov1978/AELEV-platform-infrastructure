"""Actual Compose CONFIG checks with an intentionally unavailable daemon socket."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
from typing import Any, cast
import unittest

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "AELEV_INFRASTRUCTURE_POSTGRESQL_"
ROLES = {
    "etcd-1",
    "etcd-2",
    "etcd-3",
    "postgresql-1",
    "postgresql-2",
    "postgresql-3",
    "postgresql-status-publisher",
    "postgresql-status-provision",
    "database-provisioner",
}
IPAM = {"NETWORK_SUBNET": "192.0.2.0/28", "NETWORK_GATEWAY": "192.0.2.1"}


def compose(
    recipes: list[str], values: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AELEV_", "COMPOSE_", "DOCKER_", "VITE_"))
    }
    environment.update({PREFIX + key: value for key, value in values.items()})
    environment["DOCKER_HOST"] = "unix:///nonexistent-aelev-ipam-config-test.sock"
    command = [
        "docker",
        "compose",
        "--env-file",
        str(ROOT / ".env.example"),
        "--profile",
        "*",
    ]
    for recipe in recipes:
        command += ["--file", str(ROOT / recipe)]
    return subprocess.run(
        [*command, "config", "--format", "json"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        timeout=30,
        check=False,
    )


class IpamProfileTest(unittest.TestCase):
    def model(self, recipes: list[str], values: dict[str, str]) -> dict[str, Any]:
        result = compose(recipes, values)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return cast(dict[str, Any], json.loads(result.stdout))

    def test_complete_models_differ_only_in_requested_network_ipam(self) -> None:
        for recipes in (
            ["compose.runtime.yml"],
            ["compose.yml"],
            ["compose.runtime.yml", "compose.docker.yml"],
        ):
            with self.subTest(recipes=recipes):
                before = self.model(recipes, {})
                after = self.model([*recipes, "compose.ipam.yml"], IPAM)
                expected = copy.deepcopy(before)
                expected["networks"]["postgresql"]["ipam"] = {
                    "config": [
                        {
                            "subnet": IPAM["NETWORK_SUBNET"],
                            "gateway": IPAM["NETWORK_GATEWAY"],
                        }
                    ]
                }
                self.assertEqual(after, expected)

    def test_unselected_profile_does_not_change_default_model_bytes(self) -> None:
        before = compose(["compose.yml"], {})
        after = compose(["compose.yml"], IPAM)
        self.assertEqual(before.returncode, 0)
        self.assertEqual(after.returncode, 0)
        self.assertEqual(before.stdout, after.stdout)

    def test_each_missing_or_empty_ipam_input_is_refused(self) -> None:
        for key in IPAM:
            for empty in (False, True):
                with self.subTest(key=key, empty=empty):
                    values = IPAM.copy()
                    if empty:
                        values[key] = ""
                    else:
                        del values[key]
                    result = compose(
                        ["compose.runtime.yml", "compose.ipam.yml"], values
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn((PREFIX + key).encode(), result.stderr)

    def test_every_owned_role_already_uses_the_original_network(self) -> None:
        before = self.model(["compose.runtime.yml"], {})
        after = self.model(["compose.runtime.yml", "compose.ipam.yml"], IPAM)
        self.assertTrue(ROLES <= set(before["services"]))
        self.assertEqual(set(before["services"]), set(after["services"]))
        for role in ROLES:
            with self.subTest(role=role):
                self.assertIn("postgresql", before["services"][role]["networks"])
                self.assertEqual(before["services"][role], after["services"][role])
        self.assertNotIn("postgresql-fencer", ROLES)

    def test_independent_projects_retain_their_explicit_network_names(self) -> None:
        networks = []
        for number in (1, 2):
            values = {
                **IPAM,
                "COMPOSE_PROJECT": f"ipam-test-{number}",
                "NETWORK": f"ipam-test-{number}-network",
            }
            baseline = self.model(["compose.runtime.yml"], values)
            model = self.model(["compose.runtime.yml", "compose.ipam.yml"], values)
            self.assertEqual(model["name"], values["COMPOSE_PROJECT"])
            self.assertEqual(model["networks"]["postgresql"]["name"], values["NETWORK"])
            self.assertEqual(model["services"], baseline["services"])
            networks.append(model["networks"]["postgresql"]["name"])
        self.assertNotEqual(*networks)


if __name__ == "__main__":
    unittest.main()
