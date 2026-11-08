"""Safe failure classification for the destructive PostgreSQL lifecycle driver."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PATH = Path(__file__).with_name("cluster-lifecycle.py")
SPEC = importlib.util.spec_from_file_location("cluster_lifecycle", PATH)
assert SPEC and SPEC.loader
cluster_lifecycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cluster_lifecycle)


@pytest.mark.parametrize(
    ("message", "code"),
    [
        (
            "ValueError: Persisted cluster safety configuration does not match the delivery",
            "PERSISTED_SAFETY_CONFIG_MISMATCH",
        ),
        (
            "ValueError: No reachable Patroni configuration endpoint",
            "PATRONI_CONFIG_UNREACHABLE",
        ),
        ("HTTP Error 401: Unauthorized", "PATRONI_AUTHORIZATION_FAILED"),
        ("certificate verify failed", "TLS_VERIFICATION_FAILED"),
        ("connection refused", "CONNECTION_REFUSED"),
        ("AssertionError", "ASSERTION_FAILED"),
        ("token=must-not-survive", "PROCESS_FAILED"),
    ],
)
def test_failure_output_is_reduced_to_fixed_code(message: str, code: str) -> None:
    assert cluster_lifecycle.classify_command_failure(message) == code


def test_command_failure_never_exposes_raw_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1,
            stdout="credential=must-not-survive\n",
            stderr="ValueError: No reachable Patroni configuration endpoint\n",
        )

    monkeypatch.setattr(cluster_lifecycle.subprocess, "run", failed)
    with pytest.raises(
        RuntimeError,
        match=(
            "VERIFY_CLUSTER_SAFETY failed "
            r"\(exit 1; reason=PATRONI_CONFIG_UNREACHABLE\)"
        ),
    ) as failure:
        cluster_lifecycle.command(
            "docker", "compose", operation="VERIFY_CLUSTER_SAFETY"
        )
    assert "must-not-survive" not in str(failure.value)


def test_failure_signals_exclude_private_native_output() -> None:
    output = (
        "secret=must-not-survive https://private.invalid\n"
        "reason_code=LEASE_CHANGED\n"
        "reason_code=ARBITRARY_PRIVATE_TEXT\n"
        "pre_promote script failed\nPermission denied\n"
    )
    assert cluster_lifecycle.failure_signals(output) == {
        "PRE_PROMOTE_FAILED": 1,
        "PRE_PROMOTE_COMMAND": 1,
        "LEASE_CHANGED": 1,
        "PERMISSION_DENIED": 1,
    }
