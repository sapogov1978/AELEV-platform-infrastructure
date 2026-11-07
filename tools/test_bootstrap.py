"""Check capability dependency, ownership fencing and build/start separation."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "provider",
    Path(__file__).resolve().parents[1] / "bootstrap.py",
)
assert SPEC is not None and SPEC.loader is not None
provider = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provider)


class BootstrapTest(unittest.TestCase):
    def test_keycloak_creates_group_readable_password_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            keycloak = Path(temporary).resolve()
            (keycloak / ".env.example").write_text("")
            tls = keycloak / "secrets/https"
            tls.mkdir(parents=True)
            for name in ("ca.crt", "tls.crt", "tls.key"):
                (tls / name).write_text("fixture\n")

            with mock.patch.object(provider, "KEYCLOAK", keycloak):
                provider.initialize_keycloak()

            for name in ("admin-password", "database-password"):
                status = (keycloak / "secrets" / name).stat()
                self.assertEqual(status.st_mode & 0o777, 0o640)
                self.assertEqual(status.st_gid, os.getgid())

    def test_keycloak_preserves_existing_password_file_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            keycloak = Path(temporary).resolve()
            (keycloak / ".env.example").write_text("")
            secrets_root = keycloak / "secrets"
            secrets_root.mkdir()
            admin_password = secrets_root / "admin-password"
            admin_password.write_text("existing-value\n")
            admin_password.chmod(0o600)
            tls = secrets_root / "https"
            tls.mkdir()
            for name in ("ca.crt", "tls.crt", "tls.key"):
                (tls / name).write_text("fixture\n")

            with mock.patch.object(provider, "KEYCLOAK", keycloak):
                provider.initialize_keycloak()

            self.assertEqual(admin_password.read_text(), "existing-value\n")
            self.assertEqual(admin_password.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                (secrets_root / "database-password").stat().st_mode & 0o777,
                0o640,
            )

    def test_first_run_minio_configuration_is_owned_by_its_generator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            minio = root / "minio"
            minio.mkdir()
            (root / ".env.example").write_text("EXAMPLE=value\n")
            (minio / ".env.example").write_text("MINIO_EXAMPLE=value\n")

            def prepare_minio() -> None:
                self.assertFalse((minio / ".env").exists())
                (minio / ".env").write_text("MINIO_EXAMPLE=prepared\n")

            with (
                mock.patch.object(provider, "ROOT", root),
                mock.patch.object(provider, "MINIO", minio),
                mock.patch.object(provider.shutil, "which", return_value="available"),
                mock.patch.object(provider, "initialize_keycloak"),
                mock.patch.object(provider, "initialize_platform"),
                mock.patch.object(provider, "initialize_postgresql"),
                mock.patch.object(
                    provider, "initialize_minio", side_effect=prepare_minio
                ),
                mock.patch.object(provider, "compose"),
            ):
                provider.initialize()
            self.assertEqual((minio / ".env").read_text(), "MINIO_EXAMPLE=prepared\n")

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

    def test_image_build_does_not_require_runtime_ownership(self) -> None:
        with (
            mock.patch.object(provider, "initialize"),
            mock.patch.object(
                provider,
                "verify_scope",
                side_effect=provider.QuickstartError("foreign runtime"),
            ) as scope,
            mock.patch.object(provider, "compose"),
        ):
            provider.execute("build", ["kafka"])
        scope.assert_not_called()

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
