"""Full runtime coverage, fixture isolation and preservation of existing owners."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools import full_local, full_runtime

ROOT = Path(__file__).resolve().parents[1]


class FullRuntimeTest(unittest.TestCase):
    def test_failed_native_output_is_retained_privately_without_public_disclosure(
        self,
    ) -> None:
        import stat
        import subprocess

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reply = subprocess.CompletedProcess(
                ["docker"], 1, "private output", "private failure"
            )
            with (
                mock.patch.object(full_runtime.subprocess, "run", return_value=reply),
                self.assertRaisesRegex(
                    full_local.FullLocalError, "^FULL_RUNTIME_COMMAND_FAILED$"
                ),
            ):
                full_runtime.command(["docker"], root)
            outputs = list((root / "runtime").glob("full-runtime-failure-*"))
            self.assertEqual(len(outputs), 1)
            self.assertEqual(stat.S_IMODE(outputs[0].stat().st_mode), 0o700)
            self.assertEqual((outputs[0] / "stderr").read_text(), "private failure")
            for file in outputs[0].iterdir():
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads((outputs[0] / "result.json").read_text()),
                {"status": "FAILED"},
            )

    def test_timeout_diagnostic_remains_unknown_and_handles_partial_bytes(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timeout = subprocess.TimeoutExpired(
                ["docker"], 1200, output=b"partial output", stderr=b"partial failure"
            )
            with (
                mock.patch.object(
                    full_runtime.subprocess, "run", side_effect=timeout
                ) as run,
                self.assertRaisesRegex(full_local.FullLocalError, "RESULT_UNKNOWN"),
            ):
                full_runtime.command(["docker"], root)
            run.assert_called_once()
            output = next((root / "runtime").glob("full-runtime-failure-*"))
            self.assertEqual((output / "stdout").read_bytes(), b"partial output")
            self.assertEqual(
                json.loads((output / "result.json").read_text()), {"status": "UNKNOWN"}
            )

    def test_renewal_socket_scope_is_checked_before_any_initialization(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(full_runtime.spire_renewal, "configuration") as config,
            mock.patch.object(full_local, "initialize_profile") as initialize,
            self.assertRaisesRegex(
                full_local.FullLocalError, "SOCKET_APPROVAL_REQUIRED"
            ),
        ):
            full_runtime.execute_full(
                "up",
                ROOT,
                no_build=True,
                verify_scope=mock.Mock(),
                spire_renewal_input=Path("renewal.json"),
            )
        config.assert_not_called()
        initialize.assert_not_called()

    def test_invalid_renewal_approval_precedes_core_and_native_actions(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"DRP_SPIRE_DOCKER_SOCKET_APPROVAL": "operator-root-equivalent-v1"},
                clear=True,
            ),
            mock.patch.object(
                full_runtime.spire_renewal,
                "configuration",
                side_effect=full_local.FullLocalError("RENEWAL_CONFIGURATION_INVALID"),
            ),
            mock.patch.object(full_local, "initialize_profile") as initialize,
            mock.patch.object(full_runtime, "command") as command,
            self.assertRaisesRegex(
                full_local.FullLocalError, "RENEWAL_CONFIGURATION_INVALID"
            ),
        ):
            full_runtime.execute_full(
                "up",
                ROOT,
                no_build=True,
                verify_scope=mock.Mock(),
                spire_renewal_input=Path("renewal.json"),
            )
        command.assert_not_called()
        initialize.assert_not_called()

    def test_local_input_requires_socket_approval_before_any_preparation(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                full_runtime,
                "contract",
                return_value=json.loads((ROOT / "full-local.json").read_text()),
            ),
            mock.patch.object(full_local, "initialize_profile") as initialize,
            mock.patch.object(full_runtime.spire_local, "prepare") as prepare,
            mock.patch.object(full_runtime, "command") as command,
            self.assertRaisesRegex(
                full_local.FullLocalError, "SOCKET_APPROVAL_REQUIRED"
            ),
        ):
            full_runtime.execute_full(
                "up",
                Path(directory),
                no_build=True,
                verify_scope=mock.Mock(),
                spire_local_input=Path(directory) / "input.json",
            )
        initialize.assert_not_called()
        prepare.assert_not_called()
        command.assert_not_called()

    def test_alternative_spire_inputs_cannot_be_combined(self) -> None:
        with (
            mock.patch.object(full_runtime, "command") as command,
            self.assertRaisesRegex(full_local.FullLocalError, "INPUT_OPTIONS_CONFLICT"),
        ):
            full_runtime.execute_full(
                "up",
                ROOT,
                no_build=True,
                verify_scope=mock.Mock(),
                spire_initial_input=Path("initial"),
                spire_local_input=Path("local"),
            )
        command.assert_not_called()

    def test_local_sequence_waits_for_core_and_retires_only_after_native_install(
        self,
    ) -> None:
        manifest, model = self._startup_model()
        delegated = {"spire-status-provision", "spire-configuration-init"}
        model["services"].update({name: {} for name in delegated})
        required = set(manifest["root_compose"]["required_services"])
        events: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / "runtime/spire-local-input/receipt.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text("{}")

            def ready(
                _model: dict, names: set[str], _root: Path, **kwargs: object
            ) -> None:
                events.append(
                    "all-ready"
                    if names == required
                    else "core-ready"
                    if names == required - full_runtime.SPIRE_ROLES
                    else "dependency-ready"
                )

            def prepare(_root: Path, _source: Path) -> Path:
                events.append("local-prepare")
                return root / "prepared/initial-input"

            def bootstrap(_root: Path, source: Path, _manifest: dict) -> None:
                self.assertEqual(source, root / "prepared/initial-input")
                events.append("native-bootstrap")

            def retire(_root: Path) -> bool:
                events.append("retire")
                return True

            with (
                mock.patch.object(
                    full_runtime, "command", return_value=json.dumps(model)
                ) as command,
                mock.patch.object(
                    full_runtime, "initial_binding_required", return_value=True
                ),
                mock.patch.object(full_runtime, "guard_existing"),
                mock.patch.object(full_runtime, "start"),
                mock.patch.object(full_runtime, "wait_ready", side_effect=ready),
                mock.patch.object(
                    full_runtime.spire_local, "prepare", side_effect=prepare
                ),
                mock.patch.object(
                    full_runtime.spire_local, "retire", side_effect=retire
                ),
                mock.patch.object(
                    full_runtime, "bootstrap_spire", side_effect=bootstrap
                ),
                mock.patch.object(full_runtime, "require_spire_ready"),
                mock.patch.object(full_runtime, "plan", return_value={}),
                mock.patch.object(
                    full_runtime,
                    "spire_binding",
                    return_value={"spire-server": {"compose_project": "native"}},
                ),
            ):
                result = full_runtime.up(
                    root, manifest, spire_local_input=root / "input.json"
                )
            for call in command.call_args_list:
                self.assertFalse(delegated.intersection(call.args[0]))
            self.assertEqual(
                events[-5:],
                [
                    "core-ready",
                    "local-prepare",
                    "native-bootstrap",
                    "all-ready",
                    "retire",
                ],
            )
            self.assertTrue(result["spire_local_provisioner_retired"])

    def test_owner_unknown_result_remains_unknown_across_the_cli_boundary(self) -> None:
        import subprocess

        arguments = ["python", str(ROOT / "spire/bootstrap.py"), "up"]
        reply = subprocess.CompletedProcess(
            arguments, 2, "", "SPIRE_BOOTSTRAP_RESULT_UNKNOWN\n"
        )
        with (
            mock.patch.object(full_runtime, "record_command_failure"),
            mock.patch.object(
                full_runtime.subprocess, "run", return_value=reply
            ) as run,
            self.assertRaisesRegex(
                full_local.FullLocalError, "^FULL_RUNTIME_COMMAND_RESULT_UNKNOWN$"
            ),
        ):
            full_runtime.command(arguments, ROOT)
        self.assertEqual(run.call_count, 1)

    def test_owner_error_code_is_propagated_without_arbitrary_diagnostics(self) -> None:
        import subprocess

        arguments = ["python", str(ROOT / "spire/bootstrap.py"), "validate"]
        reply = subprocess.CompletedProcess(
            arguments, 2, "", "SPIRE_BOOTSTRAP_INITIAL_BUNDLE_INVALID\n"
        )
        with (
            mock.patch.object(full_runtime, "record_command_failure"),
            mock.patch.object(full_runtime.subprocess, "run", return_value=reply),
            self.assertRaisesRegex(
                full_local.FullLocalError,
                "^FULL_RUNTIME_SPIRE_BOOTSTRAP_INITIAL_BUNDLE_INVALID$",
            ),
        ):
            full_runtime.command(arguments, ROOT)
        reply.stderr = (
            "private diagnostic text\nSPIRE_BOOTSTRAP_INITIAL_BUNDLE_INVALID\n"
        )
        with (
            mock.patch.object(full_runtime, "record_command_failure"),
            mock.patch.object(full_runtime.subprocess, "run", return_value=reply),
            self.assertRaisesRegex(
                full_local.FullLocalError, "^FULL_RUNTIME_COMMAND_FAILED$"
            ),
        ):
            full_runtime.command(arguments, ROOT)

    def test_new_owner_requires_explicit_socket_approval_before_preparation(
        self,
    ) -> None:
        manifest = json.loads((ROOT / "full-local.json").read_text())
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(full_runtime, "contract", return_value=manifest),
            mock.patch.object(full_local, "initialize_profile") as initialize,
            mock.patch.object(full_runtime, "command") as command,
            self.assertRaisesRegex(
                full_local.FullLocalError, "SOCKET_APPROVAL_REQUIRED"
            ),
        ):
            full_runtime.execute_full(
                "up",
                Path(directory),
                no_build=True,
                verify_scope=mock.Mock(),
                spire_initial_input=Path(directory) / "operator-input",
            )
        initialize.assert_not_called()
        command.assert_not_called()

    def test_invalid_existing_export_is_never_replaced_by_a_new_bootstrap(self) -> None:
        manifest = json.loads((ROOT / "full-local.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                full_runtime.SPIRE_COMPOSITION_VARIABLE
                + "=/missing/export/compose.json\n"
            )
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(full_runtime, "contract", return_value=manifest),
                mock.patch.object(full_local, "initialize_profile") as initialize,
                mock.patch.object(full_runtime, "command") as command,
                self.assertRaisesRegex(
                    full_local.FullLocalError, "PUBLIC_EXPORT_REQUIRED"
                ),
            ):
                full_runtime.execute_full(
                    "up",
                    root,
                    no_build=True,
                    verify_scope=mock.Mock(),
                    spire_initial_input=root / "operator-input",
                )
            initialize.assert_not_called()
            command.assert_not_called()

    def test_owner_bootstrap_persists_only_the_public_binding_and_preserves_env(
        self,
    ) -> None:
        manifest = json.loads((ROOT / "full-local.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt, _, _ = self._spire_export(root)
            owner_env = b"AELEV_INFRASTRUCTURE_SPIRE_COMPOSE_PROJECT=owned\n"
            (root / "spire/.env").write_bytes(owner_env)
            original = b"# operator settings\nAELEV_INFRASTRUCTURE_COMPOSE_PROJECT=core-owned\nOPERATOR_SETTING=preserve\n"
            (root / ".env").write_bytes(original)
            projection = (
                root
                / "spire/runtime/installed-composition"
                / receipt["generation"]
                / "compose.json"
            )
            reply = json.dumps(
                {
                    "status": "SPIRE_RUNTIME_INSTALLED",
                    "project": "owned",
                    "composition": str(projection),
                }
            )
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(
                    full_runtime, "command", return_value=reply
                ) as command,
            ):
                full_runtime.bootstrap_spire(root, root / "operator-input", manifest)
                self.assertEqual(full_runtime.spire_projection(root), projection)
            self.assertEqual((root / "spire/.env").read_bytes(), owner_env)
            self.assertEqual(
                (root / ".env").read_bytes(),
                original
                + (
                    full_runtime.SPIRE_COMPOSITION_VARIABLE
                    + "="
                    + str(projection)
                    + "\n"
                ).encode(),
            )
            self.assertIn("up", command.call_args.args[0])
            self.assertFalse(list((root / "runtime").glob(".spire-binding-*")))

    def test_core_initializer_runs_under_the_existing_lifecycle_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "runtime/lifecycle.lock"
            lock.mkdir(parents=True)
            manifest = {
                "environment_files": [],
                "profile": "full-local",
                "release": "1.1.7",
            }
            (root / "full-local.json").write_text(json.dumps(manifest))
            initialize = mock.Mock()
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(full_local, "command") as command,
                mock.patch.object(
                    full_local, "validate_contract", return_value=(manifest, [])
                ),
                mock.patch.object(
                    full_local, "adapt_created_env_identities", return_value=[]
                ),
                mock.patch.object(full_local, "prepare_owner_inputs", return_value=[]),
                mock.patch.object(
                    full_local, "prepare_local_runtime_secret_access", return_value=[]
                ),
            ):
                full_local.initialize_profile(
                    root, provider_fixture=False, initialize_core=initialize
                )
            initialize.assert_called_once_with()
            command.assert_not_called()
            self.assertTrue(lock.is_dir())

    def _startup_model(self) -> tuple[dict, dict]:
        manifest = json.loads((ROOT / "full-local.json").read_text())
        required = set(manifest["root_compose"]["required_services"])
        services: dict[str, Any] = {name: {} for name in required}
        tasks = [
            "platform-ui-configuration-init",
            "grafana-oidc-provision",
            "blackbox-exporter-operator-provision",
            "prometheus-operator-provision",
            "translator-api-provision",
            "owner-configuration-init",
        ]
        services.update({name: {} for name in tasks})
        services["site-replication-applier"] = {
            "depends_on": {"minio-site-a-1": {"condition": "service_healthy"}}
        }
        model = {"name": "owned", "services": services}
        return manifest, model

    def test_fresh_start_waits_for_core_before_owner_bootstrap_and_checks_all_roles(
        self,
    ) -> None:
        manifest, model = self._startup_model()
        required = set(manifest["root_compose"]["required_services"])
        events: list[tuple[str, set[str]]] = []

        def execute(arguments: list[str], root: Path) -> str:
            if "up" in arguments:
                events.append(
                    (
                        "core-start",
                        set(arguments[arguments.index("--no-recreate") + 1 :]),
                    )
                )
            return json.dumps(model) if "config" in arguments else ""

        def ready(model: dict, names: set[str], root: Path, **kwargs: object) -> None:
            events.append(("ready", names))

        def bootstrap(root: Path, source: Path, manifest: dict) -> None:
            events.append(("owner-bootstrap", full_runtime.SPIRE_ROLES))

        with (
            mock.patch.object(full_runtime, "command", side_effect=execute),
            mock.patch.object(
                full_runtime, "initial_binding_required", return_value=True
            ),
            mock.patch.object(full_runtime, "bootstrap_spire", side_effect=bootstrap),
            mock.patch.object(full_runtime, "require_spire_ready", return_value={}),
            mock.patch.object(full_runtime, "guard_existing"),
            mock.patch.object(full_runtime, "wait_ready", side_effect=ready),
            mock.patch.object(
                full_runtime,
                "spire_binding",
                return_value={"spire-server": {"compose_project": "native-owned"}},
            ),
        ):
            result = full_runtime.up(
                ROOT, manifest, spire_initial_input=ROOT / "operator-input"
            )
        index = next(
            i for i, event in enumerate(events) if event[0] == "owner-bootstrap"
        )
        self.assertEqual(
            events[index - 1], ("ready", required - full_runtime.SPIRE_ROLES)
        )
        self.assertEqual(events[-1], ("ready", required))
        self.assertTrue(
            all(
                not (names & full_runtime.SPIRE_ROLES)
                for kind, names in events
                if kind == "core-start"
            )
        )
        self.assertEqual(result["owner_coordinators"]["spire"], "native-owned")

    def test_complete_start_covers_all_real_manifest_roles_without_replacement(
        self,
    ) -> None:
        manifest, model = self._startup_model()
        required = set(manifest["root_compose"]["required_services"])
        commands: list[list[str]] = []

        def execute(arguments: list[str], root: Path) -> str:
            commands.append(arguments)
            return json.dumps(model) if "config" in arguments else ""

        with (
            mock.patch.object(full_runtime, "command", side_effect=execute),
            mock.patch.object(full_runtime, "require_spire_ready", return_value={}),
            mock.patch.object(
                full_runtime,
                "spire_binding",
                return_value={"spire-server": {"compose_project": "owned"}},
            ),
            mock.patch.object(full_runtime, "guard_existing"),
            mock.patch.object(full_runtime, "wait_ready"),
        ):
            result = full_runtime.up(ROOT, manifest)
        starts = [args for args in commands if "up" in args]
        self.assertEqual(
            set(starts[-1][starts[-1].index("--no-recreate") + 1 :]),
            required - full_runtime.SPIRE_ROLES,
        )
        self.assertTrue(all("--no-recreate" in args for args in starts))
        self.assertFalse(
            any(
                word in args
                for args in commands
                for word in ["stop", "rm", "down", "RESUME", "RESET"]
            )
        )
        self.assertEqual(result["status"], "RUNTIME_HEALTHY")
        self.assertFalse(result["provider_admission_proven"])

    def _spire_export(self, root: Path) -> tuple[dict, dict, list[dict]]:
        generation = "a" * 64
        directory = root / "spire/runtime/installed-composition"
        destination = directory / generation
        destination.mkdir(parents=True)
        receipt: dict = {"generation": generation}
        services: dict = {}
        containers = []
        for index, name in enumerate(sorted(full_runtime.SPIRE_ROLES), 1):
            identifier = str(index) * 64
            image = "sha256:" + identifier
            receipt[name] = {
                "container_id": identifier,
                "image": image,
                "native_restarts": 0,
                "restart": "no",
                "compose_project": "owned",
            }
            services[name] = {"image": image, "restart": "no"}
            containers.append(
                {
                    "Id": identifier,
                    "Image": image,
                    "RestartCount": 0,
                    "HostConfig": {
                        "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}
                    },
                    "State": {"Running": True, "Health": {"Status": "healthy"}},
                    "Config": {
                        "Labels": {
                            "com.docker.compose.service": name,
                            "io.aelev.owner": "infrastructure-spire",
                        }
                    },
                }
            )
        (root / "spire/.env").write_text(
            "# AELEV SPIRE installed composition v1\n"
            "AELEV_INFRASTRUCTURE_SPIRE_INSTALLED_COMPOSE="
            "./runtime/installed-composition/" + generation + "/compose.json\n"
        )
        (destination / "compose.json").write_text(json.dumps({"services": services}))
        (directory / "receipt.json").write_text(json.dumps(receipt))
        return receipt, {"name": "owned", "services": services}, containers

    def test_unprepared_spire_refuses_up_before_initialization_or_docker(self) -> None:
        manifest = json.loads((ROOT / "full-local.json").read_text())
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(full_runtime, "contract", return_value=manifest),
            mock.patch.object(full_local, "initialize_profile") as initialize,
            mock.patch.object(full_runtime, "command") as command,
            self.assertRaisesRegex(full_local.FullLocalError, "PUBLIC_EXPORT_REQUIRED"),
        ):
            full_runtime.execute_full(
                "up", Path(directory), no_build=True, verify_scope=mock.Mock()
            )
        initialize.assert_not_called()
        command.assert_not_called()

    def test_copying_only_the_projection_does_not_copy_its_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._spire_export(root)
            (root / "spire/runtime/installed-composition/receipt.json").unlink()
            with self.assertRaisesRegex(
                full_local.FullLocalError, "PUBLIC_EXPORT_REQUIRED"
            ):
                full_runtime.spire_binding(root)

    def test_mixed_export_generations_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt, _, _ = self._spire_export(root)
            receipt["generation"] = "b" * 64
            (root / "spire/runtime/installed-composition/receipt.json").write_text(
                json.dumps(receipt)
            )
            with self.assertRaisesRegex(
                full_local.FullLocalError, "PUBLIC_EXPORT_REQUIRED"
            ):
                full_runtime.spire_binding(root)

    def test_separate_native_coordinator_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, model, rows = self._spire_export(root)
            model["name"] = "another-project"
            with (
                mock.patch.object(
                    full_runtime, "containers", return_value=rows
                ) as containers,
                mock.patch.object(full_runtime, "command") as command,
            ):
                actual = full_runtime.verify_spire_binding(root, model)
            containers.assert_called_once_with("owned", root)
            self.assertEqual(
                {row["Id"] for row in actual.values()}, {row["Id"] for row in rows}
            )
            command.assert_not_called()

    def test_explicit_external_export_does_not_require_replacing_owner_env(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected, _, _ = self._spire_export(root)
            projection = (
                root
                / "spire/runtime/installed-composition"
                / expected["generation"]
                / "compose.json"
            )
            with mock.patch.dict(
                os.environ, {full_runtime.SPIRE_COMPOSITION_VARIABLE: str(projection)}
            ):
                self.assertEqual(
                    full_runtime.spire_binding(root / "another-checkout"), expected
                )

    def test_mixed_physical_coordinators_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt, _, _ = self._spire_export(root)
            receipt["spire-server"]["compose_project"] = "different-project"
            (root / "spire/runtime/installed-composition/receipt.json").write_text(
                json.dumps(receipt)
            )
            with self.assertRaisesRegex(
                full_local.FullLocalError, "PUBLIC_EXPORT_REQUIRED"
            ):
                full_runtime.spire_binding(root)

    def test_stopped_native_owner_requires_its_own_recovery_before_core_start(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, model, rows = self._spire_export(root)
            rows[0]["State"]["Running"] = False
            model["services"].update({name: {} for name in ["kafka", "keycloak"]})
            manifest = {
                "root_compose": {
                    "file": "compose.yml",
                    "profiles": [],
                    "required_services": sorted(model["services"]),
                }
            }
            with (
                mock.patch.object(full_runtime, "containers", return_value=rows),
                mock.patch.object(
                    full_runtime, "command", return_value=json.dumps(model)
                ) as command,
                self.assertRaisesRegex(
                    full_local.FullLocalError, "OWNER_RUNTIME_NOT_READY"
                ),
            ):
                full_runtime.up(root, manifest)
            self.assertEqual(len(command.call_args_list), 1)
            self.assertIn("config", command.call_args.args[0])

    def test_readiness_combines_core_and_exact_native_owner_containers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, model, rows = self._spire_export(root)
            model["name"] = "core-project"
            model["services"]["core-api"] = {"healthcheck": {"test": ["CMD", "probe"]}}
            core = {
                "Id": "core-id",
                "Config": {"Labels": {"com.docker.compose.service": "core-api"}},
                "State": {"Running": True, "Health": {"Status": "healthy"}},
            }

            def current(project: str, _root: Path) -> list[dict]:
                return rows if project == "owned" else [core]

            with mock.patch.object(full_runtime, "containers", side_effect=current):
                required = set(model["services"])
                self.assertEqual(
                    full_runtime.pending_services(model, required, root), []
                )
                rows[0]["State"]["Health"]["Status"] = "unhealthy"
                self.assertEqual(
                    full_runtime.pending_services(model, required, root),
                    [sorted(full_runtime.SPIRE_ROLES)[0]],
                )

    def test_second_native_writer_in_core_is_not_hidden_by_delegation(self) -> None:
        row, model = self._existing()
        name = "spire-server"
        row["Id"] = "wrong-id"
        row["Config"]["Labels"]["com.docker.compose.service"] = name
        with (
            mock.patch.object(full_runtime, "containers", return_value=[row]),
            mock.patch.object(full_runtime, "command", return_value=""),
            self.assertRaisesRegex(full_local.FullLocalError, "SPIRE_EXPORT_STALE"),
        ):
            full_runtime.guard_existing(
                ROOT,
                ["docker", "compose"],
                model,
                {name},
                delegated={name: "exact-native-id"},
            )

    def test_down_uses_each_existing_coordinator_and_never_removes_resources(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, model, _ = self._spire_export(root)
            model["name"] = "core-project"
            model["services"]["core-api"] = {}
            manifest = {
                "root_compose": {
                    "file": "compose.yml",
                    "profiles": [],
                    "required_services": sorted(model["services"]),
                }
            }
            commands: list[list[str]] = []

            def execute(arguments: list[str], _root: Path) -> str:
                commands.append(arguments)
                return json.dumps(model) if "config" in arguments else ""

            with (
                mock.patch.object(full_runtime, "contract", return_value=manifest),
                mock.patch.object(full_runtime, "verify_spire_binding"),
                mock.patch.object(full_runtime, "command", side_effect=execute),
            ):
                full_runtime.execute_full(
                    "down", root, no_build=True, verify_scope=mock.Mock()
                )
            stops = [args for args in commands if "stop" in args]
            self.assertEqual(stops[0][-2:], ["stop", "core-api"])
            self.assertEqual(stops[1][stops[1].index("--project-name") + 1], "owned")
            self.assertEqual(
                set(stops[1][stops[1].index("stop") + 1 :]), full_runtime.SPIRE_ROLES
            )
            self.assertFalse(
                any(
                    word in args
                    for args in commands
                    for word in ["up", "down", "rm", "RESUME"]
                )
            )

    def test_old_container_receipt_is_refused_without_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, model, rows = self._spire_export(root)
            rows[0]["Id"] = "b" * 64
            with (
                mock.patch.object(full_runtime, "containers", return_value=rows),
                mock.patch.object(full_runtime, "command") as command,
                self.assertRaisesRegex(full_local.FullLocalError, "EXPORT_STALE"),
            ):
                full_runtime.verify_spire_binding(root, model)
            command.assert_not_called()

    def test_native_restart_history_change_requires_a_fresh_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, model, rows = self._spire_export(root)
            rows[0]["RestartCount"] = 1
            with (
                mock.patch.object(full_runtime, "containers", return_value=rows),
                self.assertRaisesRegex(full_local.FullLocalError, "EXPORT_STALE"),
            ):
                full_runtime.verify_spire_binding(root, model)

    def test_native_restart_policy_cannot_silently_restore_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, model, rows = self._spire_export(root)
            rows[0]["HostConfig"]["RestartPolicy"] = {
                "Name": "on-failure",
                "MaximumRetryCount": 3,
            }
            with (
                mock.patch.object(full_runtime, "containers", return_value=rows),
                self.assertRaisesRegex(full_local.FullLocalError, "EXPORT_STALE"),
            ):
                full_runtime.verify_spire_binding(root, model)

    def test_spent_native_attempts_are_compared_as_remaining_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt, model, rows = self._spire_export(root)
            role = rows[0]["Config"]["Labels"]["com.docker.compose.service"]
            rows[0]["RestartCount"] = 2
            rows[0]["HostConfig"]["RestartPolicy"] = {
                "Name": "on-failure",
                "MaximumRetryCount": 3,
            }
            receipt[role]["native_restarts"] = 2
            receipt[role]["restart"] = "on-failure:1"
            model["services"][role]["restart"] = "on-failure:1"
            output = root / "spire/runtime/installed-composition"
            (output / "receipt.json").write_text(json.dumps(receipt))
            (output / receipt["generation"] / "compose.json").write_text(
                json.dumps({"services": model["services"]})
            )
            with mock.patch.object(full_runtime, "containers", return_value=rows):
                verified = full_runtime.verify_spire_binding(root, model)
            self.assertEqual(verified[role]["RestartCount"], 2)

    def test_matching_public_export_is_verified_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, model, rows = self._spire_export(root)
            with (
                mock.patch.object(full_runtime, "containers", return_value=rows),
                mock.patch.object(full_runtime, "command") as command,
            ):
                full_runtime.verify_spire_binding(root, model)
            command.assert_not_called()

    def test_plan_covers_the_actual_manifest_without_docker_or_files(self) -> None:
        expected = json.loads((ROOT / "full-local.json").read_text())
        with mock.patch.object(full_runtime, "command") as command:
            actual = full_runtime.plan(ROOT)
        self.assertEqual(
            actual["services"], expected["root_compose"]["required_services"]
        )
        self.assertEqual(actual["owners"], expected["owners"])
        self.assertEqual(len(actual["services"]), 57)
        self.assertFalse(actual["provider_fixture_selected"])
        self.assertFalse(actual["inference_requested"])
        command.assert_not_called()

    def test_regular_initializer_removes_inherited_fixture_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {
                "environment_files": [],
                "profile": "full-local",
                "release": "1.1.7",
            }
            (root / "full-local.json").write_text(json.dumps(manifest))
            with (
                mock.patch.dict(
                    os.environ,
                    {full_runtime.FIXTURE_VARIABLE: "health-only-no-inference-v1"},
                ),
                mock.patch.object(full_local, "command") as command,
                mock.patch.object(
                    full_local, "validate_contract", return_value=(manifest, [])
                ),
                mock.patch.object(
                    full_local, "adapt_created_env_identities", return_value=[]
                ),
                mock.patch.object(full_local, "prepare_owner_inputs", return_value=[]),
                mock.patch.object(
                    full_local, "prepare_local_runtime_secret_access", return_value=[]
                ),
            ):
                full_local.initialize_profile(root, provider_fixture=False)
            self.assertNotIn(
                full_runtime.FIXTURE_VARIABLE, command.call_args.kwargs["environment"]
            )

    def test_clean_room_default_still_selects_its_explicit_health_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {
                "environment_files": [],
                "profile": "full-local",
                "release": "1.1.7",
            }
            (root / "full-local.json").write_text(json.dumps(manifest))
            with (
                mock.patch.object(full_local, "command") as command,
                mock.patch.object(
                    full_local, "validate_contract", return_value=(manifest, [])
                ),
                mock.patch.object(
                    full_local, "adapt_created_env_identities", return_value=[]
                ),
                mock.patch.object(full_local, "prepare_owner_inputs", return_value=[]),
                mock.patch.object(
                    full_local, "prepare_local_runtime_secret_access", return_value=[]
                ),
            ):
                full_local.initialize_profile(root)
            self.assertEqual(
                command.call_args.kwargs["environment"][full_runtime.FIXTURE_VARIABLE],
                "health-only-no-inference-v1",
            )

    def test_full_start_rejects_ambient_fixture_before_any_effect(self) -> None:
        with (
            mock.patch.dict(os.environ, {full_runtime.FIXTURE_VARIABLE: ""}),
            mock.patch.object(full_runtime, "contract") as contract,
            mock.patch.object(full_local, "initialize_profile") as initialize,
            self.assertRaisesRegex(
                full_local.FullLocalError, "FIXTURE_ENVIRONMENT_REFUSED"
            ),
        ):
            full_runtime.execute_full(
                "up", ROOT, no_build=True, verify_scope=mock.Mock()
            )
        contract.assert_not_called()
        initialize.assert_not_called()

    def test_build_is_nonfixture_and_never_starts_runtime(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                full_runtime,
                "contract",
                return_value={
                    "root_compose": {
                        "required_services": [],
                        "profiles": [],
                        "file": "compose.yml",
                    }
                },
            ),
            mock.patch.object(full_local, "build_profile") as build,
            mock.patch.object(full_runtime, "command") as command,
        ):
            full_runtime.execute_full(
                "build", ROOT, no_build=False, verify_scope=mock.Mock()
            )
        build.assert_called_once_with(
            ROOT, provider_fixture=False, initialize_core=None
        )
        command.assert_not_called()

    def _existing(self) -> tuple[dict[str, Any], dict[str, Any]]:
        row: dict[str, object] = {
            "Image": "sha256:original",
            "Config": {
                "Labels": {
                    "com.docker.compose.service": "owner-api",
                    "com.docker.compose.config-hash": "original",
                }
            },
            "State": {"Running": True},
        }
        model: dict[str, object] = {
            "name": "owned",
            "services": {"owner-api": {"image": "owner@sha256:pinned"}},
        }
        return row, model

    def test_config_change_requires_handoff_before_any_start(self) -> None:
        row, model = self._existing()
        with (
            mock.patch.object(full_runtime, "containers", return_value=[row]),
            mock.patch.object(
                full_runtime, "command", return_value="owner-api changed\n"
            ) as command,
            self.assertRaisesRegex(full_local.FullLocalError, "OWNER_HANDOFF_REQUIRED"),
        ):
            full_runtime.guard_existing(
                ROOT, ["docker", "compose"], model, {"owner-api"}
            )
        self.assertEqual(len(command.call_args_list), 1)

    def test_native_image_change_requires_handoff_even_when_tag_is_unchanged(
        self,
    ) -> None:
        row, model = self._existing()
        with (
            mock.patch.object(full_runtime, "containers", return_value=[row]),
            mock.patch.object(
                full_runtime,
                "command",
                side_effect=["owner-api original\n", "sha256:changed\n"],
            ),
            self.assertRaisesRegex(full_local.FullLocalError, "IMAGE_HANDOFF_REQUIRED"),
        ):
            full_runtime.guard_existing(
                ROOT, ["docker", "compose"], model, {"owner-api"}
            )

    def test_replica_count_is_not_silently_reduced(self) -> None:
        row, model = self._existing()
        with (
            mock.patch.object(full_runtime, "containers", return_value=[row, row]),
            mock.patch.object(
                full_runtime,
                "command",
                side_effect=["owner-api original\n", "sha256:original\n"],
            ),
            self.assertRaisesRegex(
                full_local.FullLocalError, "REPLICA_HANDOFF_REQUIRED"
            ),
        ):
            full_runtime.guard_existing(
                ROOT, ["docker", "compose"], model, {"owner-api"}
            )

    def test_unchanged_incarnation_is_accepted_without_any_mutating_command(
        self,
    ) -> None:
        row, model = self._existing()
        with (
            mock.patch.object(full_runtime, "containers", return_value=[row]),
            mock.patch.object(
                full_runtime,
                "command",
                side_effect=["owner-api original\n", "sha256:original\n"],
            ) as command,
        ):
            full_runtime.guard_existing(
                ROOT, ["docker", "compose"], model, {"owner-api"}
            )
        self.assertFalse(
            any(
                "up" in call.args[0] or "stop" in call.args[0]
                for call in command.call_args_list
            )
        )

    def test_no_health_restart_no_is_not_mistaken_for_a_completed_job(self) -> None:
        model = {
            "name": "owned",
            "services": {
                "owner-api": {"restart": "no"},
                "migrator": {},
                "consumer": {
                    "depends_on": {
                        "migrator": {"condition": "service_completed_successfully"}
                    }
                },
            },
        }
        with mock.patch.object(
            full_runtime,
            "states",
            return_value={
                "owner-api": [{"Running": True, "Status": "running"}],
                "migrator": [{"Running": False, "Status": "exited", "ExitCode": 0}],
            },
        ):
            self.assertEqual(
                full_runtime.pending_services(model, {"owner-api", "migrator"}, ROOT),
                [],
            )

    def test_start_cannot_recreate_existing_containers(self) -> None:
        with mock.patch.object(full_runtime, "command") as command:
            full_runtime.start(["docker", "compose"], ["owner-api"], ROOT)
        self.assertIn("--no-recreate", command.call_args.args[0])
        self.assertIn("--pull", command.call_args.args[0])

    def test_unknown_command_result_is_reported_without_retry(self) -> None:
        import subprocess

        with (
            mock.patch.object(full_runtime, "record_command_failure"),
            mock.patch.object(
                full_runtime.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["docker"], 1200),
            ) as run,
            self.assertRaisesRegex(full_local.FullLocalError, "COMMAND_RESULT_UNKNOWN"),
        ):
            full_runtime.command(["docker", "compose", "up"], ROOT)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
