"""Structured diagnosis never changes recovery decisions or exposes payloads."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery
from test_recovery import enrolled as base_enrolled_fixture  # noqa: F401
from test_recovery import restarts, tick


@pytest.fixture
def fixture(request: pytest.FixtureRequest) -> dict[str, Any]:
    return cast(dict[str, Any], request.getfixturevalue("base_enrolled_fixture"))


def probes() -> list[dict[str, Any]]:
    return [
        {
            "Start": "1970-01-01T00:16:39Z",
            "End": "1970-01-01T00:16:40Z",
            "ExitCode": number,
            "Output": "private-probe-output",
        }
        for number in range(5)
    ]


def test_probe_snapshot_is_bounded_and_has_no_payload(fixture: dict[str, Any]) -> None:
    row = copy.deepcopy(fixture["row"])
    row["State"].update(ExitCode=137, OOMKilled=True)
    row["State"]["Health"].update(Status="unhealthy", Log=probes())
    row["Config"].update(Env=["private-environment"], Cmd=["private-argument"])
    result = recovery.diagnostic(
        row, "PROBE_CHECK", time.monotonic(), "UNHEALTHY", RuntimeError("private-error")
    )
    assert result["error_type"] == "RuntimeError"
    assert result["oom_killed"] and result["process_exit_code"] == 137
    assert [r["exit_code"] for r in result["probes"]] == [2, 3, 4]
    assert all(r["seconds"] == 1 for r in result["probes"])
    assert "private-" not in json.dumps(result)


def test_inspection_exception_has_phase_without_text(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        recovery, "inspect", Mock(side_effect=KeyError("private-error"))
    )
    result = tick(fixture, 1000)
    detail = result["last_failure_diagnostic"]
    assert result["code"] == "RUNTIME_CHECK_FAILED" and result["attempts"] == 0
    assert detail["phase"] == "INSPECT" and detail["error_type"] == "KeyError"
    report = json.loads((fixture["root"] / "report.json").read_bytes())
    assert report["targets"]["worker"]["last_failure_diagnostic"] == detail
    assert "private-error" not in (fixture["root"] / "state.json").read_text()
    assert restarts(fixture) == 0


def test_pre_action_evidence_is_durable_before_dispatch(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = recovery.docker
    fixture["row"]["State"]["Health"]["Log"] = probes()
    observed = []

    def dispatch(state: dict[str, Any], *args: str) -> bytes:
        if args[0] == "restart":
            stored = json.loads((fixture["root"] / "state.json").read_bytes())
            target = stored["targets"]["worker"]
            evidence = target["last_action_diagnostic"]
            assert target["restart_intent"]["phase"] == "DISPATCH_INTENT"
            assert evidence["phase"] == "BEFORE_RESTART"
            assert evidence["code"] == "UNHEALTHY"
            assert len(evidence["probes"]) == 3
            assert any(e["code"] == "PRE_ACTION_DIAGNOSTIC" for e in stored["events"])
            observed.append(evidence)
        return original(state, *args)

    monkeypatch.setattr(recovery, "docker", dispatch)
    tick(fixture, 1000)
    result = tick(fixture, 1030)
    assert len(observed) == restarts(fixture) == result["attempts"] == 1
    assert "private-probe-output" not in (fixture["root"] / "state.json").read_text()


def test_circuit_preserves_original_evidence(fixture: dict[str, Any]) -> None:
    tick(fixture, 1000)
    fixture["row"]["RestartCount"] = 3
    result = tick(fixture, 1010)
    evidence = result["last_failure_diagnostic"]
    assert evidence["phase"] == "NATIVE_ACCOUNTING"
    assert evidence["code"] == "NATIVE_RESTART_OBSERVED"
    assert result["code"] == "CIRCUIT_OPEN"
    assert tick(fixture, 2000, "healthy")["last_failure_diagnostic"] == evidence
    assert restarts(fixture) == 0


def test_invalid_probe_does_not_mask_the_original_error(
    fixture: dict[str, Any],
) -> None:
    fixture["row"]["State"]["Health"]["Log"] = [
        {"End": "private-invalid-timestamp", "Output": "private-output"}
    ]
    result = tick(fixture, 1000, "healthy")
    assert result["code"] == "RUNTIME_CHECK_FAILED"
    assert result["last_failure_diagnostic"]["phase"] == "PROBE_CHECK"
    assert result["last_failure_diagnostic"]["error_type"] == "ValueError"
    assert "private-" not in json.dumps(result)
    assert restarts(fixture) == 0


def test_guard_timeout_retains_phase_and_budget(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = fixture["root"] / "guard.py"
    path.write_text("# fixed read-only guard\n")
    state = json.loads((fixture["root"] / "state.json").read_bytes())
    state["targets"]["worker"]["guard"] = {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    recovery.commit(fixture["root"], state)
    monkeypatch.setattr(
        recovery,
        "execute",
        Mock(side_effect=subprocess.TimeoutExpired(["private-command"], 50)),
    )
    tick(fixture, 1000)
    result = tick(fixture, 1030)
    assert result["code"] == "OWNER_GUARD_BLOCKED" and result["attempts"] == 0
    assert result["last_failure_diagnostic"]["phase"] == "OWNER_GUARD"
    assert result["last_failure_diagnostic"]["error_type"] == "TimeoutExpired"
    assert "private-command" not in json.dumps(result)
    assert restarts(fixture) == 0


@pytest.mark.parametrize("value", [None, {}, {"State": []}, {"State": {"Status": []}}])
def test_malformed_diagnostic_source_is_harmless(value: dict[str, Any] | None) -> None:
    result = recovery.diagnostic(
        value, "INSPECT", time.monotonic(), "RUNTIME_CHECK_FAILED"
    )
    assert result["code"] == "RUNTIME_CHECK_FAILED"
