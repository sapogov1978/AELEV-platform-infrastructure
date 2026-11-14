"""Actual owner-process effects and fail-closed node execution boundaries."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

from tools import node_execution as node


class NodeExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.entrypoint = self.source / "translator-service/compute-runtime/station.py"
        self.entrypoint.parent.mkdir(parents=True)
        self.counter = self.root / "counter"
        self.entrypoint.write_text(
            "from pathlib import Path\n"
            + "p=Path("
            + repr(str(self.counter))
            + ")\n"
            + "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\n"
            + "print('owner-private-output')\n"
        )
        helper = self.source / "tools/node_execution.py"
        helper.parent.mkdir()
        helper.write_bytes(Path(node.__file__).read_bytes())
        config = self.root / "worker.env"
        config.write_text("private-input")
        config.chmod(0o600)
        self.request = {
            "schema_version": 1,
            "operation": str(uuid.uuid4()),
            "source_root": str(self.source),
            "source_files": {
                str(p.relative_to(self.source)): node.checksum(p.read_bytes())
                for p in (self.entrypoint, helper)
            },
            "state_root": str(self.root / "state"),
            "procedure": "translator-station",
            "arguments": [
                "start",
                "--role",
                "worker",
                "--env-file",
                str(self.root / "worker.env"),
            ],
            "inputs": {str(config): node.checksum(config.read_bytes())},
            "environment": {},
            "timeout_seconds": 10,
        }

    def test_native_process_runs_once_and_output_remains_private(self) -> None:
        first = node.invoke({"kind": "local"}, self.request, "execute")
        second = node.invoke({"kind": "local"}, self.request, "execute")
        self.assertEqual(first, second)
        self.assertEqual(self.counter.read_text(), "1")
        self.assertEqual(first["status"], "COMMAND_SUCCEEDED")
        self.assertFalse(first["owner_readiness_verified"])
        self.assertNotIn("owner-private-output", json.dumps(first))
        folder = self.root / "state" / self.request["operation"]
        self.assertEqual((folder / "stdout").read_text(), "owner-private-output\n")
        for name in ("receipt.json", "stdout", "stderr"):
            self.assertEqual((folder / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual((folder.parent / "scope.lock").stat().st_mode & 0o777, 0o600)

    def test_status_of_absent_operation_creates_nothing(self) -> None:
        result = node.invoke({"kind": "local"}, self.request, "status")
        self.assertEqual(result["status"], "NOT_OBSERVED")
        self.assertFalse((self.root / "state").exists())

    def test_same_operation_with_different_scope_never_reexecutes(self) -> None:
        node.target(self.request, "execute")
        changed = copy.deepcopy(self.request)
        changed["arguments"][2] = "provider"
        with self.assertRaisesRegex(node.ExecutionError, "NODE_OPERATION_CONFLICT"):
            node.target(changed, "execute")
        self.assertEqual(self.counter.read_text(), "1")

    def test_source_drift_is_refused_before_intent_or_owner_process(self) -> None:
        self.entrypoint.write_text("raise SystemExit(0)")
        with self.assertRaisesRegex(node.ExecutionError, "NODE_SOURCE_DRIFT"):
            node.target(self.request, "execute")
        self.assertFalse(self.counter.exists())
        self.assertFalse(list((self.root / "state").rglob("receipt.json")))

    def test_config_drift_is_refused_before_owner_process(self) -> None:
        config = self.root / "worker.env"
        config.write_text("private-input")
        config.chmod(0o600)
        self.request["inputs"] = {str(config): node.checksum(b"other-input")}
        with self.assertRaisesRegex(node.ExecutionError, "NODE_INPUT_DRIFT"):
            node.target(self.request, "execute")
        self.assertFalse(self.counter.exists())

    def test_interrupted_intent_is_not_repeated(self) -> None:
        with mock.patch.object(node.subprocess, "run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                node.target(self.request, "execute")
        with self.assertRaisesRegex(
            node.ExecutionError, "NODE_RESULT_UNKNOWN_RECONCILIATION_REQUIRED"
        ):
            node.target(self.request, "execute")
        self.assertEqual(node.target(self.request, "status")["status"], "INTENT")
        self.assertFalse(self.counter.exists())

    def test_timeout_requires_reconciliation_and_never_reexecutes(self) -> None:
        with mock.patch.object(
            node.subprocess, "run", side_effect=subprocess.TimeoutExpired("owner", 10)
        ) as call:
            first = node.target(self.request, "execute")
            second = node.target(self.request, "execute")
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "RESULT_UNKNOWN")
        call.assert_called_once()

    def test_native_owner_failure_is_retained_without_automatic_retry(self) -> None:
        self.entrypoint.write_text("raise SystemExit(7)\n")
        self.request["source_files"][str(self.entrypoint.relative_to(self.source))] = (
            node.checksum(self.entrypoint.read_bytes())
        )
        first = node.target(self.request, "execute")
        second = node.target(self.request, "execute")
        self.assertEqual(first, second)
        self.assertEqual(first["exit_code"], 7)
        self.assertEqual(first["status"], "OWNER_COMMAND_FAILED")

    def test_unrelated_environment_credentials_are_not_inherited(self) -> None:
        self.entrypoint.write_text(
            "import os\nassert 'OPENAI_API_KEY' not in os.environ\nassert 'PYTHONPATH' not in os.environ\nassert 'DOCKER_CONTEXT' not in os.environ\n"
        )
        self.request["source_files"][str(self.entrypoint.relative_to(self.source))] = (
            node.checksum(self.entrypoint.read_bytes())
        )
        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "never-forward-test-only",
                "PYTHONPATH": "wrong",
                "DOCKER_CONTEXT": "wrong",
            },
        ):
            self.assertEqual(
                node.target(self.request, "execute")["status"], "COMMAND_SUCCEEDED"
            )

    def test_unsafe_scopes_are_refused(self) -> None:
        changes = [
            {"procedure": "arbitrary-shell"},
            {"arguments": ["stop", "--role", "worker"]},
            {"arguments": ["start", "--role", "bundle"]},
            {"arguments": ["start", "--role", "worker", "--role=bundle"]},
            {"arguments": ["start", "--role", "worker", "--role", "bundle"]},
            {"arguments": ["start", "--role", "worker", "--update"]},
            {"environment": {"OPENAI_API_KEY": "not-accepted"}},
            {"environment": {"DOCKER_HOST": "tcp://remote:2375"}},
            {"state_root": str(self.source / "state")},
            {"schema_version": True},
        ]
        for changed in changes:
            with self.subTest(changed=changed):
                request = {**self.request, **changed}
                with self.assertRaises(node.ExecutionError):
                    node.target(request, "execute")
        self.assertFalse(self.counter.exists())

    def ssh_binding(self) -> dict:
        identity = self.root / "identity"
        identity.write_text("synthetic-reference")
        identity.chmod(0o600)
        hosts = self.root / "known_hosts"
        hosts.write_text("synthetic-reference")
        hosts.chmod(0o600)
        return {
            "kind": "ssh",
            "host": "node.example",
            "user": "operator",
            "port": 22,
            "identity_file": str(identity),
            "known_hosts_file": str(hosts),
            "evidence_root": str(self.root / "transport-evidence"),
        }

    def test_ssh_pins_host_keys_and_disables_config_password_and_forwarding(
        self,
    ) -> None:
        command = node.ssh_command(self.ssh_binding())
        for value in (
            "StrictHostKeyChecking=yes",
            "BatchMode=yes",
            "ForwardAgent=no",
            "ClearAllForwardings=yes",
            "GlobalKnownHostsFile=/dev/null",
        ):
            self.assertIn(value, command)
        self.assertEqual(command[:3], ["ssh", "-F", "/dev/null"])
        self.assertNotIn("StrictHostKeyChecking=no", command)

    def test_ssh_host_key_failure_never_calls_local_owner(self) -> None:
        with mock.patch.object(
            node.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 255, b"", b"private-ssh-diagnostic"
            ),
        ):
            with self.assertRaisesRegex(
                node.ExecutionError, "NODE_TRANSPORT_UNKNOWN_RECONCILE_BEFORE_RETRY"
            ):
                node.invoke(self.ssh_binding(), self.request, "execute")
        self.assertFalse(self.counter.exists())

    def test_remote_carrier_preserves_literal_arguments_without_shell_execution(
        self,
    ) -> None:
        # Execute the exact carrier through a subprocess; SSH channel itself is mocked.
        binding = self.ssh_binding()
        original = subprocess.run
        self.request["arguments"].append("$(touch should-not-exist); `false`")

        def channel(command, **kwargs):
            self.assertEqual(command[0], "ssh")
            return original([sys.executable, "-c", node.BOOTSTRAP], **kwargs)

        with mock.patch.object(node.subprocess, "run", side_effect=channel):
            result = node.invoke(binding, self.request, "execute")
        self.assertEqual(result["status"], "COMMAND_SUCCEEDED")
        self.assertEqual(self.counter.read_text(), "1")
        self.assertFalse((self.entrypoint.parent / "should-not-exist").exists())

    def test_source_symlink_cannot_substitute_pinned_entrypoint(self) -> None:
        alternate = self.root / "alternate.py"
        alternate.write_bytes(self.entrypoint.read_bytes())
        self.entrypoint.unlink()
        self.entrypoint.symlink_to(alternate)
        with self.assertRaisesRegex(node.ExecutionError, "NODE_SYMLINK_REFUSED"):
            node.target(self.request, "execute")
        self.assertFalse(self.counter.exists())

    def test_concurrent_operations_share_owner_scope_lock(self) -> None:
        import fcntl

        state = self.root / "state"
        state.mkdir(mode=0o700)
        with (state / "scope.lock").open("wb") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(node.ExecutionError, "NODE_OPERATION_BUSY"):
                node.target(self.request, "execute")
        self.assertFalse(self.counter.exists())

    def test_durability_failure_before_intent_never_starts_owner(self) -> None:
        with mock.patch.object(
            node.os, "fsync", side_effect=OSError("synthetic disk sync failure")
        ):
            with self.assertRaises(OSError):
                node.target(self.request, "execute")
        self.assertFalse(self.counter.exists())
        self.assertFalse(list((self.root / "state").rglob("receipt.json")))

    def test_controller_does_not_probe_target_paths_on_its_own_filesystem(self) -> None:
        remote = self.root / "remote-only-prefix"
        remote.symlink_to(self.source, target_is_directory=True)
        request = {
            **self.request,
            "source_root": str(remote / "source"),
            "state_root": str(remote / "state"),
        }
        with mock.patch.object(
            node.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 255, b"", b"connection failed"
            ),
        ) as transport:
            with self.assertRaisesRegex(
                node.ExecutionError, "NODE_TRANSPORT_UNKNOWN_RECONCILE_BEFORE_RETRY"
            ):
                node.invoke(self.ssh_binding(), request, "execute")
        transport.assert_called_once()
        diagnostics = next((self.root / "transport-evidence").iterdir())
        self.assertEqual((diagnostics / "stderr").read_bytes(), b"connection failed")
        self.assertEqual((diagnostics / "stderr").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
