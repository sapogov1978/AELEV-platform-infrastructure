"""Check capability dependency, ownership fencing and build/start separation."""

from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "provider",
    Path(__file__).resolve().parents[1] / "services/infrastructure/bootstrap.py",
)
assert SPEC is not None and SPEC.loader is not None
provider = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provider)


class BootstrapTest(unittest.TestCase):
    def test_platform_adds_identity_but_kafka_does_not(self) -> None:
        self.assertIn("keycloak", provider.selected(["platform-ui"]))
        self.assertEqual(provider.selected(["kafka"]), ("kafka",))
        self.assertFalse(
            any(
                "translator" in role or "worker" in role
                for role in provider.selected(list(provider.CAPABILITIES))
            )
        )

    def test_build_never_starts_a_container(self) -> None:
        with (
            mock.patch.object(provider, "initialize"),
            mock.patch.object(provider, "verify_scope"),
            mock.patch.object(provider, "compose") as compose,
        ):
            provider.execute("build", ["kafka"])
        self.assertEqual([call.args[1] for call in compose.call_args_list], ["build"])

    def test_foreign_installation_blocks_before_start(self) -> None:
        with (
            mock.patch.object(
                provider,
                "read_env",
                return_value={"AELEV_INFRASTRUCTURE_COMPOSE_PROJECT": "example"},
            ),
            mock.patch.object(
                provider,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout="/different/installation\n"
                ),
            ),
        ):
            with self.assertRaises(provider.QuickstartError):
                provider.verify_scope()

    def test_kafka_start_does_not_launch_other_capabilities(self) -> None:
        with (
            mock.patch.object(provider, "initialize"),
            mock.patch.object(provider, "verify_scope"),
            mock.patch.object(provider, "compose") as compose,
        ):
            provider.execute("up", ["kafka"], no_build=True)
        self.assertEqual(
            compose.call_args.args[1:], ("up", "-d", "--no-build", "kafka")
        )


if __name__ == "__main__":
    unittest.main()
