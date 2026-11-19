"""Protect native owner delegation, receipt-bound observations and uncertain effects."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tools import spire_runtime as subject

_INVOKE = subject.invoke


class SpireRuntimeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.source = self.root / "source"
        (self.source / "tools").mkdir(parents=True)
        for name in [
            "node_execution.py",
            "node_artifact.py",
            "owner_compose.py",
            "spire_runtime.py",
        ]:
            (self.source / "tools" / name).write_bytes(
                (Path(subject.__file__).parent / name).read_bytes()
            )
        (self.source / "spire").mkdir()
        for name in ["compose.yml", "install.sh", "export.sh"]:
            (self.source / "spire" / name).write_text("# pinned owner entrypoint\n")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        (self.workspace / "secrets").mkdir(mode=0o700)
        self.initial = self.workspace / "secrets/native"
        self.initial.mkdir(mode=0o700)
        self.env = self.workspace / ".env"
        self.write(self.env, b"PUBLIC_ENDPOINT=https://separate-node.example:28081\n")
        self.images = ["sha256:" + letter * 64 for letter in ["a", "b", "c"]]
        self.value = {
            "schema_version": 1,
            "executor_root": str(self.source),
            "owner_root": str(self.source),
            "catalog": "deployment-catalog.json",
            "project_directory": str(self.workspace),
            "project": "native-qualification",
            "env_file": str(self.env),
            "recipes": ["spire/compose.yml"],
            "configuration_sha256": None,
            "expected_runtime": None,
            "native_lifecycle": {
                "kind": "spire",
                "runtime_image": self.images[0],
                "installer_image": self.images[1],
                "input_directory": str(self.initial),
                "server": {},
            },
        }
        self.commands = []
        self.installed = False
        self.running = True
        self.started_at = "2026-11-19T00:00:00Z"
        self.container = "d" * 64
        self.change_input_after_install = False
        self.component("server")
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {subject.APPROVAL: subject.APPROVAL_VALUE}).start()
        self.operation("initial")
        mock.patch.object(subject.owner, "call", side_effect=self.docker).start()
        mock.patch.object(subject, "invoke", side_effect=self.native).start()

    def write(self, path, data):
        path.write_bytes(data)
        path.chmod(0o600)

    def component(self, name):
        self.value["role"] = "platform-infrastructure/spire-" + name
        self.value["image"] = (
            self.images[2] if name == "telemetry-sender" else self.images[0]
        )
        self.value["native_lifecycle"]["server"] = (
            {"bind_address": "172.16.0.1", "public_port": 28081}
            if name == "server"
            else {}
        )
        for path in self.initial.iterdir():
            path.unlink()
        path = self.initial / ("plan.json" if name == "server" else "component.json")
        self.write(
            path,
            json.dumps(
                {
                    "owner": "infrastructure-spire",
                    "component": name,
                    "bind_address": "172.16.0.1",
                    "public_port": 28081,
                    "observation": {"sender_image": self.images[2]},
                }
            ).encode(),
        )
        self.value["inputs"] = {
            str(p): subject.owner.digest(p.read_bytes()) for p in [self.env, path]
        }
        (self.source / "deployment-catalog.json").write_text(
            json.dumps(
                {
                    "product": "platform-infrastructure",
                    "roles": [
                        {
                            "id": "spire-" + name,
                            "owner": "spire",
                            "kind": "runtime",
                            "opaque": False,
                            "recipes": ["spire/compose.yml"],
                        }
                    ],
                }
            )
        )
        files = {
            str(p.relative_to(self.source)): subject.owner.digest(p.read_bytes())
            for p in self.source.rglob("*")
            if p.is_file()
        }
        self.value.update(executor_files=files, owner_files=files)

    def operation(self, name):
        self.operation_dir = self.root / ("operation-" + name)
        self.operation_dir.mkdir(mode=0o700)
        os.environ["AELEV_NODE_OPERATION_DIRECTORY"] = str(self.operation_dir)

    def docker(self, args):
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps(
                [{"Id": args[3], "Os": "linux", "Architecture": "arm64"}]
            ).encode()
        if args[:3] == ["docker", "volume", "ls"]:
            return (
                (self.value["project"] + "_installer\n").encode()
                if self.installed
                else b""
            )
        if args[:2] == ["docker", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": self.container,
                        "Image": self.value["image"],
                        "RestartCount": 0,
                        "Config": {
                            "Labels": {
                                "com.docker.compose.project": self.value["project"],
                                "com.docker.compose.service": self.value["role"].split(
                                    "/"
                                )[1],
                            }
                        },
                        "HostConfig": {"RestartPolicy": {"Name": "no"}},
                        "State": {
                            "Running": self.running,
                            "Paused": False,
                            "Restarting": False,
                            "StartedAt": self.started_at,
                            "Health": {"Status": "healthy"},
                        },
                    }
                ]
            ).encode()
        raise AssertionError(args)

    def native(self, args, **kwargs):
        self.commands.append((args, kwargs))
        if args[1].endswith("install.sh"):
            self.installed = True
            if self.change_input_after_install:
                path = self.initial / "plan.json"
                path.write_bytes(b'{"changed":true}')
            return b""
        self.assertTrue(args[1].endswith("export.sh"))
        directory = Path(args[4]) / "runtime/installed-composition"
        directory.mkdir(parents=True)
        (directory / "receipt.json").write_text(
            json.dumps(
                {
                    "generation": "e" * 64,
                    self.value["role"].split("/")[1]: {
                        "container_id": self.container,
                        "image": self.value["image"],
                        "native_restarts": 0,
                        "restart": "no",
                        "compose_project": self.value["project"],
                    },
                }
            )
        )
        return b""

    def planned(self):
        planned = subject.execute(self.value, "plan")
        self.value["configuration_sha256"] = planned["configuration_sha256"]
        return planned

    def started(self):
        self.planned()
        result = subject.execute(self.value, "start")
        self.value["expected_runtime"] = {
            k: result[k]
            for k in [
                "configuration_sha256",
                "container_id",
                "native_image_id",
                "incarnation_sha256",
            ]
        }
        return result

    def test_plan_validates_inputs_without_native_effect_or_new_binding_store(self):
        self.assertEqual(self.planned()["status"], "OWNER_NATIVE_PLANNED")
        self.assertEqual(self.commands, [])
        self.assertEqual(
            sorted(p.name for p in self.workspace.iterdir()), [".env", "secrets"]
        )

    def test_start_delegates_exact_owner_command_and_status_uses_original_incarnation(
        self,
    ):
        result = self.started()
        self.assertEqual(result["status"], "OWNER_ROLE_RUNNING")
        args, options = self.commands[0]
        self.assertEqual(
            args[2:],
            [
                "server",
                self.value["project"],
                *self.images[:2],
                "--native-restart-policy",
                "no",
                "--bind-address",
                "172.16.0.1",
                "--public-port",
                "28081",
            ],
        )
        self.assertTrue(options["initial"].is_relative_to(self.operation_dir))
        self.assertEqual(options["initial"].stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (options["initial"] / "plan.json").read_bytes(),
            (self.initial / "plan.json").read_bytes(),
        )
        self.assertTrue(options["effectful"])
        self.operation("status")
        self.assertEqual(subject.execute(self.value, "status"), result)
        self.assertEqual(
            sum(args[1].endswith("install.sh") for args, _ in self.commands), 1
        )
        self.assertFalse((self.workspace / ".spire-native-binding.json").exists())

    def test_each_independent_observation_role_uses_only_its_canonical_installer(self):
        for name in ["observer", "status-publisher", "telemetry-sender"]:
            with self.subTest(component=name):
                self.component(name)
                self.value["configuration_sha256"] = None
                self.value["expected_runtime"] = None
                self.installed = False
                self.operation(name)
                self.started()
                command = [
                    args for args, _ in self.commands if args[1].endswith("install.sh")
                ][-1]
                self.assertEqual(command[2], name)
                self.assertEqual(command[-2:], ["--native-restart-policy", "no"])

    def test_status_of_stopped_owner_does_not_restart_it(self):
        self.started()
        self.running = False
        self.operation("stopped")
        self.assertEqual(
            subject.execute(self.value, "status")["status"], "OWNER_ROLE_STOPPED"
        )
        self.assertEqual(
            sum(args[1].endswith("install.sh") for args, _ in self.commands), 1
        )

    def test_changed_container_or_manual_restart_requires_handoff(self):
        self.started()
        self.started_at = "2026-11-19T00:01:00Z"
        self.operation("restarted")
        with self.assertRaisesRegex(subject.node.ExecutionError, "INCARNATION_HANDOFF"):
            subject.execute(self.value, "status")

    def test_status_requires_authoritative_start_projection(self):
        self.planned()
        with self.assertRaisesRegex(
            subject.node.ExecutionError, "START_RECEIPT_REQUIRED"
        ):
            subject.execute(self.value, "status")
        self.assertEqual(self.commands, [])

    def test_existing_unbound_native_installation_cannot_be_retried_or_adopted(self):
        self.planned()
        self.installed = True
        with self.assertRaisesRegex(subject.node.ExecutionError, "UNBOUND_HANDOFF"):
            subject.execute(self.value, "start")
        self.assertEqual(self.commands, [])

    def test_wrong_role_image_and_sender_plan_are_refused_before_installation(self):
        self.value["image"] = self.images[1]
        with self.assertRaisesRegex(subject.node.ExecutionError, "ROLE_IMAGE_MISMATCH"):
            self.planned()
        self.component("telemetry-sender")
        self.value["image"] = self.images[0]
        with self.assertRaisesRegex(subject.node.ExecutionError, "ROLE_IMAGE_MISMATCH"):
            self.planned()
        self.assertEqual(self.commands, [])

    def test_undeclared_or_changed_initial_input_is_refused_before_installation(self):
        self.write(self.initial / "undeclared", b"fixture\n")
        with self.assertRaisesRegex(subject.node.ExecutionError, "UNDECLARED_INPUT"):
            self.planned()
        (self.initial / "undeclared").unlink()
        (self.initial / "plan.json").write_bytes(b"changed\n")
        with self.assertRaisesRegex(subject.node.ExecutionError, "INPUT_DRIFT"):
            self.planned()
        self.assertEqual(self.commands, [])

    def test_source_drift_is_refused_before_installation(self):
        (self.source / "spire/install.sh").write_text("changed\n")
        with self.assertRaisesRegex(subject.node.ExecutionError, "SOURCE_DRIFT"):
            self.planned()
        self.assertEqual(self.commands, [])

    def test_post_effect_input_change_is_unknown_without_false_success(self):
        self.planned()
        self.change_input_after_install = True
        with self.assertRaises(subject.owner.EffectUnknown):
            subject.execute(self.value, "start")
        self.assertTrue(self.installed)
        self.assertFalse((self.workspace / ".spire-native-binding.json").exists())

    def test_symlink_and_open_private_inputs_are_refused(self):
        target = self.initial / "plan.json"
        target.chmod(0o644)
        with self.assertRaisesRegex(subject.node.ExecutionError, "PRIVATE_PERMISSIONS"):
            self.planned()
        target.chmod(0o600)
        link = self.initial / "link"
        link.symlink_to(target)
        with self.assertRaisesRegex(subject.node.ExecutionError, "SYMLINK"):
            self.planned()

    def test_no_credential_or_unrelated_environment_reaches_native_child(self):
        # Exercise the real invocation helper rather than its native-owner fixture.
        with mock.patch.object(
            subject.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, b"", b""),
        ) as run:
            with mock.patch.dict(
                os.environ,
                {"UPSTREAM_API_KEY": "fixture-not-secret", "PYTHONPATH": "/untrusted"},
            ):
                _INVOKE(["sh", "pinned-entrypoint"], initial=self.initial)
            environment = run.call_args.kwargs["env"]
            self.assertNotIn("UPSTREAM_API_KEY", environment)
            self.assertNotIn("PYTHONPATH", environment)
            self.assertEqual(environment[subject.APPROVAL], subject.APPROVAL_VALUE)

    def test_native_effect_timeout_is_unknown(self):
        with mock.patch.object(
            subject.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("native", 600),
        ):
            with self.assertRaises(subject.owner.EffectUnknown):
                _INVOKE(["sh", "pinned-entrypoint"], effectful=True)

    def test_server_plan_cannot_override_declared_published_bind_and_port(self):
        self.value["native_lifecycle"]["server"]["public_port"] = 28082
        with self.assertRaisesRegex(
            subject.node.ExecutionError, "SERVER_PLAN_MISMATCH"
        ):
            self.planned()
        self.assertEqual(self.commands, [])

    def test_native_node_projection_requires_complete_incarnation_and_excludes_raw_data(
        self,
    ):
        projection = self.started()
        path = self.root / "projection.json"
        self.write(
            path,
            json.dumps({**projection, "raw_private": "fixture-private-value"}).encode(),
        )
        verified = subject.node.owner_projection(path, native=True)
        self.assertIsNotNone(verified)
        self.assertNotIn("raw_private", verified)
        del projection["incarnation_sha256"]
        self.write(path, json.dumps(projection).encode())
        self.assertIsNone(subject.node.owner_projection(path, native=True))

    def test_native_node_procedure_keeps_literal_action_and_input_guards(self):
        import uuid

        path = self.root / "native-request.json"
        self.write(path, json.dumps(self.value).encode())
        request = {
            "schema_version": 1,
            "operation": str(uuid.uuid4()),
            "source_root": str(self.source),
            "source_files": self.value["executor_files"],
            "state_root": str(self.root / "node-state"),
            "procedure": "spire-runtime",
            "arguments": [
                "plan",
                "--request",
                str(path),
                "--request-sha256",
                subject.owner.digest(path.read_bytes()),
            ],
            "inputs": {
                **self.value["inputs"],
                str(path): subject.owner.digest(path.read_bytes()),
            },
            "environment": {subject.APPROVAL: subject.APPROVAL_VALUE},
            "timeout_seconds": 1800,
        }
        _, _, command = subject.node.validate(request)
        self.assertIn("tools.spire_runtime", command)
        request["arguments"][0] = "task"
        with self.assertRaisesRegex(
            subject.node.ExecutionError, "CONFIGURATION_BINDING"
        ):
            subject.node.validate(request)

    def test_prepared_nested_directories_are_repacked_privately_without_permission_rewrite(
        self,
    ):
        (self.workspace / "secrets").chmod(0o755)
        self.initial.chmod(0o755)
        self.started()
        self.assertEqual(self.workspace.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.initial.stat().st_mode & 0o777, 0o755)
        args, options = next(
            row for row in self.commands if row[0][1].endswith("install.sh")
        )
        self.assertEqual(options["initial"].stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (options["initial"] / "plan.json").stat().st_mode & 0o777, 0o600
        )

    def test_initial_directory_with_untrusted_writer_is_rejected_before_native_effect(
        self,
    ):
        self.initial.chmod(0o775)
        with self.assertRaisesRegex(
            subject.node.ExecutionError, "INPUT_DIRECTORY_PERMISSIONS"
        ):
            self.planned()
        self.assertEqual(self.commands, [])
