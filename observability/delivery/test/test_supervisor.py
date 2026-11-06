"""A stale stall observation cannot terminate an already recovered loop."""

from __future__ import annotations

import signal
from typing import Any
from unittest.mock import Mock

import pytest

import delivery


@pytest.mark.parametrize(
    "recovery_at", ["context", "history", "different-loop", "never"]
)
def test_supervisor_rechecks_failure_after_diagnostics_and_durable_history(
    monkeypatch: pytest.MonkeyPatch, recovery_at: str
) -> None:
    clock = 0.0
    state = "WORKER_STALLED"
    handlers: dict[int, Any] = {}
    stop, agent, endpoint, thread = Mock(), Mock(), Mock(), Mock()
    agent.stop = stop
    settings = Mock(mode="sender", prometheus_command=[], timeout_seconds=2)

    def wait(seconds: float) -> bool:
        nonlocal clock
        clock += 2
        if clock > 6:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return True
        return False

    def failure(*args: object) -> dict[str, str] | None:
        return {"code": state, "phase": "RETRY_WAIT"} if state else None

    def context(*args: object) -> dict[str, str | float]:
        nonlocal state
        if recovery_at == "context":
            state = ""
        elif recovery_at == "different-loop":
            state = "HEALTH_LISTENER_STALLED"
        return {"worker_phase": "RETRY_WAIT", "worker_progress_age_seconds": 20.0}

    def history(*args: object) -> None:
        nonlocal state
        if recovery_at == "history":
            state = ""

    stop.wait.side_effect = wait
    monkeypatch.setattr(delivery, "Delivery", Mock(return_value=agent))
    monkeypatch.setattr(delivery, "DeliveryServer", Mock(return_value=endpoint))
    monkeypatch.setattr(delivery.threading, "Thread", Mock(return_value=thread))
    monkeypatch.setattr(
        delivery.signal, "signal", lambda code, fn: handlers.update({code: fn})
    )
    monkeypatch.setattr(delivery.time, "monotonic", lambda: clock)
    monkeypatch.setattr(delivery, "clear_heartbeat", Mock())
    monkeypatch.setattr(delivery, "publish_heartbeat", Mock())
    monkeypatch.setattr(delivery, "stop_listener", Mock())
    observed = Mock(side_effect=failure)
    recorded = Mock(side_effect=history)
    monkeypatch.setattr(delivery, "critical_failure", observed)
    monkeypatch.setattr(delivery, "failure_context", context)
    monkeypatch.setattr(delivery, "record_failure", recorded)
    if recovery_at == "never":
        with pytest.raises(RuntimeError, match="WORKER_STALLED"):
            delivery.run(settings)
    else:
        delivery.run(settings)
    assert observed.call_count >= 4
    assert recorded.call_count == int(recovery_at in {"history", "never"})
    thread.join.assert_called_once_with(timeout=3)
