from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FULL_LOCAL_SPEC = importlib.util.spec_from_file_location(
    "full_local_contract", ROOT / "tools/full_local.py"
)
assert FULL_LOCAL_SPEC is not None and FULL_LOCAL_SPEC.loader is not None
full_local = importlib.util.module_from_spec(FULL_LOCAL_SPEC)
FULL_LOCAL_SPEC.loader.exec_module(full_local)
SPEC = importlib.util.spec_from_file_location(
    "clean_room_evidence", ROOT / "test_infrastructure/clean_room.py"
)
assert SPEC is not None and SPEC.loader is not None
clean_room = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(clean_room)
GATE_SPEC = importlib.util.spec_from_file_location(
    "clean_room_gate", ROOT / "test_infrastructure/clean_room_gate.py"
)
assert GATE_SPEC is not None and GATE_SPEC.loader is not None
clean_room_gate = importlib.util.module_from_spec(GATE_SPEC)
sys.modules[GATE_SPEC.name] = clean_room_gate
GATE_SPEC.loader.exec_module(clean_room_gate)


class FullLocalContractTests(unittest.TestCase):
    def test_repository_contract_is_complete(self) -> None:
        manifest, errors = full_local.validate_contract(ROOT)
        self.assertEqual(errors, [])
        self.assertEqual(manifest["profile"], "full-local")
        self.assertEqual(
            manifest["acceptance"]["required_functions"],
            sorted(
                set(manifest["acceptance"]["required_functions"]),
                key=manifest["acceptance"]["required_functions"].index,
            ),
        )

    def test_plan_includes_compute_and_acceptance(self) -> None:
        plan = full_local.source_plan(ROOT)
        self.assertEqual(plan["compose_models_included"], ["root", "compute"])
        self.assertFalse(plan["translation_provider_implementation_included"])
        self.assertEqual(
            plan["compute_services"], ["translation-provider", "translator-worker"]
        )
        self.assertEqual(
            plan["build_inputs"],
            [
                {
                    "id": "observability-sender-image",
                    "kind": "container-image-digest",
                    "owner": "observability/delivery",
                    "admitted_reference": (
                        "ghcr.io/sapogov1978/"
                        "aelev-platform-infrastructure-observability-sender"
                        "@sha256:162f07e6fd043eb3d9e04e7c415bee07771321a6f7f3864f"
                        "cba0ce1f80dd941e"
                    ),
                }
            ],
        )
        self.assertEqual(
            plan["release_images"],
            [
                {
                    "id": "observability-delivery-image",
                    "owner": "observability/delivery",
                    "admitted_reference": (
                        "ghcr.io/sapogov1978/"
                        "aelev-platform-infrastructure-observability-delivery"
                        "@sha256:d54693da1819b78a20af65d3fc5eaab12b6b12873dd231f6"
                        "5f18f4aa5f3aedc0"
                    ),
                }
            ],
        )
        self.assertEqual(len(plan["acceptance_functions"]), 11)

    def test_gate_stages_cover_each_function_once(self) -> None:
        manifest = json.loads((ROOT / "full-local.json").read_text())
        mapped = [
            function
            for functions in clean_room_gate.FUNCTION_STAGES.values()
            for function in functions
        ]
        self.assertEqual(
            sorted(mapped), sorted(manifest["acceptance"]["required_functions"])
        )
        self.assertEqual(len(mapped), len(set(mapped)))

    def test_digest_pinned_image_is_not_a_build_tag(self) -> None:
        manifest = json.loads((ROOT / "full-local.json").read_text())
        model = {
            "services": {
                "source-build": {"build": {"context": "."}, "image": "local:test"},
                "release-pull": {
                    "build": {"context": "."},
                    "image": "example.invalid/release@sha256:" + "a" * 64,
                },
            }
        }
        source_builds, pinned_pulls = full_local.build_service_sets(model)
        self.assertEqual(source_builds, ["source-build"])
        self.assertEqual(pinned_pulls, ["release-pull"])
        self.assertEqual(manifest["profile"], "full-local")

    def test_new_env_identity_adaptation_preserves_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fresh = root / "fresh/.env"
            existing = root / "existing/.env"
            fresh.parent.mkdir()
            existing.parent.mkdir()
            content = "OWNER_SECRET_GID=10000\nOWNER_UI_PROVISION_UID=501\nVALUE=x\n"
            fresh.write_text(content, encoding="utf-8")
            existing.write_text(content, encoding="utf-8")
            manifest = {"environment_files": ["fresh/.env", "existing/.env"]}

            adapted = full_local.adapt_created_env_identities(
                manifest, {"existing/.env"}, root
            )

            self.assertEqual(adapted, ["fresh/.env"])
            self.assertIn(f"OWNER_SECRET_GID={os.getgid()}", fresh.read_text())
            self.assertIn(f"OWNER_UI_PROVISION_UID={os.getuid()}", fresh.read_text())
            self.assertEqual(existing.read_text(), content)

    def test_new_local_runtime_secrets_are_group_readable_without_ca_key_scope(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            translator = root / "translator-service"
            postgresql = root / "postgresql"
            translator.mkdir()
            postgresql.mkdir()
            (translator / ".env").write_text(
                "AELEV_INFRASTRUCTURE_TRANSLATOR_DATABASE_SECRET_ROOT=./secrets/database\n"
                "AELEV_INFRASTRUCTURE_TRANSLATOR_KAFKA_SECRET_ROOT=./secrets/kafka-runtime\n"
                "AELEV_INFRASTRUCTURE_TRANSLATOR_KAFKA_ADMIN_SECRET_ROOT=./secrets/kafka-admin\n"
                "AELEV_INFRASTRUCTURE_TRANSLATOR_TELEMETRY_SECRET_ROOT=./secrets/telemetry\n"
                "AELEV_INFRASTRUCTURE_TRANSLATOR_API_TLS_ROOT=./runtime/api-pki/api-tls\n"
            )
            roots = [
                translator / "secrets/database",
                translator / "secrets/kafka-runtime",
                translator / "secrets/kafka-admin",
                translator / "secrets/telemetry/client",
                translator / "runtime/api-pki/api-tls",
                postgresql / "secrets/fencing",
            ]
            for path in roots:
                path.mkdir(parents=True)
                path.chmod(0o700)
                secret = path / "secret.key"
                secret.write_text("fixture\n")
                secret.chmod(0o600)
            ca_key = translator / "runtime/api-pki/api-ca/ca.key"
            ca_key.parent.mkdir(parents=True)
            ca_key.write_text("fixture\n")
            ca_key.chmod(0o600)

            prepared = full_local.prepare_local_runtime_secret_access(set(), root)

            self.assertEqual(len(prepared), 6)
            for path in roots:
                self.assertEqual(path.stat().st_mode & 0o777, 0o750)
                self.assertEqual((path / "secret.key").stat().st_mode & 0o777, 0o640)
                self.assertEqual((path / "secret.key").stat().st_gid, os.getgid())
            self.assertEqual(ca_key.stat().st_mode & 0o777, 0o600)

            for path in roots:
                path.chmod(0o700)
                (path / "secret.key").chmod(0o600)
            self.assertEqual(
                full_local.prepare_local_runtime_secret_access(
                    {"translator-service/.env", "postgresql/.env"}, root
                ),
                [],
            )
            self.assertTrue(
                all(
                    (path / "secret.key").stat().st_mode & 0o777 == 0o600
                    for path in roots
                )
            )

    def test_local_deployment_admin_is_owner_local_and_create_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keycloak_secret = root / "keycloak/secrets/admin-password"
            keycloak_secret.parent.mkdir(parents=True)
            keycloak_secret.write_bytes(b"generated-local-password\n")
            (root / "keycloak/.env").write_text(
                "AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_USER=local-admin\n"
            )
            first_env = root / "first/.env"
            second_env = root / "second/.env"
            first_env.parent.mkdir()
            second_env.parent.mkdir()
            first_env.write_text("FIRST_UI_ADMIN_ROOT=./secrets/status-admin\n")
            second_env.write_text("SECOND_UI_ADMIN_ROOT=./secrets/status-admin\n")
            existing = second_env.parent / "secrets/status-admin/admin-password"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"preserved-password\n")
            manifest = {"environment_files": ["first/.env", "second/.env"]}

            prepared = full_local.prepare_local_deployment_admin(manifest, root)

            self.assertEqual(
                prepared, ["first/secrets/status-admin", "second/secrets/status-admin"]
            )
            self.assertEqual(
                (first_env.parent / "secrets/status-admin/admin-password").read_bytes(),
                b"generated-local-password\n",
            )
            self.assertEqual(existing.read_bytes(), b"preserved-password\n")
            for owner in (first_env.parent, second_env.parent):
                admin_root = owner / "secrets/status-admin"
                self.assertEqual(
                    (admin_root / "admin-user").read_text(), "local-admin\n"
                )
                self.assertEqual(
                    (admin_root / "admin-user").stat().st_mode & 0o777, 0o640
                )

            self.assertEqual(
                full_local.prepare_local_deployment_admin(manifest, root), []
            )

    def test_local_oidc_trust_delivers_only_public_ca_create_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ca = root / "keycloak/secrets/https/ca.crt"
            ca.parent.mkdir(parents=True)
            ca.write_bytes(b"public-local-ca\n")
            owner_env = root / "owner/.env"
            owner_env.parent.mkdir()
            owner_env.write_text("OWNER_OIDC_CA_FILE=./trust/oidc-ca.crt\n")
            manifest = {"environment_files": ["owner/.env"]}

            self.assertEqual(
                full_local.prepare_local_oidc_trust(manifest, root),
                ["owner/trust/oidc-ca.crt"],
            )
            delivered = owner_env.parent / "trust/oidc-ca.crt"
            self.assertEqual(delivered.read_bytes(), b"public-local-ca\n")
            self.assertEqual(delivered.stat().st_mode & 0o777, 0o644)
            self.assertEqual(delivered.stat().st_uid, os.getuid())
            self.assertEqual(delivered.stat().st_gid, os.getgid())
            self.assertEqual(delivered.parent.stat().st_mode & 0o777, 0o755)

            delivered.write_bytes(b"approved-existing-ca\n")
            self.assertEqual(full_local.prepare_local_oidc_trust(manifest, root), [])
            self.assertEqual(delivered.read_bytes(), b"approved-existing-ca\n")

    def test_local_operator_trust_is_owner_local_and_create_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ca = root / "keycloak/secrets/https/ca.crt"
            ca.parent.mkdir(parents=True)
            ca.write_bytes(b"public-local-ca\n")
            first_env = root / "first/.env"
            second_env = root / "second/.env"
            first_env.parent.mkdir()
            second_env.parent.mkdir()
            first_env.write_text("FIRST_OPERATOR_SECRET_ROOT=./secrets/operator\n")
            second_env.write_text("SECOND_OPERATOR_SECRET_ROOT=./secrets/operator\n")
            existing = second_env.parent / "secrets/operator/identity-ca.crt"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"approved-existing-ca\n")
            manifest = {"environment_files": ["first/.env", "second/.env"]}

            self.assertEqual(
                full_local.prepare_local_operator_trust(manifest, root),
                ["first/secrets/operator/identity-ca.crt"],
            )
            delivered = first_env.parent / "secrets/operator/identity-ca.crt"
            self.assertEqual(delivered.read_bytes(), b"public-local-ca\n")
            self.assertEqual(delivered.stat().st_mode & 0o777, 0o640)
            self.assertEqual(delivered.parent.stat().st_mode & 0o777, 0o750)
            self.assertEqual(existing.read_bytes(), b"approved-existing-ca\n")
            self.assertEqual(
                full_local.prepare_local_operator_trust(manifest, root), []
            )

    def test_local_status_identities_are_owner_local_and_create_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = {
                "platform-ui/secrets/issuer/publisher.crt": b"publisher-ca\n",
                "platform-ui/secrets/issuer/publisher.key": b"publisher-key\n",
                "platform-ui/secrets/issuer/transport.crt": b"transport-ca\n",
                "platform-ui/secrets/issuer/transport.key": b"transport-key\n",
                "keycloak/secrets/https/ca.crt": b"identity-ca\n",
                "observability/runtime/local-issuer/internal-ca.crt": b"native-ca\n",
                "observability/runtime/local-issuer/internal-ca.key": b"native-key\n",
            }
            for relative, payload in sources.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            owner_env = root / "observability/example/.env"
            owner_env.parent.mkdir(parents=True)
            owner_env.write_text("EXAMPLE_UI_SECRET_ROOT=./secrets/status\n")
            platform_env = root / "platform-ui/.env"
            platform_env.parent.mkdir(parents=True, exist_ok=True)
            platform_env.write_text(
                "AELEV_INFRASTRUCTURE_PLATFORM_UI_SECRET_ROOT=./secrets\n"
                "AELEV_INFRASTRUCTURE_PLATFORM_UI_UI_SECRET_ROOT=./secrets/status\n"
            )
            manifest = {
                "environment_files": [
                    "observability/example/.env",
                    "platform-ui/.env",
                ]
            }

            def issue(arguments: list[str], **_: object) -> None:
                destination = Path(arguments[arguments.index("--root") + 1])
                name = arguments[arguments.index("--name") + 1]
                destination.mkdir(parents=True, exist_ok=True)
                (destination / f"{name}.crt").write_bytes(b"issued-cert\n")
                (destination / f"{name}.key").write_bytes(b"issued-key\n")

            with mock.patch.object(full_local, "command", side_effect=issue):
                self.assertEqual(
                    full_local.prepare_local_status_identities(manifest, root),
                    [
                        "observability/example/secrets/status",
                        "platform-ui/secrets/status",
                    ],
                )

            status = owner_env.parent / "secrets/status"
            self.assertEqual(
                {path.name for path in status.iterdir()},
                {
                    "identity-ca.crt",
                    "probe-ca.crt",
                    "probe.crt",
                    "probe.key",
                    "publisher.crt",
                    "publisher.key",
                    "shell-ca.crt",
                },
            )
            self.assertEqual((status / "publisher.key").read_bytes(), b"issued-key\n")
            self.assertFalse((platform_env.parent / "secrets/publisher.key").exists())
            with mock.patch.object(full_local, "command") as command:
                self.assertEqual(
                    full_local.prepare_local_status_identities(manifest, root), []
                )
            command.assert_not_called()

    def test_complete_evidence_verifies_and_tamper_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "full-local.json").write_bytes(
                (ROOT / "full-local.json").read_bytes()
            )
            (root / "release.json").write_bytes((ROOT / "release.json").read_bytes())
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            artifact = evidence_root / "probe.txt"
            artifact.write_text("bounded probe\n", encoding="utf-8")
            artifact_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            source_commit = "a" * 40
            bom = {
                "schema_version": 1,
                "profile": "full-local",
                "release": "0.2.0",
                "source_commit": source_commit,
            }
            bom_path = root / "release-bom.json"
            bom_path.write_text(
                json.dumps(bom, sort_keys=True) + "\n", encoding="utf-8"
            )
            manifest = json.loads((root / "full-local.json").read_text())
            evidence = {
                "schema_version": 1,
                "profile": "full-local",
                "release": "0.2.0",
                "source_commit": source_commit,
                "bom_sha256": hashlib.sha256(bom_path.read_bytes()).hexdigest(),
                "started_at": "2026-11-06T10:00:00Z",
                "finished_at": "2026-11-06T10:01:00Z",
                "clean_baseline": True,
                "cleanup_preserved": True,
                "functions": [
                    {
                        "id": identity,
                        "status": "PASS",
                        "evidence": [{"path": "probe.txt", "sha256": artifact_digest}],
                    }
                    for identity in manifest["acceptance"]["required_functions"]
                ],
                "result": "PASS",
            }
            evidence_path = evidence_root / "evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            result = clean_room.verify(evidence_path, bom_path, root)
            self.assertEqual(result["status"], "PASS")

            artifact.write_text("tampered\n", encoding="utf-8")
            result = clean_room.verify(evidence_path, bom_path, root)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn(
                "FUNCTION_ARTIFACT_DIGEST_MISMATCH:identity-login",
                result["blocking_codes"],
            )


if __name__ == "__main__":
    unittest.main()
