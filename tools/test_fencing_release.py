"""Exercise the actual release preflight before any registry mutation."""

from __future__ import annotations

from typing import Any
import unittest

from workflow_admission import AdmissionError, require_success

REVISION = "1" * 40


class FencingReleaseGateTests(unittest.TestCase):
    def runs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "head_sha": REVISION,
                "run_number": 2,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "success",
            }
            for name in ("audit", "postgresql-fencing-ssh-native")
        ]

    def admitted(self, runs: list[dict[str, Any]]) -> bool:
        try:
            require_success(runs, REVISION, ("audit", "postgresql-fencing-ssh-native"))
            return True
        except AdmissionError:
            return False

    def test_exact_source_success(self) -> None:
        self.assertTrue(self.admitted(self.runs()))

    def test_success_for_another_source_is_not_admission(self) -> None:
        self.assertFalse(
            self.admitted([{**run, "head_sha": "2" * 40} for run in self.runs()])
        )

    def test_missing_native_result_is_not_admission(self) -> None:
        self.assertFalse(self.admitted(self.runs()[:1]))

    def test_newer_failed_native_result_blocks_old_success(self) -> None:
        runs = self.runs()
        runs.append({**runs[1], "run_number": 3, "conclusion": "failure"})
        self.assertFalse(self.admitted(runs))

    def test_newer_pending_audit_blocks_old_success(self) -> None:
        runs = self.runs()
        runs.append(
            {**runs[0], "run_number": 3, "status": "in_progress", "conclusion": None}
        )
        self.assertFalse(self.admitted(runs))

    def test_latest_successful_rerun_is_admitted(self) -> None:
        runs = self.runs()
        runs.append({**runs[1], "run_number": 3, "run_attempt": 2})
        self.assertTrue(self.admitted(runs))


if __name__ == "__main__":
    unittest.main()
