"""Offline safety checks of the native lifecycle; no Docker resources are created."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from lifecycle import Inventory, cleanup, preflight, states


def baseline() -> Inventory:
    return {
        "containers": {},
        "networks": {},
        "volumes": {},
        "images": {"sha": {"references": "prepared:1"}},
    }


def model() -> dict[str, Any]:
    return {
        "name": "owned",
        "volumes": {"data": {"name": "data"}},
        "services": {"native": {"image": "prepared:1"}},
    }


class LifecycleSafetyTest(unittest.TestCase):
    def test_preflight_refuses_existing_containers(self) -> None:
        before = baseline()
        before["containers"]["existing"] = {"project": "owned", "state": "exited"}
        with self.assertRaisesRegex(RuntimeError, "existing native containers"):
            preflight(before, model(), True)

    def test_preflight_refuses_existing_data_without_explicit_permission(self) -> None:
        before = baseline()
        before["volumes"]["data"] = {"project": "original"}
        with self.assertRaisesRegex(RuntimeError, "reuse-existing-data"):
            preflight(before, model(), False)

    def test_preflight_refuses_other_writer(self) -> None:
        before = baseline()
        before["containers"]["writer"] = {"project": "foreign", "state": "running"}
        with patch("lifecycle.volume_mounts", return_value={"data"}):
            with self.assertRaisesRegex(RuntimeError, "another running container"):
                preflight(before, model(), True)

    def test_cleanup_preserves_original_data_and_foreign_resources(self) -> None:
        before = baseline()
        original = {"project": "owned", "anonymous": "False"}
        before["volumes"]["data"] = original
        current = baseline()
        current["volumes"] = {
            "data": original,
            "created": {"project": "owned", "anonymous": "False"},
            "foreign": {"project": "foreign", "anonymous": "False"},
        }
        with (
            patch("lifecycle.compose"),
            patch("lifecycle.inventory", side_effect=[current, before]),
            patch("lifecycle.execute") as execute,
        ):
            cleanup(before, "owned", set())
        execute.assert_called_once_with(["docker", "volume", "rm", "created"])

    def test_cleanup_preserves_original_anonymous_volume(self) -> None:
        before = baseline()
        before["volumes"]["anonymous"] = {"project": "", "anonymous": "True"}
        with (
            patch("lifecycle.compose"),
            patch("lifecycle.inventory", return_value=before),
            patch("lifecycle.execute") as execute,
        ):
            cleanup(before, "owned", {"anonymous"})
        execute.assert_not_called()

    def test_cleanup_detects_changed_original_resource(self) -> None:
        before = baseline()
        with (
            patch("lifecycle.compose"),
            patch("lifecycle.inventory", return_value=baseline()),
        ):
            before["volumes"]["missing"] = {"project": ""}
            with self.assertRaisesRegex(RuntimeError, "pre-existing resources"):
                cleanup(before, "owned", set())

    def test_compose_rows_accept_array_and_line_protocol(self) -> None:
        for output in ('[{"Service":"native"}]', '{"Service":"native"}\n'):
            with patch("lifecycle.compose", return_value=output):
                self.assertEqual(set(states()), {"native"})


if __name__ == "__main__":
    unittest.main()
