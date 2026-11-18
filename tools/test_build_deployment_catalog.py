"""Explicit owner scopes separate quickstart processes from full placement."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from build_deployment_catalog import build
except ModuleNotFoundError as error:
    if error.name != "yaml":
        raise
    build = None


@unittest.skipIf(build is None, "requires pinned tools/requirements-catalog.txt")
class BuildDeploymentCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.owner = self.root / "technical-owner"
        self.owner.mkdir()
        self.manifest = self.root / "owners.json"
        self.manifest.write_text(json.dumps({"owners": ["technical-owner"]}))

    def catalog(
        self,
        document: str,
        overlay: str = "",
        *,
        node_roles: set[str] | None = None,
        opaque_roles: set[str] | None = None,
    ) -> dict:
        recipe = self.owner / "compose.yml"
        recipe.write_text(document)
        files = [recipe.relative_to(self.root).as_posix()]
        if overlay:
            second = self.owner / "compose.client.yml"
            second.write_text(overlay)
            files.append(second.relative_to(self.root).as_posix())
        with patch("build_deployment_catalog.subprocess.check_output") as listed:
            listed.return_value = "\0".join(files).encode() + b"\0"
            assert build is not None
            return build(
                self.root,
                self.manifest,
                "technical-product",
                node_roles or set(),
                set(),
                opaque_roles or set(),
            )

    def test_unannotated_roles_remain_required(self) -> None:
        result = self.catalog("services:\n  worker: {}\n  database: {}\n")
        self.assertEqual([row["id"] for row in result["roles"]], ["database", "worker"])
        self.assertTrue(all(row["kind"] == "runtime" for row in result["roles"]))

    def test_explicit_quickstart_role_is_not_a_full_runtime_requirement(self) -> None:
        result = self.catalog(
            "services:\n  worker: {}\n  local-database:\n"
            "    labels: {io.aelev.deployment.scope: quickstart}\n",
            "services:\n  local-database:\n    ports: ['25432:5432']\n",
        )
        self.assertEqual([row["id"] for row in result["roles"]], ["worker"])
        for name, expected in result["sources"].items():
            self.assertEqual(
                hashlib.sha256((self.root / name).read_bytes()).hexdigest(), expected
            )
        self.assertIn("technical-owner/compose.client.yml", result["sources"])

    def test_list_label_syntax_preserves_explicit_full_scope(self) -> None:
        result = self.catalog(
            "services:\n  worker:\n    labels: [io.aelev.deployment.scope=full]\n"
        )
        self.assertEqual([row["id"] for row in result["roles"]], ["worker"])

    def test_conflicting_overlay_scope_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflicting deployment scopes"):
            self.catalog(
                "services:\n  worker:\n    labels: {io.aelev.deployment.scope: full}\n",
                "services:\n  worker:\n"
                "    labels: {io.aelev.deployment.scope: quickstart}\n",
            )

    def test_misspelled_scope_cannot_silently_hide_a_role(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid deployment scope"):
            self.catalog(
                "services:\n  worker:\n    labels: {io.aelev.deployment.scope: quick}\n"
            )

    def test_empty_full_catalogue_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires roles"):
            self.catalog(
                "services:\n  worker:\n"
                "    labels: {io.aelev.deployment.scope: quickstart}\n"
            )

    def test_node_and_opaque_requirements_cannot_be_hidden(self) -> None:
        document = (
            "services:\n  worker: {}\n  protected:\n"
            "    labels: {io.aelev.deployment.scope: quickstart}\n"
        )
        for keyword in ("node_roles", "opaque_roles"):
            with (
                self.subTest(kind=keyword),
                self.assertRaisesRegex(ValueError, "cannot be quickstart-only"),
            ):
                if keyword == "node_roles":
                    self.catalog(document, node_roles={"protected"})
                else:
                    self.catalog(document, opaque_roles={"protected"})


if __name__ == "__main__":
    unittest.main()
