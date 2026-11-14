"""Exercise approval separation, preserved bindings and uncertain CLI outcomes."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools import full_local, spire_local, spire_renewal


class SpireRenewalTest(unittest.TestCase):
    def test_truncated_cli_response_is_an_unknown_outcome(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                spire_renewal.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["operator"], -9, b"", b"private diagnostic"
                ),
            ),
            self.assertRaisesRegex(full_local.FullLocalError, "RESULT_UNKNOWN"),
        ):
            spire_renewal.execute(["operator"], Path(directory), Path(directory))

    def test_busy_before_lock_can_wait_for_the_existing_minute_task(self) -> None:
        busy = full_local.FullLocalError(
            "FULL_RUNTIME_SPIRE_RENEWAL_TELEMETRY_RENEWAL_BUSY"
        )
        with (
            mock.patch.object(
                spire_renewal, "execute", side_effect=[busy, {"ready": True}]
            ) as execute,
            mock.patch.object(spire_renewal.time, "sleep") as sleep,
        ):
            self.assertEqual(
                spire_renewal.invoke(
                    ["same", "action"], Path("/root"), Path("/evidence")
                ),
                {"ready": True},
            )
        self.assertEqual(execute.call_args_list[0], execute.call_args_list[1])
        sleep.assert_called_once()

    def test_unknown_outcome_is_never_repeated_by_busy_wait(self) -> None:
        with (
            mock.patch.object(
                spire_renewal,
                "execute",
                side_effect=full_local.FullLocalError(
                    "FULL_RUNTIME_COMMAND_RESULT_UNKNOWN"
                ),
            ) as execute,
            mock.patch.object(spire_renewal.time, "sleep") as sleep,
            self.assertRaisesRegex(full_local.FullLocalError, "RESULT_UNKNOWN"),
        ):
            spire_renewal.invoke(["same", "action"], Path("/root"), Path("/evidence"))
        execute.assert_called_once()
        sleep.assert_not_called()

    def source(
        self, root: Path, *, public_source: bool = False, component: str | None = None
    ) -> Path:
        profiles = {}
        selected = spire_renewal.PROFILES | (
            spire_renewal.PUBLIC_SOURCE_PROFILE if public_source else {}
        )
        if component is not None:
            selected = spire_renewal.ROLE_PROFILES[component]
        for name, ids in selected.items():
            directory = root / name.removesuffix(".json")
            directory.mkdir(mode=0o700)
            reports = directory / "reports"
            reports.mkdir(mode=0o700)
            entries = []
            for key in ids:
                entry = {"id": key}
                for field in ("certificate", "key", "issuer_certificate", "issuer_key"):
                    file = directory / (key + "-" + field)
                    spire_local.private(file, (name + key + field).encode())
                    entry[field] = str(file)
                entries.append(entry)
            approval = directory / "approval.json"
            spire_local.save(
                approval,
                {
                    "schema_version": 1,
                    "environment": "test",
                    "consumer_owner": "infrastructure-spire",
                    "openssl": "/explicit/openssl",
                    "report": str(reports / name),
                    "certificates": entries,
                },
            )
            profiles[name] = {
                "approval": str(approval),
                "state": str(directory / "state"),
            }
        source = root / "renewal.json"
        spire_local.save(
            source,
            {
                "schema_version": 2 if component is not None else 1,
                "environment": "test",
                "maintenance": "operator",
                "helper_image": "sha256:" + "b" * 64,
                "profiles": profiles,
                **({"component": component} if component is not None else {}),
            },
        )
        return source

    def test_network_source_adds_separate_fourth_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self.source(Path(directory).resolve(), public_source=True)
            result = spire_renewal.configuration(source)
            self.assertEqual(len(spire_renewal.profiles(result)), 4)
            self.assertEqual(
                spire_renewal.profiles(result)["public-source.json"],
                {"public-source": ("public-source-secrets", "source.crt")},
            )

    def test_each_node_approves_only_its_component(self) -> None:
        for component in spire_renewal.ROLE_PROFILES:
            with (
                self.subTest(component=component),
                tempfile.TemporaryDirectory() as directory,
            ):
                source = self.source(Path(directory).resolve(), component=component)
                result = spire_renewal.configuration(source)
                self.assertEqual(
                    spire_renewal.profiles(result),
                    spire_renewal.ROLE_PROFILES[component],
                )

    def test_observer_node_rejects_sender_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self.source(Path(directory).resolve(), component="observer")
            value = spire_renewal.read(source)
            value["profiles"]["sender.json"] = {
                "approval": "never-read",
                "state": "never-created",
            }
            spire_local.save(source, value, replace=True)
            with self.assertRaisesRegex(
                full_local.FullLocalError, "COMPONENT_SCOPE_INVALID"
            ):
                spire_renewal.configuration(source)

    def native(self) -> dict[str, dict[str, Any]]:
        return {
            "spire-server": {
                "Image": "sha256:" + "a" * 64,
                "Config": {
                    "Labels": {
                        "io.aelev.owner": "infrastructure-spire",
                        "io.aelev.installation": "logical-test",
                        "com.docker.compose.project": "different-physical-project",
                    }
                },
            }
        }

    def reply(self, arguments: list[str], *_: Any) -> Any:
        if arguments[1:3] == ["context", "inspect"]:
            return "unix:///explicit/docker.sock"
        if "info" in arguments:
            return {"id": "00000000-0000-4000-8000-000000000000"}
        if "check" in arguments:
            return {
                "code": "TELEMETRY_RENEWAL_INSTALLATION_READY",
                "renewal_healthy": True,
            }
        return {"code": "OPERATOR_COMPLETED"}

    def test_separate_scopes_bind_logical_installation_and_operator_is_bounded(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = self.source(root)
            with (
                mock.patch.dict("os.environ", {}, clear=True),
                mock.patch.object(
                    spire_renewal.shutil, "which", return_value="/explicit/docker"
                ),
                mock.patch.object(
                    spire_renewal, "invoke", side_effect=self.reply
                ) as invoke,
            ):
                result = spire_renewal.apply(root, source, self.native())
                self.assertFalse(result["automatic_maintenance_verified"])
                self.assertEqual(result["timer_labels"], [])
                actions = [
                    call.args[0][2]
                    for call in invoke.call_args_list
                    if "renewal.py" in call.args[0][1]
                ]
                self.assertEqual(
                    actions, ["enroll", "bind-delivery", "once", "check"] * 3
                )
                before = (
                    root / "runtime/spire-renewal/status.json.binding.json"
                ).read_bytes()
                spire_renewal.apply(root, source, self.native())
                self.assertEqual(
                    before,
                    (
                        root / "runtime/spire-renewal/status.json.binding.json"
                    ).read_bytes(),
                )
            spec = json.loads(before)
            self.assertEqual(spec["project"], "logical-test")
            self.assertEqual(
                spec["report"]["volume"], "logical-test_certificate-renewal-status"
            )
            self.assertEqual(len(spec["destinations"]), 6)

    def test_copied_publisher_authority_is_rejected_before_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = self.source(root)
            profiles = spire_renewal.read(source)["profiles"]
            first = spire_renewal.read(Path(profiles["status.json"]["approval"]))
            publisher = spire_renewal.read(
                Path(profiles["status-publisher.json"]["approval"])
            )
            Path(publisher["certificates"][0]["issuer_key"]).write_bytes(
                Path(first["certificates"][0]["issuer_key"]).read_bytes()
            )
            with self.assertRaisesRegex(
                full_local.FullLocalError, "APPROVAL_SCOPE_OVERLAP"
            ):
                spire_renewal.configuration(source)

    def test_nested_state_roots_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = self.source(root)
            config = spire_renewal.read(source)
            config["profiles"]["status-query.json"]["state"] = config["profiles"][
                "status.json"
            ]["state"]
            spire_local.save(source, config, replace=True)
            with self.assertRaisesRegex(
                full_local.FullLocalError, "APPROVAL_SCOPE_OVERLAP"
            ):
                spire_renewal.configuration(source)

    def test_linux_cannot_claim_launchagent_installation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = self.source(root)
            config = spire_renewal.read(source)
            config["maintenance"] = "launchd"
            spire_local.save(source, config, replace=True)
            with (
                mock.patch.object(spire_renewal.sys, "platform", "linux"),
                self.assertRaisesRegex(
                    full_local.FullLocalError, "MACOS_TIMER_REQUIRED"
                ),
            ):
                spire_renewal.configuration(source)

    def test_changed_approval_is_rejected_without_second_cli_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = self.source(root)
            with (
                mock.patch.object(spire_renewal, "invoke", side_effect=self.reply),
                mock.patch.object(
                    spire_renewal.shutil, "which", return_value="/explicit/docker"
                ),
            ):
                spire_renewal.apply(root, source, self.native())
            approval = Path(
                spire_renewal.read(source)["profiles"]["status.json"]["approval"]
            )
            config = spire_renewal.read(approval)
            config["openssl"] = "/changed/openssl"
            spire_local.save(approval, config, replace=True)
            with (
                mock.patch.object(spire_renewal, "invoke") as invoke,
                self.assertRaisesRegex(full_local.FullLocalError, "SCOPE_CHANGED"),
            ):
                spire_renewal.apply(root, source, self.native())
            invoke.assert_not_called()

    def test_unknown_delivery_requires_inspection_before_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = self.source(root)

            def reply(arguments: list[str], *rest: Any) -> Any:
                if "once" in arguments:
                    raise full_local.FullLocalError(
                        "FULL_RUNTIME_COMMAND_RESULT_UNKNOWN"
                    )
                return self.reply(arguments, *rest)

            with (
                mock.patch.object(spire_renewal, "invoke", side_effect=reply),
                mock.patch.object(
                    spire_renewal.shutil, "which", return_value="/explicit/docker"
                ),
                self.assertRaisesRegex(full_local.FullLocalError, "RESULT_UNKNOWN"),
            ):
                spire_renewal.apply(root, source, self.native())
            with (
                mock.patch.object(spire_renewal, "invoke") as invoke,
                self.assertRaisesRegex(
                    full_local.FullLocalError, "INSPECTION_REQUIRED"
                ),
            ):
                spire_renewal.apply(root, source, self.native())
            invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
