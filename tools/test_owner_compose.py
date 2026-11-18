"""Prove selected-role execution, immutable configuration and handoff boundaries."""

from __future__ import annotations

import copy
import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid
import subprocess

from tools import node_execution as node
from tools import owner_compose as subject


class OwnerComposeTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.source = self.root / "source"
        (self.source / "tools").mkdir(parents=True)
        for name in ("owner_compose.py", "node_execution.py"):
            (self.source / "tools" / name).write_bytes(
                (Path(__file__).parent / name).read_bytes()
            )
        self.owner = self.root / "owner"
        self.owner.mkdir()
        (self.owner / "compose.yml").write_text("services: {}\n")
        self.catalog = {
            "product": "qualification",
            "roles": [
                {
                    "id": "worker",
                    "kind": "runtime",
                    "owner": ".",
                    "opaque": False,
                    "recipes": ["compose.yml"],
                }
            ],
        }
        (self.owner / "deployment-catalog.json").write_text(json.dumps(self.catalog))
        workspace = self.root / "workspace"
        workspace.mkdir(mode=0o700)
        self.env = self.root / "owner.env"
        self.env.write_text("OWNER_SETTING=original\n")
        self.env.chmod(0o600)
        self.image = "registry.invalid/qualification@sha256:" + "a" * 64
        self.image_id = "sha256:" + "b" * 64
        self.config_hash = "c" * 64
        self.value = {
            "schema_version": 1,
            "executor_root": str(self.source),
            "executor_files": self.source_files(),
            "owner_root": str(self.owner),
            "owner_files": self.owner_files(),
            "catalog": "deployment-catalog.json",
            "role": "qualification/worker",
            "recipes": ["compose.yml"],
            "project_directory": str(workspace),
            "project": "owner-qualification",
            "env_file": str(self.env),
            "inputs": {str(self.env): subject.digest(self.env.read_bytes())},
            "image": self.image,
            "configuration_sha256": self.config_hash,
        }
        self.model = {
            "services": {
                "worker": {
                    "image": self.image,
                    "restart": "no",
                    "environment": {"OWNER_SETTING": "original"},
                }
            }
        }
        self.rows = []
        self.mutations = []
        self.commands = []

    def source_files(self):
        return {
            str(path.relative_to(self.source)): subject.digest(path.read_bytes())
            for path in self.source.rglob("*")
            if path.is_file()
        }

    def owner_files(self):
        return {
            str(path.relative_to(self.owner)): subject.digest(path.read_bytes())
            for path in self.owner.rglob("*")
            if path.is_file()
        }

    def row(self):
        return {
            "Id": "d" * 64,
            "Image": self.image_id,
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": self.value["project"],
                    "com.docker.compose.service": "worker",
                    "com.docker.compose.config-hash": self.config_hash,
                    "com.docker.compose.oneoff": "False",
                }
            },
            "State": {
                "Running": True,
                "Paused": False,
                "Restarting": False,
                "Health": {"Status": "healthy"},
            },
        }

    def docker(self, command, **kwargs):
        self.commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": self.image_id}]).encode()
        if command[:2] == ["docker", "ps"]:
            return self.rows[0]["Id"].encode() if self.rows else b""
        if command[:2] == ["docker", "inspect"]:
            return json.dumps(self.rows).encode()
        if "config" in command and "--format" in command:
            return json.dumps(self.model).encode()
        snapshot = Path(command[command.index("--file") + 1])
        self.assertNotEqual(snapshot, self.owner / "compose.yml")
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(snapshot.read_text()), self.model)
        self.assertEqual(
            Path(command[command.index("--env-file") + 1]).read_bytes(), b""
        )
        if "config" in command:
            return ("worker " + self.config_hash + "\n").encode()
        if "up" in command:
            self.mutations.append(command)
            self.rows = [self.row()]
            return b""
        if "run" in command:
            self.mutations.append(command)
            return b""
        raise AssertionError(command)

    def run_action(self, action):
        with mock.patch.object(subject, "call", side_effect=self.docker):
            return subject.execute(self.value, action)

    def test_plan_does_not_contact_image_store_or_start_any_role(self):
        self.value["configuration_sha256"] = None
        result = self.run_action("plan")
        self.assertEqual(result["configuration_sha256"], self.config_hash)
        self.assertEqual(result["status"], "OWNER_COMPOSE_PLANNED")
        self.assertEqual(self.mutations, [])
        self.assertEqual(len(self.commands), 2)

    def test_profiled_role_is_rendered_before_selected_role_execution(self):
        self.model["services"]["worker"]["profiles"] = ["provision"]

        def profiled_docker(command, **kwargs):
            if (
                "config" in command
                and "--hash" in command
                and "--profile" not in command
            ):
                return b""
            if "config" in command and "--format" in command:
                if (
                    "--profile" not in command
                    or command[command.index("--profile") + 1] != "*"
                ):
                    return b'{"services":{}}'
            return self.docker(command, **kwargs)

        with mock.patch.object(subject, "call", side_effect=profiled_docker):
            result = subject.execute(self.value, "start")
        self.assertEqual(result["status"], "OWNER_ROLE_RUNNING")
        self.assertEqual(len(self.mutations), 1)
        self.assertEqual(self.mutations[0][-1], "worker")
        self.assertIn("--no-deps", self.mutations[0])
        self.assertNotIn("--profile", self.mutations[0])

    def test_executor_and_owner_have_independent_source_generations(self):
        command, role = subject.definition(self.value)
        self.assertNotEqual(self.value["executor_root"], self.value["owner_root"])
        self.assertNotIn("tools/owner_compose.py", self.value["owner_files"])
        self.assertEqual(role["id"], "worker")
        self.assertEqual(command[-1], str(self.owner / "compose.yml"))
        (self.source / "tools/node_execution.py").write_text("changed executor\n")
        with self.assertRaisesRegex(node.ExecutionError, "OWNER_COMPOSE_SOURCE_DRIFT"):
            subject.definition(self.value)

    def test_start_and_repeat_only_touch_selected_role_once(self):
        first = self.run_action("start")
        second = self.run_action("start")
        self.assertEqual(first, second)
        self.assertEqual(len(self.mutations), 1)
        self.assertEqual(self.mutations[0][-1], "worker")
        for flag in ("--no-deps", "--no-build", "--no-recreate"):
            self.assertIn(flag, self.mutations[0])
        self.assertFalse(first["owner_readiness_verified"])
        self.assertFalse(first["queue_resume_issued"])

    def test_stopped_existing_role_is_not_resumed(self):
        self.rows = [self.row()]
        self.rows[0]["State"]["Running"] = False
        with self.assertRaisesRegex(
            node.ExecutionError, "OWNER_COMPOSE_OWNER_HANDOFF_REQUIRED"
        ):
            self.run_action("start")
        self.assertEqual(self.mutations, [])
        self.assertEqual(self.run_action("status")["status"], "OWNER_ROLE_STOPPED")

    def test_native_restart_change_requires_handoff(self):
        self.rows = [self.row()]
        self.rows[0]["HostConfig"]["RestartPolicy"]["Name"] = "always"
        with self.assertRaisesRegex(
            node.ExecutionError, "OWNER_COMPOSE_NATIVE_RESTART_REFUSED"
        ):
            self.run_action("start")
        self.assertEqual(self.mutations, [])

    def test_container_image_config_and_oneoff_drift_refuse_replacement(self):
        for change in ("image", "config", "oneoff"):
            self.rows = [self.row()]
            if change == "image":
                self.rows[0]["Image"] = "sha256:" + "e" * 64
            else:
                key = "com.docker.compose." + (
                    "config-hash" if change == "config" else "oneoff"
                )
                self.rows[0]["Config"]["Labels"][key] = "different"
            with self.subTest(change=change), self.assertRaises(node.ExecutionError):
                self.run_action("start")
        self.assertEqual(self.mutations, [])

    def test_configuration_mismatch_prevents_any_container_effect(self):
        self.value["configuration_sha256"] = "f" * 64
        with self.assertRaisesRegex(
            node.ExecutionError, "OWNER_COMPOSE_CONFIGURATION_DRIFT"
        ):
            self.run_action("start")
        self.assertEqual(self.mutations, [])

    def test_private_input_drift_is_rejected_before_compose(self):
        self.env.write_text("OWNER_SETTING=changed\n")
        with mock.patch.object(subject, "call") as call:
            with self.assertRaisesRegex(
                node.ExecutionError, "OWNER_COMPOSE_INPUT_DRIFT"
            ):
                subject.execute(self.value, "start")
        call.assert_not_called()

    def test_source_drift_is_rejected_before_compose(self):
        (self.owner / "compose.yml").write_text("changed\n")
        with mock.patch.object(subject, "call") as call:
            with self.assertRaisesRegex(
                node.ExecutionError, "OWNER_COMPOSE_SOURCE_DRIFT"
            ):
                subject.execute(self.value, "start")
        call.assert_not_called()

    def test_unregistered_recipe_and_mutable_image_are_rejected(self):
        for change in ("recipe", "image"):
            value = copy.deepcopy(self.value)
            if change == "recipe":
                value["recipes"] = ["unregistered.yml"]
            else:
                value["image"] = "registry.invalid/qualification:latest"
            with self.subTest(change=change), self.assertRaises(node.ExecutionError):
                subject.execute(value, "start")

    def test_restart_and_shared_service_runtime_are_rejected(self):
        for change in (
            {"restart": "always"},
            {"pull_policy": "build"},
            {"network_mode": "service:other"},
            {"volumes_from": ["other"]},
        ):
            previous = copy.deepcopy(self.model)
            self.model["services"]["worker"].update(change)
            with self.subTest(change=change), self.assertRaises(node.ExecutionError):
                self.run_action("start")
            self.model = previous
        self.assertEqual(self.mutations, [])

    def test_task_requires_operation_and_uses_its_unmodified_command(self):
        with self.assertRaisesRegex(
            node.ExecutionError, "OWNER_COMPOSE_ACTION_KIND_MISMATCH"
        ):
            self.run_action("task")
        self.catalog["roles"][0]["kind"] = "operation"
        (self.owner / "deployment-catalog.json").write_text(json.dumps(self.catalog))
        self.value["owner_files"] = self.owner_files()
        result = self.run_action("task")
        self.assertEqual(result["status"], "OWNER_TASK_COMPLETED")
        self.assertEqual(self.mutations[0][-1], "worker")
        self.assertIn("--no-deps", self.mutations[0])
        self.assertIn("--rm", self.mutations[0])
        self.assertIn("-T", self.mutations[0])
        self.assertNotIn("--no-tty", self.mutations[0])
        self.assertNotIn("--build", self.mutations[0])

    def test_env_edit_after_render_cannot_change_frozen_model(self):
        original = self.docker

        def editing_docker(command, **kwargs):
            data = original(command)
            if "--format" in command:
                self.env.write_text("OWNER_SETTING=concurrent-edit\n")
            return data

        with mock.patch.object(subject, "call", side_effect=editing_docker):
            subject.execute(self.value, "start")
        self.assertEqual(len(self.mutations), 1)

    def test_node_binds_exact_public_source_and_private_definition(self):
        path = self.root / "definition.json"
        path.write_text(json.dumps(self.value))
        path.chmod(0o600)
        request_hash = subject.digest(path.read_bytes())
        request = {
            "schema_version": 1,
            "operation": str(uuid.uuid4()),
            "source_root": str(self.source),
            "source_files": self.value["executor_files"],
            "state_root": str(self.root / "state"),
            "procedure": "owner-compose",
            "arguments": [
                "plan",
                "--request",
                str(path),
                "--request-sha256",
                request_hash,
            ],
            "inputs": {**self.value["inputs"], str(path): request_hash},
            "environment": {},
            "timeout_seconds": 60,
        }
        _, _, command = node.validate(request)
        self.assertIn("tools.owner_compose", command)
        changed = copy.deepcopy(request)
        changed["source_files"] = {**request["source_files"], "untrusted": "a" * 64}
        with self.assertRaisesRegex(
            node.ExecutionError, "NODE_COMPOSE_SOURCE_OR_INPUT_BINDING_MISMATCH"
        ):
            node.target(changed, "execute")
        self.assertFalse((self.root / "state").exists())
        request["arguments"][-1] = "a" * 64
        with self.assertRaisesRegex(
            node.ExecutionError, "NODE_CONFIGURATION_BINDING_REQUIRED"
        ):
            node.validate(request)

    def test_failed_command_retains_exact_exit_without_exporting_diagnostics(self):
        with mock.patch.object(
            subject.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["docker"], 7, stdout=b"private-stdout", stderr=b"private-stderr"
            ),
        ):
            with self.assertRaises(subject.CommandFailure) as failure:
                subject.call(["docker"])
        self.assertEqual(failure.exception.exit_code, 7)
        self.assertNotIn("private", str(failure.exception))
        self.assertEqual(failure.exception.stderr, b"private-stderr")

    def test_effectful_timeout_is_unknown_instead_of_invalid_input(self):
        with mock.patch.object(
            subject.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["private-command"], 600),
        ):
            with self.assertRaises(subject.EffectUnknown) as failure:
                subject.call(["docker", "compose", "up"], effectful=True)
        self.assertNotIn("private-command", str(failure.exception))

    def test_public_owner_projection_excludes_private_fields(self):
        path = self.root / "owner-output.json"
        path.write_text(
            json.dumps(
                {
                    "status": "OWNER_COMPOSE_PLANNED",
                    "configuration_sha256": "a" * 64,
                    "api_key": "private-value",
                    "raw_diagnostics": "private-output",
                }
            )
        )
        projection = node.owner_projection(path)
        self.assertEqual(
            projection,
            {"status": "OWNER_COMPOSE_PLANNED", "configuration_sha256": "a" * 64},
        )
        self.assertNotIn("private", json.dumps(projection))
        path.write_text(
            json.dumps(
                {"status": "OWNER_ROLE_RUNNING", "container_id": "private-value"}
            )
        )
        self.assertIsNone(node.owner_projection(path))

    def test_unknown_owner_exit_is_retained_without_reexecution(self):
        path = self.root / "definition.json"
        path.write_text(json.dumps(self.value))
        path.chmod(0o600)
        request_hash = subject.digest(path.read_bytes())
        request = {
            "schema_version": 1,
            "operation": str(uuid.uuid4()),
            "source_root": str(self.source),
            "source_files": self.value["executor_files"],
            "state_root": str(self.root / "state"),
            "procedure": "owner-compose",
            "arguments": [
                "start",
                "--request",
                str(path),
                "--request-sha256",
                request_hash,
            ],
            "inputs": {**self.value["inputs"], str(path): request_hash},
            "environment": {},
            "timeout_seconds": 60,
        }
        with mock.patch.object(
            node.subprocess, "run", return_value=subprocess.CompletedProcess([], 3)
        ) as run:
            first = node.target(request, "execute")
            second = node.target(request, "execute")
        self.assertEqual(first["status"], "RESULT_UNKNOWN")
        self.assertEqual(first, second)
        run.assert_called_once()

    def test_private_command_diagnostics_are_saved_only_inside_node_operation(self):
        request = self.root / "definition.json"
        request.write_text(json.dumps(self.value))
        request.chmod(0o600)
        operation = self.root / "operation"
        operation.mkdir(mode=0o700)
        stdout = io.StringIO()
        with (
            mock.patch(
                "sys.argv",
                [
                    "owner-compose",
                    "start",
                    "--request",
                    str(request),
                    "--request-sha256",
                    subject.digest(request.read_bytes()),
                ],
            ),
            mock.patch.object(
                subject,
                "execute",
                side_effect=subject.CommandFailure(
                    7, b"private-output", b"private-error"
                ),
            ),
            mock.patch.dict(
                os.environ, {"AELEV_NODE_OPERATION_DIRECTORY": str(operation)}
            ),
            mock.patch("sys.stdout", stdout),
        ):
            self.assertEqual(subject.main(), 2)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["owner_exit_code"], 7)
        self.assertTrue(result["command_diagnostics_retained_privately"])
        self.assertNotIn("private-error", stdout.getvalue())
        path = operation / "owner-command-failure.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())["stderr"], "private-error")


if __name__ == "__main__":
    unittest.main()
