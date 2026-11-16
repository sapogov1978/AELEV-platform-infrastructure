"""Verify the real private diagnostic producer without exporting its messages."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import lifecycle
from runtime_diagnostic import (
    LIMIT,
    command_failure_summaries,
    failure_summary,
    runtime_progress,
)
from tools import full_runtime


def test_real_command_failure_keeps_only_known_exception_type(tmp_path: Path) -> None:
    process = subprocess.run(
        [sys.executable, "-c", "raise ValueError('private-message-must-not-survive')"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 1
    full_runtime.record_command_failure(
        tmp_path, process.stdout, process.stderr, unknown=False
    )
    records = command_failure_summaries(tmp_path)
    assert records == [
        {
            "status": "FAILED",
            "error_types": ["ValueError"],
            "http_statuses": [],
            "categories": [],
        }
    ]
    assert "private" not in json.dumps(records)


def test_unknown_result_and_http_failure_remain_distinct(tmp_path: Path) -> None:
    full_runtime.record_command_failure(
        tmp_path,
        "private-output",
        "httpx.HTTPStatusError: Client error '401 Unauthorized' for private-endpoint\nProvisioning failed: privateCredential",
        unknown=True,
    )
    assert command_failure_summaries(tmp_path) == [
        {
            "status": "UNKNOWN",
            "error_types": ["HTTPStatusError"],
            "http_statuses": [401],
            "categories": [],
        }
    ]


def test_progress_uses_public_catalog_and_means_started(tmp_path: Path) -> None:
    (tmp_path / "deployment-catalog.json").write_text(
        json.dumps({"roles": [{"id": "grafana-oidc-provision"}]})
    )
    assert runtime_progress(
        "[full-runtime] starting Kafka\n[full-runtime] provisioning grafana-oidc-provision\nprivate-output",
        tmp_path,
    ) == {"step": "PROVISION_OWNER_TASK", "task": "grafana-oidc-provision"}
    assert runtime_progress(
        "[full-runtime] provisioning privateCredential", tmp_path
    ) == {"step": "PROVISION_OWNER_TASK"}
    assert runtime_progress("private-output", tmp_path) == {}


def test_reader_rejects_links_oversized_files_and_unknown_codes(tmp_path: Path) -> None:
    directory = tmp_path / "runtime/full-runtime-failure-owned"
    directory.mkdir(parents=True)
    (directory / "result.json").write_text('{"status":"FAILED"}')
    private = tmp_path / "private"
    private.write_text("ValueError: must-not-survive")
    (directory / "stdout").symlink_to(private)
    (directory / "stderr").write_bytes(b"x" * (LIMIT + 1))
    assert command_failure_summaries(tmp_path) == [
        {"status": "FAILED", "error_types": [], "http_statuses": [], "categories": []}
    ]
    (directory / "stderr").write_text(
        "PrivateException: must-not-survive\nSPIRE_UNKNOWN_PRIVATE"
    )
    assert command_failure_summaries(tmp_path)[0]["error_types"] == []


def test_reader_refuses_symlinked_runtime_root(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "runtime").symlink_to(external)
    assert command_failure_summaries(root) == []


@pytest.mark.parametrize(
    ("raw", "category"),
    [
        (
            "cannot identify the existing site-replication state: private",
            "REPLICATION_STATE_UNRECOGNIZED",
        ),
        (
            "existing site replication does not contain the configured site B endpoint",
            "REPLICATION_TOPOLOGY_REJECTED",
        ),
        (
            "required MinIO secret file is missing, empty, or unreadable: private",
            "SECRET_INPUT_UNAVAILABLE",
        ),
    ],
)
def test_known_minio_guards_survive_only_as_categories(raw: str, category: str) -> None:
    assert failure_summary(raw) == {
        "error_types": [],
        "http_statuses": [],
        "categories": [category],
    }


def test_group_failure_preserves_context_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "deployment-catalog.json").write_text(
        json.dumps({"roles": [{"id": "grafana-oidc-provision"}]})
    )
    full_runtime.record_command_failure(
        tmp_path, "", "Provisioning failed: ConnectError\nprivate-output", unknown=False
    )
    monkeypatch.setattr(lifecycle, "ROOT", tmp_path)
    monkeypatch.setattr(lifecycle, "states", lambda _: {})
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(
            returncode=2,
            stdout="",
            stderr="[full-runtime] provisioning grafana-oidc-provision\n[infrastructure] FULL_RUNTIME_COMMAND_FAILED",
        ),
    )
    output = tmp_path / "public/failure.json"
    with pytest.raises(RuntimeError, match="FULL_RUNTIME_COMMAND_FAILED"):
        lifecycle.execute_group_start(
            ["docker", "compose"], "owned", {}, output, "initial"
        )
    payload = json.loads(output.read_text())
    assert (
        payload["runtime_context"]["last_announced_step"]["task"]
        == "grafana-oidc-provision"
    )
    assert payload["runtime_context"]["commands"][0]["error_types"] == ["ConnectError"]
    assert "private" not in output.read_text()
