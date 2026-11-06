from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.build_observability_tools_patch import build


ROOT = Path(__file__).resolve().parents[1]


class ObservabilityToolsPatchTest(unittest.TestCase):
    def test_reproducible_patch_adds_required_identifiers(self) -> None:
        cases = (
            ("delivery", "1.0.0", "1.0.1"),
            ("blackbox-exporter", "1.1.2", "1.1.3"),
        )
        for owner, base_version, patch_version in cases:
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as directory:
                base = ROOT / (
                    f"observability/{owner}/vendor/"
                    f"aelev_observability_tools-{base_version}-py3-none-any.whl"
                )
                first = Path(directory) / "first.whl"
                second = Path(directory) / "second.whl"
                build(base, patch_version, first)
                build(base, patch_version, second)
                self.assertEqual(first.read_bytes(), second.read_bytes())
                with zipfile.ZipFile(first) as archive:
                    source = archive.read(
                        "aelev_observability_tools/__init__.py"
                    ).decode()
                    metadata = archive.read(
                        f"aelev_observability_tools-{patch_version}.dist-info/METADATA"
                    ).decode()
                self.assertIn("subjectKeyIdentifier=hash", source)
                self.assertIn("authorityKeyIdentifier=keyid,issuer", source)
                self.assertIn("authorityKeyIdentifier=keyid:always", source)
                self.assertIn(f"Version: {patch_version}", metadata)


if __name__ == "__main__":
    unittest.main()
