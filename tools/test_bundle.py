"""The published flat bundle must match one commit and exclude private/dev state."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from build_bundle import build


class FlatBundleTest(unittest.TestCase):
    def test_flat_source_and_version_come_from_the_same_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "source"
            root.mkdir()
            for name, data in {
                "release.json": '{"version":"0.2.0"}\n',
                "kafka/compose.yml": "services: {}\n",
                "docs/README.md": "# Documentation\n",
                "graphify-out/graph.json": "{}\n",
                ".codex/skill.md": "development\n",
                ".gitignore": ".env\n",
            }.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(data)
            (root / ".env").write_text("LOCAL_STATE=excluded\n")
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=sapogov1978",
                    "-c",
                    "user.email=60944481+sapogov1978@users.noreply.github.com",
                    "commit",
                    "-m",
                    "fixture",
                ],
                check=True,
                capture_output=True,
            )
            (root / "release.json").write_text('{"version":"uncommitted"}\n')
            output = Path(temporary) / "bundle.zip"
            lock = build(root, output)
            self.assertEqual(lock["schema_version"], 2)
            self.assertEqual(lock["version"], "0.2.0")
            self.assertEqual(
                set(lock["files"]),
                {".gitignore", "release.json", "kafka/compose.yml", "docs/README.md"},
            )
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(
                    json.loads(archive.read("release.json"))["version"], "0.2.0"
                )


if __name__ == "__main__":
    unittest.main()
