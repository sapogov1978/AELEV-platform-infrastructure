"""Safe native CI evidence retains phases without publishing runtime payloads."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

SPEC = importlib.util.spec_from_file_location(
    "fencing_ssh_lifecycle", Path(__file__).with_name("fencing-ssh-lifecycle.py")
)
assert SPEC and SPEC.loader
lifecycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lifecycle)


@pytest.mark.parametrize(
    "untrusted", ["private-credential-payload", {"private": "payload"}, ["private"]]
)
def test_intent_diagnostic_cannot_echo_unknown_payloads(untrusted: Any) -> None:
    result = lifecycle.safe_intent(
        {
            "node": "postgresql-1",
            "phase": untrusted,
            "start_attempts": True,
            "last_return_code": untrusted,
            "claim": {"candidate": untrusted, "credential": "DO_NOT_PUBLISH"},
            "container_id": "DO_NOT_PUBLISH",
            "private_key": "DO_NOT_PUBLISH",
        }
    )
    assert result == {
        "node": "postgresql-1",
        "phase": "UNKNOWN",
        "start_attempts": None,
        "last_return_code": "UNRECOGNIZED",
        "candidate": None,
        "restart_required": False,
    }
    assert "DO_NOT_PUBLISH" not in json.dumps(result)


def test_intent_retains_phase_budget_and_known_failure_code() -> None:
    result = lifecycle.safe_intent(
        {
            "node": "postgresql-2",
            "phase": "STOP_CONFIRMED",
            "start_attempts": 1,
            "last_return_code": "LEASE_CHANGED",
            "claim": {"candidate": "postgresql-3"},
            "restart_required": True,
        }
    )
    assert result["phase"] == "STOP_CONFIRMED"
    assert result["start_attempts"] == 1
    assert result["last_return_code"] == "LEASE_CHANGED"
    assert result["candidate"] == "postgresql-3"


def test_audit_diagnostic_counts_only_known_events_and_codes() -> None:
    result = lifecycle.safe_audit_counts(
        [
            {
                "phase": "return_blocked",
                "reason_code": "DCS_QUORUM_UNCONFIRMED",
                "claim": {"private": "DO_NOT_PUBLISH"},
            },
            {"phase": "DO_NOT_PUBLISH", "reason_code": {"private": "DO_NOT_PUBLISH"}},
        ]
    )
    assert result == {
        "return_blocked:DCS_QUORUM_UNCONFIRMED": 1,
        "UNKNOWN:UNRECOGNIZED": 1,
    }


def test_runtime_diagnostic_excludes_configuration_and_raw_logs() -> None:
    result = lifecycle.safe_runtime_state(
        {
            "State": {
                "Running": False,
                "OOMKilled": True,
                "ExitCode": 137,
                "Health": {
                    "Status": "unhealthy",
                    "Log": [{"Output": "DO_NOT_PUBLISH"}],
                },
            },
            "RestartCount": 0,
            "Config": {"Env": ["SECRET=DO_NOT_PUBLISH"]},
        },
        "DO_NOT_PUBLISH SSH_RETURN_UNCONFIRMED SSH_RETURN_UNCONFIRMED",
    )
    assert result["running"] is False
    assert result["oom_killed"] is True
    assert result["exit_code"] == 137
    assert result["health"] == "unhealthy"
    assert result["reason_code_counts"] == {"SSH_RETURN_UNCONFIRMED": 2}
    assert "DO_NOT_PUBLISH" not in json.dumps(result)
