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
    def test_keycloak_creates_group_readable_tls_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            keycloak = Path(temporary).resolve()
            (keycloak / ".env.example").write_text("")

            def create_openssl_outputs(*arguments: str) -> None:
                for option in ("-keyout", "-out"):
                    if option in arguments:
                        output = Path(arguments[arguments.index(option) + 1])
                        output.write_text("fixture\n")

            with (
                mock.patch.object(provider, "KEYCLOAK", keycloak),
                mock.patch.object(
                    provider, "openssl", side_effect=create_openssl_outputs
                ),
            ):
                provider.initialize_keycloak()

            tls_key = keycloak / "secrets/https/tls.key"
            self.assertEqual(tls_key.stat().st_mode & 0o777, 0o640)
            self.assertEqual(tls_key.stat().st_gid, os.getgid())

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

    def test_keycloak_migrates_only_legacy_local_admin_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            keycloak = Path(temporary).resolve()
            (keycloak / ".env.example").write_text("")
            (keycloak / ".env").write_text(
                "AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_ENDPOINT=https://127.0.0.1:18443\n"
            )
            tls = keycloak / "secrets/https"
            tls.mkdir(parents=True)
            for name in ("ca.crt", "tls.crt", "tls.key"):
                (tls / name).write_text("fixture\n")

            with mock.patch.object(provider, "KEYCLOAK", keycloak):
                provider.initialize_keycloak()

            self.assertEqual(
                provider.read_env(keycloak / ".env")[
                    "AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_ENDPOINT"
                ],
                "https://keycloak:8443",
            )

    def test_keycloak_preserves_custom_admin_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            keycloak = Path(temporary).resolve()
            (keycloak / ".env.example").write_text("")
            (keycloak / ".env").write_text(
                "AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_ENDPOINT=https://identity.example.com\n"
            )
            tls = keycloak / "secrets/https"
            tls.mkdir(parents=True)
            for name in ("ca.crt", "tls.crt", "tls.key"):
                (tls / name).write_text("fixture\n")

            with mock.patch.object(provider, "KEYCLOAK", keycloak):
                provider.initialize_keycloak()

            self.assertEqual(
                provider.read_env(keycloak / ".env")[
                    "AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_ENDPOINT"
                ],
                "https://identity.example.com",
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
                    provider, "docker_bridge_gateway", return_value="172.31.0.1"
                ),
                mock.patch.object(provider, "initialize_local_bridge_routes"),
                mock.patch.object(
                    provider, "initialize_translation_provider_credential"
                ),
                mock.patch.object(
                    provider, "initialize_minio", side_effect=prepare_minio
                ),
                mock.patch.object(provider, "compose"),
            ):
                provider.initialize()
            self.assertEqual((minio / ".env").read_text(), "MINIO_EXAMPLE=prepared\n")

    def test_local_bridge_routes_follow_docker_gateway_without_overriding_custom(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            keycloak = root / "keycloak"
            platform = root / "platform-ui"
            keycloak.mkdir()
            platform.mkdir()
            (keycloak / ".env").write_text(
                "AELEV_INFRASTRUCTURE_KEYCLOAK_BIND=127.0.0.1\n"
                "AELEV_INFRASTRUCTURE_KEYCLOAK_LOCAL_BRIDGE_BIND=172.17.0.1\n"
            )
            (platform / ".env").write_text(
                "AELEV_INFRASTRUCTURE_PLATFORM_UI_BIND=127.0.0.1\n"
                "AELEV_INFRASTRUCTURE_PLATFORM_UI_LOCAL_BRIDGE_BIND=172.30.0.1\n"
            )

            with (
                mock.patch.object(provider, "KEYCLOAK", keycloak),
                mock.patch.object(provider, "PLATFORM", platform),
            ):
                provider.initialize_local_bridge_routes("172.31.0.1")

            self.assertEqual(
                provider.read_env(keycloak / ".env")[
                    "AELEV_INFRASTRUCTURE_KEYCLOAK_LOCAL_BRIDGE_BIND"
                ],
                "172.31.0.1",
            )
            self.assertEqual(
                provider.read_env(platform / ".env")[
                    "AELEV_INFRASTRUCTURE_PLATFORM_UI_LOCAL_BRIDGE_BIND"
                ],
                "172.30.0.1",
            )

    def test_docker_bridge_gateway_is_private(self) -> None:
        with mock.patch.object(
            provider,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 0, stdout="172.31.0.1\n", stderr=""
            ),
        ):
            self.assertEqual(provider.docker_bridge_gateway(), "172.31.0.1")

    def test_additive_public_trust_path_preserves_existing_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner = root / "owner"
            owner.mkdir()
            (owner / ".env.example").write_text(
                "OWNER_VALUE=template\n" "OWNER_OIDC_CA_FILE=./trust/oidc-ca.crt\n"
            )
            (owner / ".env").write_text("OWNER_VALUE=custom\n")

            with mock.patch.object(provider, "ROOT", root):
                provider.initialize_additive_local_paths()

            self.assertEqual(
                provider.read_env(owner / ".env"),
                {
                    "OWNER_VALUE": "custom",
                    "OWNER_OIDC_CA_FILE": "./trust/oidc-ca.crt",
                },
            )

    def test_new_observability_environment_selects_operational_owner_envs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observability = root / "observability"
            owner = root / "owner"
            observability.mkdir()
            owner.mkdir()
            (observability / ".env.example").write_text(
                "AELEV_INFRASTRUCTURE_OBSERVABILITY_COMPOSE_PROJECT=example\n"
                "AELEV_INFRASTRUCTURE_OBSERVABILITY_ENV_FILE=.env.example\n"
            )
            (owner / ".env.example").write_text("OWNER_VALUE=template\n")

            with (
                mock.patch.object(provider, "ROOT", root),
                mock.patch.object(provider, "OBSERVABILITY", observability),
            ):
                provider.initialize_owner_environments()

            self.assertEqual(
                provider.read_env(observability / ".env"),
                {
                    "AELEV_INFRASTRUCTURE_OBSERVABILITY_COMPOSE_PROJECT": "example",
                    "AELEV_INFRASTRUCTURE_OBSERVABILITY_ENV_FILE": ".env",
                },
            )
            self.assertEqual(
                provider.read_env(owner / ".env"), {"OWNER_VALUE": "template"}
            )

    def test_existing_observability_environment_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observability = root / "observability"
            observability.mkdir()
            (observability / ".env.example").write_text(
                "AELEV_INFRASTRUCTURE_OBSERVABILITY_ENV_FILE=.env.example\n"
            )
            (observability / ".env").write_text(
                "AELEV_INFRASTRUCTURE_OBSERVABILITY_ENV_FILE=custom.env\n"
            )

            with (
                mock.patch.object(provider, "ROOT", root),
                mock.patch.object(provider, "OBSERVABILITY", observability),
            ):
                provider.initialize_owner_environments()

            self.assertEqual(
                provider.read_env(observability / ".env")[
                    "AELEV_INFRASTRUCTURE_OBSERVABILITY_ENV_FILE"
                ],
                "custom.env",
            )

    def test_local_bridge_routes_reject_wildcard_primary_listener(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            keycloak = root / "keycloak"
            platform = root / "platform-ui"
            keycloak.mkdir()
            platform.mkdir()
            (keycloak / ".env").write_text(
                "AELEV_INFRASTRUCTURE_KEYCLOAK_BIND=0.0.0.0\n"
            )
            (platform / ".env").write_text(
                "AELEV_INFRASTRUCTURE_PLATFORM_UI_BIND=127.0.0.1\n"
            )

            with (
                mock.patch.object(provider, "KEYCLOAK", keycloak),
                mock.patch.object(provider, "PLATFORM", platform),
                self.assertRaisesRegex(provider.QuickstartError, "must not overlap"),
            ):
                provider.initialize_local_bridge_routes("172.31.0.1")

    def test_platform_adds_identity_but_kafka_does_not(self) -> None:
        self.assertIn("keycloak", provider.selected(["platform-ui"]))
        self.assertEqual(provider.selected(["kafka"]), ("kafka",))
        self.assertIn("translation-provider", provider.selected(["translation"]))
        self.assertIn("translator-worker", provider.selected(["translation"]))
        self.assertEqual(
            provider.selected_pulls(["translation"]),
            ("translation-provider", "translator-worker"),
        )

    def test_provider_credential_is_imported_without_changing_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            translator = root / "translator-service"
            source = root.parent / "TranslationProvider/.env.local"
            translator.mkdir()
            source.parent.mkdir(exist_ok=True)
            source.write_bytes(b"OPENAI_API_KEY=fixture-value\n")
            (translator / ".env").write_text(
                "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_SECRET_FILE="
                "./runtime/provider-credential.env\n"
                f"AELEV_INFRASTRUCTURE_TRANSLATOR_SECRET_GID={os.getgid()}\n"
            )

            with (
                mock.patch.object(provider, "ROOT", root),
                mock.patch.object(provider, "TRANSLATOR", translator),
            ):
                provider.initialize_translation_provider_credential()

            imported = translator / "runtime/provider-credential.env"
            self.assertEqual(imported.read_bytes(), source.read_bytes())
            self.assertEqual(imported.stat().st_mode & 0o777, 0o640)
            self.assertEqual(imported.stat().st_gid, os.getgid())

    def test_additive_translation_pins_preserve_existing_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            translator = root / "translator-service"
            translator.mkdir()
            (translator / ".env.example").write_text(
                "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_CPUS=2.0\n"
                "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_EGRESS_NETWORK="
                "provider-egress\n"
                "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_EGRESS_SUBNET="
                "10.253.53.0/28\n"
                "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_IMAGE=provider@sha256:new\n"
                "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_MEMORY_LIMIT=8g\n"
                "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_RESTART_POLICY="
                "on-failure:3\n"
                "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_SECRET_FILE="
                "./runtime/provider-credential.env\n"
                "AELEV_INFRASTRUCTURE_TRANSLATOR_WORKER_IMAGE=worker@sha256:new\n"
            )
            (translator / ".env").write_text(
                "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_IMAGE="
                "provider@sha256:existing\n"
            )

            with mock.patch.object(provider, "ROOT", root):
                provider.initialize_additive_local_paths()

            values = provider.read_env(translator / ".env")
            self.assertEqual(
                values["AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_IMAGE"],
                "provider@sha256:existing",
            )
            self.assertEqual(
                values["AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_SECRET_FILE"],
                "./runtime/provider-credential.env",
            )
            self.assertEqual(
                values["AELEV_INFRASTRUCTURE_TRANSLATOR_WORKER_IMAGE"],
                "worker@sha256:new",
            )
            self.assertEqual(
                values["AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_CPUS"], "2.0"
            )
            self.assertEqual(
                values["AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_EGRESS_NETWORK"],
                "provider-egress",
            )
            self.assertEqual(
                values["AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_MEMORY_LIMIT"],
                "8g",
            )

    def test_platform_migrates_only_legacy_local_keycloak_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            keycloak = root / "keycloak"
            platform = root / "platform-ui"
            keycloak.mkdir()
            platform.mkdir()
            (keycloak / ".env").write_text(
                "AELEV_INFRASTRUCTURE_KEYCLOAK_INGRESS_NETWORK=owned-ingress\n"
            )
            (keycloak / "secrets").mkdir()
            (keycloak / "secrets/admin-password").write_text("fixture\n")
            (platform / ".env").write_text(
                "AELEV_INFRASTRUCTURE_PLATFORM_UI_KEYCLOAK_BACKCHANNEL_URL=https://keycloak:18443\n"
                "AELEV_INFRASTRUCTURE_PLATFORM_UI_SECRET_GID=123\n"
            )
            (platform / "secrets/nginx").mkdir(parents=True)
            (platform / "secrets/nginx/server.crt").write_text("fixture\n")

            with (
                mock.patch.object(provider, "KEYCLOAK", keycloak),
                mock.patch.object(provider, "PLATFORM", platform),
                mock.patch.object(provider, "run"),
            ):
                provider.initialize_platform()

            values = provider.read_env(platform / ".env")
            self.assertEqual(
                values["AELEV_INFRASTRUCTURE_PLATFORM_UI_KEYCLOAK_BACKCHANNEL_URL"],
                "https://keycloak:8443",
            )
            self.assertEqual(
                values["AELEV_INFRASTRUCTURE_PLATFORM_UI_KEYCLOAK_INGRESS_NETWORK"],
                "owned-ingress",
            )
            self.assertEqual(
                values["AELEV_INFRASTRUCTURE_PLATFORM_UI_SECRET_GID"], "123"
            )

    def test_platform_preserves_remote_keycloak_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            keycloak = root / "keycloak"
            platform = root / "platform-ui"
            keycloak.mkdir()
            platform.mkdir()
            (keycloak / ".env").write_text(
                "AELEV_INFRASTRUCTURE_KEYCLOAK_INGRESS_NETWORK=owned-ingress\n"
            )
            (keycloak / "secrets").mkdir()
            (keycloak / "secrets/admin-password").write_text("fixture\n")
            (platform / ".env").write_text(
                "AELEV_INFRASTRUCTURE_PLATFORM_UI_KEYCLOAK_BACKCHANNEL_URL=https://identity.example.com\n"
                "AELEV_INFRASTRUCTURE_PLATFORM_UI_SECRET_GID=123\n"
            )
            (platform / "secrets/nginx").mkdir(parents=True)
            (platform / "secrets/nginx/server.crt").write_text("fixture\n")

            with (
                mock.patch.object(provider, "KEYCLOAK", keycloak),
                mock.patch.object(provider, "PLATFORM", platform),
                mock.patch.object(provider, "run"),
            ):
                provider.initialize_platform()

            values = provider.read_env(platform / ".env")
            self.assertEqual(
                values["AELEV_INFRASTRUCTURE_PLATFORM_UI_KEYCLOAK_BACKCHANNEL_URL"],
                "https://identity.example.com",
            )
            self.assertEqual(
                values["AELEV_INFRASTRUCTURE_PLATFORM_UI_KEYCLOAK_INGRESS_NETWORK"],
                "owned-ingress",
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
