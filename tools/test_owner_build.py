"""Protect source-only build inputs and immutable, non-deploying receipts."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

from tools import node_execution as node
from tools import owner_build as subject


class OwnerBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.executor, self.owner = self.root / "executor", self.root / "source"
        (self.executor / "tools").mkdir(parents=True)
        for name in ("owner_build.py", "owner_compose.py", "node_execution.py"):
            (self.executor / "tools" / name).write_bytes(
                (Path(__file__).parent / name).read_bytes()
            )
        application = self.owner / "application"
        application.mkdir(parents=True)
        (application / "Dockerfile").write_text(
            "FROM scratch AS runtime\nARG PUBLIC_ORIGIN\nARG API_KEY\nCOPY app /app\n"
        )
        (application / "app").write_text("public executable\n")
        (application / "app").chmod(0o755)
        (application / "package").mkdir()
        (application / "package/module.py").write_text("public_value = 1\n")
        catalog = {
            "product": "qualification",
            "roles": [
                {
                    "id": "worker",
                    "owner": "application",
                    "kind": "runtime",
                    "opaque": False,
                }
            ],
        }
        (self.owner / "deployment-catalog.json").write_text(json.dumps(catalog))
        self.directory = self.root / "operation"
        self.directory.mkdir(mode=0o700)
        self.identity = "sha256:" + "b" * 64
        self.value = {
            "schema_version": 1,
            "executor_root": str(self.executor),
            "executor_files": self.manifest(self.executor),
            "owner_root": str(self.owner),
            "owner_files": self.manifest(self.owner),
            "catalog": "deployment-catalog.json",
            "role": "qualification/worker",
            "context": "application",
            "recipe": "application/Dockerfile",
            "source_sha256": "sha256:"
            + subject.owner.digest((application / "Dockerfile").read_bytes()),
            "target": "runtime",
            "platform": "linux/arm64",
            "build_arguments": {},
            "inputs": {},
        }

    def manifest(self, root: Path) -> dict[str, str]:
        return {
            str(p.relative_to(root)): subject.owner.digest(p.read_bytes())
            for p in root.rglob("*")
            if p.is_file()
        }

    def observations(self, arguments: list[str]) -> bytes:
        if arguments[0] == "info":
            return b"linux/aarch64\n"
        self.assertEqual(arguments[:2], ["image", "inspect"])
        return json.dumps(
            {"id": self.identity, "os": "linux", "architecture": "arm64"}
        ).encode()

    def build(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.assertEqual(command[:4], ["docker", "buildx", "build", "--load"])
        self.assertNotIn("--tag", command)
        self.assertNotIn("--push", command)
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        context = Path(command[-1])
        self.assertFalse((context / ".env").exists())
        self.assertEqual((context / "app").stat().st_mode & 0o777, 0o755)
        self.assertEqual(context.stat().st_mode & 0o777, 0o755)
        self.assertEqual((context / "package").stat().st_mode & 0o777, 0o755)
        Path(command[command.index("--iidfile") + 1]).write_text(self.identity)
        Path(command[command.index("--metadata-file") + 1]).write_text(
            '{"buildx.build.provenance":{"materials":[]}}'
        )
        return subprocess.CompletedProcess(command, 0)

    def test_manifest_snapshot_excludes_untracked_inputs_and_keeps_build_receipt(
        self,
    ) -> None:
        (self.owner / "application/.env").write_text("ignored-private-fixture")
        with (
            mock.patch.object(subject, "docker", side_effect=self.observations),
            mock.patch.object(subject.subprocess, "run", side_effect=self.build),
            mock.patch.dict(
                subject.os.environ, {"OPENAI_API_KEY": "synthetic-not-a-key"}
            ),
        ):
            result = subject.execute(self.value, self.directory)
        self.assertEqual(result["status"], "OWNER_IMAGE_BUILT")
        self.assertEqual(result["native_image_id"], self.identity)
        self.assertFalse(result["image_admitted"])
        self.assertFalse(result["owner_readiness_verified"])
        self.assertFalse(list(self.directory.glob("build-source-*")))
        for path in self.directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_drift_opaque_cross_owner_and_traversal_fail_before_docker(self) -> None:
        mutations = [
            {"source_sha256": "sha256:" + "0" * 64},
            {"context": "../foreign"},
            {"context": "."},
            {"target": "--push"},
            {"platform": "linux/ppc64le"},
            {"inputs": {"/private/secret": "0" * 64}},
        ]
        for change in mutations:
            with (
                self.subTest(change=change),
                mock.patch.object(subject, "docker") as docker,
            ):
                with self.assertRaises(node.ExecutionError):
                    subject.execute({**self.value, **change}, self.directory)
                docker.assert_not_called()
        catalog = self.owner / "deployment-catalog.json"
        data = json.loads(catalog.read_text())
        data["roles"][0]["opaque"] = True
        catalog.write_text(json.dumps(data))
        self.value["owner_files"] = self.manifest(self.owner)
        with self.assertRaisesRegex(
            node.ExecutionError, "OWNER_BUILD_OPAQUE_FORBIDDEN"
        ):
            subject.definition(self.value)

    def test_secret_unknown_or_credential_url_arguments_are_refused(self) -> None:
        for arguments in (
            {"API_KEY": "synthetic-not-a-key"},
            {"UNKNOWN": "value"},
            {"PUBLIC_ORIGIN": "https://operator:password@localhost"},
        ):
            with (
                self.subTest(arguments=arguments),
                self.assertRaisesRegex(
                    node.ExecutionError, "OWNER_BUILD_ARGUMENTS_INVALID"
                ),
            ):
                subject.definition({**self.value, "build_arguments": arguments})

    def test_architecture_mismatch_never_builds(self) -> None:
        with (
            mock.patch.object(subject, "docker", return_value=b"linux/x86_64\n"),
            mock.patch.object(subject.subprocess, "run") as build,
        ):
            with self.assertRaisesRegex(
                node.ExecutionError, "OWNER_BUILD_NATIVE_PLATFORM_REQUIRED"
            ):
                subject.execute(self.value, self.directory)
            build.assert_not_called()

    def test_copy_rechecks_pinned_bytes(self) -> None:
        (self.owner / "application/app").write_text("changed bytes")
        with self.assertRaisesRegex(node.ExecutionError, "OWNER_BUILD_SOURCE_DRIFT"):
            subject.snapshot(
                self.owner, self.value["owner_files"], "application", self.directory
            )

    def test_private_operator_umask_does_not_make_public_source_unreadable(
        self,
    ) -> None:
        output = self.directory / "snapshot"
        output.mkdir(mode=0o700)
        previous = subject.os.umask(0o077)
        try:
            subject.snapshot(
                self.owner, self.value["owner_files"], "application", output
            )
        finally:
            subject.os.umask(previous)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((output / "package").stat().st_mode & 0o777, 0o755)
        self.assertEqual((output / "package/module.py").stat().st_mode & 0o777, 0o644)

    def test_known_failure_and_unknown_effect_remain_distinct(self) -> None:
        with (
            mock.patch.object(subject, "docker", side_effect=self.observations),
            mock.patch.object(
                subject.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 7),
            ),
        ):
            self.assertEqual(
                subject.execute(self.value, self.directory)["owner_exit_code"], 7
            )
        with (
            mock.patch.object(subject, "docker", side_effect=self.observations),
            mock.patch.object(
                subject.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("docker", 6600),
            ),
        ):
            self.assertEqual(
                subject.execute(self.value, self.directory)["status"], "RESULT_UNKNOWN"
            )

    def test_node_binding_and_typed_projection_require_the_same_build_definition(
        self,
    ) -> None:
        definition = self.root / "definition.json"
        definition.write_bytes(node.encode(self.value))
        definition.chmod(0o600)
        request = {
            "schema_version": 1,
            "operation": str(uuid.uuid4()),
            "source_root": str(self.executor),
            "source_files": self.value["executor_files"],
            "state_root": str(self.root / "node-journal"),
            "procedure": "owner-build",
            "arguments": [
                "build",
                "--request",
                str(definition),
                "--request-sha256",
                node.checksum(definition.read_bytes()),
            ],
            "inputs": {str(definition): node.checksum(definition.read_bytes())},
            "environment": {},
            "timeout_seconds": 7000,
        }
        self.assertEqual(node.validate(request)[2][1:3], ["-m", "tools.owner_build"])
        changed = copy.deepcopy(request)
        changed["arguments"][0] = "start"
        with self.assertRaises(node.ExecutionError):
            node.validate(changed)
        with (
            mock.patch.object(subject, "docker", side_effect=self.observations),
            mock.patch.object(subject.subprocess, "run", side_effect=self.build),
        ):
            result = subject.execute(self.value, self.directory)
        path = self.root / "reply.json"
        path.write_text(json.dumps({**result, "raw_private": "must-be-excluded"}))
        projection = node.owner_projection(path)
        self.assertNotIn("raw_private", projection)
        del result["context_sha256"]
        path.write_text(json.dumps(result))
        self.assertIsNone(node.owner_projection(path))


if __name__ == "__main__":
    unittest.main()
