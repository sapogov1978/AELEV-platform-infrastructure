"""Reject foreign normative dependencies without rewriting historical documents."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from check_documentation import documentation_boundary_errors, FROZEN_SDK_DOCUMENTS


class DocumentationBoundaryTest(unittest.TestCase):
    def test_composition_cannot_reintroduce_a_specific_translation_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "infrastructure.mmd"
            path.write_text('flowchart LR\n Root --> Translator["Index 9B/worker"]\n')
            self.assertTrue(documentation_boundary_errors(root))
            path.write_text(
                'flowchart LR\n Root --> Translator["Opaque translation port"]\n'
            )
            self.assertEqual(documentation_boundary_errors(root), [])

    def test_consumer_contracts_and_foreign_normative_links_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "docs/api/events.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "Deep Screening uses discovery.example.v1. "
                "https://github.com/sapogov1978/discovery-research-platform/blob/commit/contract.md"
            )
            errors = documentation_boundary_errors(root)
            self.assertTrue(any("consumer repository" in error for error in errors))
            self.assertTrue(any("Consumer contract" in error for error in errors))
            path.write_text(
                "Owner-controlled translation.requests.v2; https://kafka.apache.org/39/documentation/"
            )
            self.assertEqual(documentation_boundary_errors(root), [])

    def test_historical_report_is_not_a_normative_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text = "https://github.com/sapogov1978/AELEV/blob/commit/historical.md"
            report = root / "test_infrastructure/result-20261001.md"
            report.parent.mkdir()
            report.write_text(text)
            self.assertEqual(documentation_boundary_errors(root), [])
            (root / "README.md").write_text(text)
            self.assertTrue(documentation_boundary_errors(root))

    def test_frozen_sdk_exception_requires_unchanged_bytes_and_current_guide(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hashes = {}
            for name in FROZEN_SDK_DOCUMENTS:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "https://github.com/sapogov1978/AELEV/blob/old/provenance.md"
                )
                hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            record = root / "docs/audit/frozen-sdk-documents.json"
            record.parent.mkdir(parents=True)
            record.write_text(
                json.dumps(
                    {"schema_version": 1, "version": "0.1.4", "documents": hashes}
                )
            )
            (root / "platform-ui/SDK.md").write_text(
                "# Current infrastructure SDK contract"
            )
            self.assertEqual(documentation_boundary_errors(root), [])
            (root / sorted(FROZEN_SDK_DOCUMENTS)[0]).write_text("changed")
            self.assertTrue(
                any(
                    "Immutable SDK document changed" in error
                    for error in documentation_boundary_errors(root)
                )
            )
