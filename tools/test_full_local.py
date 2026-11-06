from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

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
