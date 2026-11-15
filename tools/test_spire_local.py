"""Check initial grants, preservation and exact retirement across owner CLIs."""

from __future__ import annotations

import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools import full_local, spire_local


class SpireLocalTest(unittest.TestCase):
    def source(self, root: Path, *, provided: bool = False) -> Path:
        value: dict[str, Any] = {
            "schema_version": 1,
            "environment": "test",
            "server_plan": {
                "owner": "infrastructure-spire",
                "schema_version": 1,
                "environment": "test",
            },
            "observation_plan": {
                "owner": "infrastructure-spire",
                "schema_version": 1,
                "grafana_url": "https://grafana:13000",
            },
        }
        if provided:
            value["grafana_tokens"] = {
                "publisher": "/explicit/publisher",
                "provisioner": "/explicit/provisioner",
            }
        path = root / "input.json"
        spire_local.save(path, value)
        return path

    def test_repeated_preparation_never_issues_a_second_grant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = self.source(root)
            phases: list[str] = []

            def invoke(
                arguments: list[str], _root: Path, directory: Path, phase: str
            ) -> dict[str, Any]:
                phases.append(phase)
                if phase == "grafana-issue":
                    self.assertIn("--with-alert-provisioner", arguments)
                    return {"status": "TOKENS_ISSUED", "folder": "infrastructure-spire"}
                return {
                    "status": "LOCAL_INITIAL_BUNDLE_PREPARED",
                    "initial_input": str(directory / "material/initial-input"),
                }

            with (
                mock.patch.object(
                    spire_local, "operator_arguments", return_value=["operator-input"]
                ),
                mock.patch.object(spire_local, "invoke", side_effect=invoke),
            ):
                expected = spire_local.prepare(root, source)
                self.assertEqual(spire_local.prepare(root, source), expected)
            self.assertEqual(
                phases, ["grafana-issue", "owner-prepare", "owner-prepare"]
            )
            owner_input = json.loads(
                (root / "runtime/spire-local-input/owner-input.json").read_text()
            )
            self.assertEqual(
                set(owner_input["grafana_tokens"]), {"publisher", "provisioner"}
            )
            for file in (root / "runtime/spire-local-input").iterdir():
                if file.is_file():
                    self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)

    def test_lost_issue_response_leaves_intent_and_prevents_another_grant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = self.source(root)
            with (
                mock.patch.object(
                    spire_local, "operator_arguments", return_value=["operator-input"]
                ),
                mock.patch.object(
                    spire_local,
                    "invoke",
                    side_effect=full_local.FullLocalError(
                        "FULL_RUNTIME_COMMAND_RESULT_UNKNOWN"
                    ),
                ) as invoke,
                self.assertRaisesRegex(full_local.FullLocalError, "RESULT_UNKNOWN"),
            ):
                spire_local.prepare(root, source)
            self.assertEqual(invoke.call_count, 1)
            with (
                mock.patch.object(spire_local, "invoke") as repeat,
                self.assertRaisesRegex(
                    full_local.FullLocalError, "INSPECTION_REQUIRED"
                ),
            ):
                spire_local.prepare(root, source)
            repeat.assert_not_called()
            receipt = json.loads(
                (root / "runtime/spire-local-input/receipt.json").read_text()
            )
            self.assertEqual(receipt["status"], "PREPARING")

    def test_external_tokens_are_preserved_and_never_reported_as_retired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = self.source(root, provided=True)
            directory = root / "runtime/spire-local-input"
            with mock.patch.object(
                spire_local,
                "invoke",
                return_value={
                    "status": "LOCAL_INITIAL_BUNDLE_PREPARED",
                    "initial_input": str(directory / "material/initial-input"),
                },
            ):
                spire_local.prepare(root, source)
            with mock.patch.object(spire_local, "invoke") as invoke:
                self.assertFalse(spire_local.retire(root))
            invoke.assert_not_called()
            self.assertEqual(
                spire_local.configuration_receipt(directory / "receipt.json")["status"],
                "BUNDLE_PREPARED",
            )

    def test_exact_auto_provisioner_retirement_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = root / "runtime/spire-local-input"
            directory.mkdir(parents=True, mode=0o700)
            spire_local.save(
                directory / "receipt.json",
                {
                    "status": "BUNDLE_PREPARED",
                    "operator_arguments": ["--environment", "test"],
                },
            )
            with mock.patch.object(
                spire_local, "invoke", return_value={"status": "PROVISIONER_RETIRED"}
            ) as invoke:
                self.assertTrue(spire_local.retire(root))
                self.assertTrue(spire_local.retire(root))
            self.assertEqual(invoke.call_count, 1)
            self.assertIn("retire-provisioner", invoke.call_args.args[0])

    def test_config_change_never_issues_new_tokens_into_an_existing_operation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = self.source(root, provided=True)
            directory = root / "runtime/spire-local-input"
            with mock.patch.object(
                spire_local,
                "invoke",
                return_value={
                    "status": "LOCAL_INITIAL_BUNDLE_PREPARED",
                    "initial_input": str(directory / "material/initial-input"),
                },
            ):
                spire_local.prepare(root, source)
            value = spire_local.configuration(source)
            value["server_plan"]["trust_domain"] = "changed.example"
            spire_local.save(source, value, replace=True)
            with (
                mock.patch.object(spire_local, "invoke") as invoke,
                self.assertRaisesRegex(
                    full_local.FullLocalError, "INSPECTION_REQUIRED"
                ),
            ):
                spire_local.prepare(root, source)
            invoke.assert_not_called()

    def test_remote_grafana_requires_explicit_tokens_before_any_secret_copy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            owner = root / "observability/grafana"
            owner.mkdir(parents=True)
            (owner / ".env").write_text("AELEV_INFRASTRUCTURE_GRAFANA_PORT=13000\n")
            with self.assertRaisesRegex(full_local.FullLocalError, "TARGET_MISMATCH"):
                spire_local.operator_arguments(
                    root,
                    {"observation_plan": {"grafana_url": "https://remote.example"}},
                    root,
                )
            self.assertFalse((root / "operator-password").exists())

    def test_unknown_child_does_not_retry_and_diagnostics_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            arguments = ["python", "owner-command"]
            with (
                mock.patch.object(
                    spire_local.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired(arguments, 600),
                ) as run,
                self.assertRaisesRegex(
                    full_local.FullLocalError, "^FULL_RUNTIME_COMMAND_RESULT_UNKNOWN$"
                ),
            ):
                spire_local.invoke(arguments, root, root, "owner-prepare")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(len(list(root.glob("*.unknown"))), 1)
            self.assertEqual(
                stat.S_IMODE(next(root.glob("*.unknown")).stat().st_mode), 0o600
            )

    def test_arbitrary_child_error_is_not_exposed_to_the_operator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            reply = subprocess.CompletedProcess(
                [], 2, "", "private diagnostic\nGRAFANA_LOCAL_HTTP_403\n"
            )
            with (
                mock.patch.object(spire_local.subprocess, "run", return_value=reply),
                self.assertRaisesRegex(
                    full_local.FullLocalError,
                    "^FULL_RUNTIME_SPIRE_LOCAL_PREPARATION_FAILED$",
                ),
            ):
                spire_local.invoke(
                    ["python", "owner-command"], root, root, "owner-prepare"
                )
            self.assertEqual(next(root.glob("*.stderr")).read_text(), reply.stderr)


if __name__ == "__main__":
    unittest.main()
