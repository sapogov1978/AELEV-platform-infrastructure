"""Exercise the actual release preflight before any registry mutation."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import re
import textwrap
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
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
        workflow = (
            ROOT / ".github/workflows/postgresql-fencing-release.yml"
        ).read_text()
        match = re.search(r"(?ms)^([ ]+)python - <<'PY'\n(.*?)^\1PY\s*$", workflow)
        self.assertIsNotNone(match)
        assert match is not None
        code = compile(textwrap.dedent(match.group(2)), "release-workflow", "exec")
        with (
            patch.dict(
                os.environ,
                GITHUB_REPOSITORY="sapogov1978/AELEV-platform-infrastructure",
                GITHUB_SHA=REVISION,
                GH_TOKEN="offline-test-token",
            ),
            patch(
                "urllib.request.urlopen",
                return_value=io.StringIO(json.dumps({"workflow_runs": runs})),
            ) as request,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            try:
                exec(code, {})
                result = True
            except SystemExit:
                result = False
        request.assert_called_once()
        self.assertEqual(
            request.call_args.args[0].full_url,
            "https://api.github.com/repos/sapogov1978/AELEV-platform-infrastructure/actions/runs?per_page=100",
        )
        return result

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
