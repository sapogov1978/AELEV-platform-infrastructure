"""Exercise private workspace publication, isolation, drift and durable inputs."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import node_execution as node
from tools import owner_prepare as subject


class OwnerPrepareTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.executor = self.root / "executor"
        (self.executor / "tools").mkdir(parents=True)
        for name in ("owner_prepare", "owner_build", "owner_compose", "node_execution"):
            source = Path(subject.__file__).parent / (name + ".py")
            (self.executor / "tools" / source.name).write_bytes(source.read_bytes())
        self.source = self.root / "owner"
        (self.source / "service/config").mkdir(parents=True)
        (self.source / "service/compose.yml").write_text("services: {}\n")
        (self.source / "service/config/public.json").write_text('{"value":"public"}\n')
        (self.source / "unrelated").write_text("not selected\n")
        self.catalog = {
            "product": "qualification",
            "roles": [
                {
                    "id": "api",
                    "owner": "service",
                    "opaque": False,
                    "recipes": ["service/compose.yml"],
                }
            ],
        }
        (self.source / "deployment-catalog.json").write_text(json.dumps(self.catalog))
        self.environment = self.root / "input.env"
        self.body = b"ENDPOINT=https://independent-host.example:18443\nPRIVATE_FILE=/explicit/private/path\n"
        self.environment.write_bytes(self.body)
        self.environment.chmod(0o600)
        self.workspace = self.root / "workspace"
        self.value = {
            "schema_version": 1,
            "executor_root": str(self.executor),
            "executor_files": self.files(self.executor),
            "owner_root": str(self.source),
            "owner_files": self.files(self.source),
            "catalog": "deployment-catalog.json",
            "role": "qualification/api",
            "workspace": str(self.workspace),
            "env_file": str(self.environment),
            "inputs": {str(self.environment): subject.owner.digest(self.body)},
        }

    def files(self, root: Path) -> dict[str, str]:
        return {
            str(p.relative_to(root)): subject.owner.digest(p.read_bytes())
            for p in root.rglob("*")
            if p.is_file()
        }

    def test_prepare_preserves_exact_environment_and_only_owner_public_files(
        self,
    ) -> None:
        private_extra = self.source / "service/.env"
        private_extra.write_text("NOT_A_SOURCE_INPUT=private\n")
        with mock.patch.object(subject.build.subprocess, "run") as commands:
            result = subject.execute(self.value, "prepare")
        commands.assert_not_called()
        self.assertEqual(result["status"], "OWNER_CONFIGURATION_PREPARED")
        self.assertEqual((self.workspace / ".env").read_bytes(), self.body)
        self.assertFalse((self.workspace / "unrelated").exists())
        self.assertEqual(
            (self.workspace / "config/public.json").stat().st_mode & 0o777, 0o644
        )
        self.assertEqual((self.workspace / "config").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.workspace / ".env").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.workspace.stat().st_mode & 0o777, 0o700)
        self.assertEqual(private_extra.read_text(), "NOT_A_SOURCE_INPUT=private\n")

    def test_repeat_and_observation_do_not_rewrite_workspace(self) -> None:
        first = subject.execute(self.value, "prepare")
        before = {p: p.stat().st_mtime_ns for p in self.workspace.rglob("*")}
        self.assertEqual(subject.execute(self.value, "prepare"), first)
        self.assertEqual(subject.execute(self.value, "status"), first)
        self.assertEqual(
            {p: p.stat().st_mtime_ns for p in self.workspace.rglob("*")}, before
        )

    def private_input(self, target: str = "secrets/client/key") -> Path:
        source = self.root / "explicit-private-input"
        source.write_bytes(b"synthetic-private-bytes\x00\xff\n")
        source.chmod(0o600)
        self.value["private_files"] = {target: {"source": str(source), "mode": 0o640}}
        self.value["inputs"][str(source)] = subject.owner.digest(source.read_bytes())
        return source

    def test_selected_private_files_preserve_bytes_group_and_declared_mode(
        self,
    ) -> None:
        source = self.private_input()
        result = subject.execute(self.value, "prepare")
        target = self.workspace / "secrets/client/key"
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(target.stat().st_gid, subject.os.getgid())
        self.assertNotIn("synthetic-private", json.dumps(result))
        before = target.stat().st_mtime_ns
        self.assertEqual(subject.execute(self.value, "prepare"), result)
        self.assertEqual(subject.execute(self.value, "status"), result)
        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_private_source_hash_or_permissions_drift_prevents_publication(
        self,
    ) -> None:
        source = self.private_input()
        source.write_bytes(b"changed")
        with self.assertRaisesRegex(node.ExecutionError, "BINDING_INVALID"):
            subject.execute(self.value, "prepare")
        source.chmod(0o640)
        with self.assertRaisesRegex(
            node.ExecutionError, "PRIVATE_PERMISSIONS_REQUIRED"
        ):
            subject.execute(self.value, "prepare")
        self.assertFalse(self.workspace.exists())

    def test_private_target_bytes_or_mode_drift_are_not_restored(self) -> None:
        self.private_input()
        subject.execute(self.value, "prepare")
        target = self.workspace / "secrets/client/key"
        target.chmod(0o644)
        with self.assertRaisesRegex(node.ExecutionError, "WORKSPACE_DRIFT"):
            subject.execute(self.value, "status")
        target.chmod(0o640)
        target.write_bytes(b"owner changed bytes")
        with self.assertRaisesRegex(node.ExecutionError, "WORKSPACE_DRIFT"):
            subject.execute(self.value, "prepare")
        self.assertEqual(target.read_bytes(), b"owner changed bytes")

    def test_private_target_scope_modes_and_collisions_are_rejected(self) -> None:
        source = self.private_input()
        for target, mode in (
            ("../secrets/key", 0o600),
            ("/secrets/key", 0o600),
            ("config/private-key", 0o600),
            ("secrets", 0o600),
            ("secrets/key", 0o644),
            ("secrets/key", True),
        ):
            changed = copy.deepcopy(self.value)
            changed["private_files"] = {target: {"source": str(source), "mode": mode}}
            with self.assertRaises(node.ExecutionError):
                subject.execute(changed, "prepare")
        changed = copy.deepcopy(self.value)
        changed["private_files"]["secrets/client"] = {
            "source": str(source),
            "mode": 0o600,
        }
        with self.assertRaisesRegex(node.ExecutionError, "PRIVATE_FILE_COLLISION"):
            subject.execute(changed, "prepare")
        public = self.source / "service/secrets/client/key"
        public.parent.mkdir(parents=True)
        public.write_text("public source fixture\n")
        self.value["owner_files"] = self.files(self.source)
        with self.assertRaisesRegex(node.ExecutionError, "PRIVATE_FILE_COLLISION"):
            subject.execute(self.value, "prepare")
        self.assertFalse(self.workspace.exists())

    def test_private_inputs_are_explicit_bounded_and_never_follow_symlinks(
        self,
    ) -> None:
        source = self.private_input()
        changed = copy.deepcopy(self.value)
        changed["private_files"] = {
            "secrets/" + str(index): {"source": str(source), "mode": 0o600}
            for index in range(129)
        }
        with self.assertRaisesRegex(node.ExecutionError, "PRIVATE_FILES_INVALID"):
            subject.execute(changed, "prepare")
        source.write_bytes(b"x" * (1024**2 + 1))
        with self.assertRaisesRegex(node.ExecutionError, "FILE_SIZE_INVALID"):
            subject.execute(self.value, "prepare")
        source.unlink()
        source.symlink_to(self.environment)
        with self.assertRaisesRegex(node.ExecutionError, "SYMLINK_REFUSED"):
            subject.execute(self.value, "prepare")
        self.assertFalse(self.workspace.exists())

    def test_empty_optional_private_set_preserves_old_workspace_receipt(self) -> None:
        first = subject.execute(self.value, "prepare")
        self.value["private_files"] = {}
        self.assertEqual(subject.execute(self.value, "status"), first)
        self.assertNotIn("private_files", node.read(self.workspace / subject.RECEIPT))

    def test_status_cannot_create_workspace_or_lock(self) -> None:
        before = list(self.root.iterdir())
        with self.assertRaisesRegex(node.ExecutionError, "WORKSPACE_NOT_FOUND"):
            subject.execute(self.value, "status")
        self.assertEqual(list(self.root.iterdir()), before)

    def test_changed_environment_requires_handoff_and_preserves_old_bytes(self) -> None:
        subject.execute(self.value, "prepare")
        self.environment.write_text("ENDPOINT=https://different.example:19443\n")
        changed = copy.deepcopy(self.value)
        changed["inputs"][str(self.environment)] = subject.owner.digest(
            self.environment.read_bytes()
        )
        with self.assertRaisesRegex(node.ExecutionError, "WORKSPACE_HANDOFF_REQUIRED"):
            subject.execute(changed, "prepare")
        self.assertEqual((self.workspace / ".env").read_bytes(), self.body)

    def test_environment_drift_or_public_source_drift_blocks_publication(self) -> None:
        self.environment.write_bytes(self.body + b"CHANGED=1\n")
        with self.assertRaisesRegex(node.ExecutionError, "ENVIRONMENT_BINDING_INVALID"):
            subject.execute(self.value, "prepare")
        self.environment.write_bytes(self.body)
        (self.source / "service/config/public.json").write_text("changed\n")
        with self.assertRaisesRegex(node.ExecutionError, "SOURCE_DRIFT"):
            subject.execute(self.value, "prepare")
        self.assertFalse(self.workspace.exists())

    def test_workspace_public_drift_is_never_automatically_restored(self) -> None:
        subject.execute(self.value, "prepare")
        path = self.workspace / "config/public.json"
        path.write_text("operator change\n")
        for action in ("prepare", "status"):
            with self.assertRaisesRegex(node.ExecutionError, "WORKSPACE_DRIFT"):
                subject.execute(self.value, action)
        self.assertEqual(path.read_text(), "operator change\n")

    def test_missing_existing_receipt_does_not_overwrite_directory(self) -> None:
        self.workspace.mkdir(mode=0o700)
        marker = self.workspace / "canonical-state"
        marker.write_text("preserve\n")
        with self.assertRaises(OSError):
            subject.execute(self.value, "prepare")
        self.assertEqual(marker.read_text(), "preserve\n")
        self.assertFalse((self.workspace / ".env").exists())

    def test_workspace_source_overlap_and_symlink_are_refused(self) -> None:
        for path in (
            self.source / "service/workspace",
            self.executor / "workspace",
            self.root,
        ):
            changed = {**self.value, "workspace": str(path)}
            with self.assertRaises(node.ExecutionError):
                subject.execute(changed, "prepare")
        self.workspace.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(node.ExecutionError, "SYMLINK_REFUSED"):
            subject.execute(self.value, "prepare")

    def test_shared_environment_permissions_are_refused_before_copy(self) -> None:
        self.environment.chmod(0o644)
        with self.assertRaisesRegex(
            node.ExecutionError, "PRIVATE_PERMISSIONS_REQUIRED"
        ):
            subject.execute(self.value, "prepare")
        self.assertFalse(self.workspace.exists())

    def test_failed_sync_does_not_publish_a_partial_workspace(self) -> None:
        with mock.patch.object(
            subject, "sync_tree", side_effect=OSError("fixture failure")
        ):
            with self.assertRaises(OSError):
                subject.execute(self.value, "prepare")
        self.assertFalse(self.workspace.exists())
        self.assertEqual(self.environment.read_bytes(), self.body)

    def test_simultaneous_prepare_is_refused_without_second_publication(self) -> None:
        with subject.workspace_lock(self.workspace):
            with self.assertRaises(BlockingIOError):
                subject.execute(self.value, "prepare")
        self.assertFalse(self.workspace.exists())

    def test_owner_or_catalog_mismatch_is_refused(self) -> None:
        for changed in (
            {**self.value, "role": "qualification/missing"},
            {**self.value, "catalog": "unlisted-catalog.json"},
        ):
            with self.assertRaises(node.ExecutionError):
                subject.execute(changed, "prepare")
        self.assertFalse(self.workspace.exists())

    def test_preparation_projection_strips_private_fields_and_requires_hashes(
        self,
    ) -> None:
        result = subject.execute(self.value, "prepare")
        path = self.root / "projection.json"
        path.write_text(json.dumps({**result, "private_environment": "not public"}))
        self.assertEqual(node.owner_projection(path), result)
        result.pop("workspace_sha256")
        path.write_text(json.dumps(result))
        self.assertIsNone(node.owner_projection(path))


if __name__ == "__main__":
    unittest.main()
