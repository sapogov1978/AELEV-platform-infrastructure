"""Completed work, not a successful upstream or a phase marker, proves life."""

import threading
from typing import Any
from unittest.mock import Mock

import pytest

from aelev_spire import observation
from aelev_spire.observation_diagnostics import Diagnostics


@pytest.fixture
def worker_state(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    clock = [120.0]
    monkeypatch.setattr(observation.time, "monotonic", lambda: clock[0])
    worker = Mock(spec=threading.Thread)
    worker.is_alive.return_value = True
    return {
        "worker": worker,
        "progress": [100.0],
        "clock": clock,
        "diagnostics": Mock(spec=Diagnostics),
    }


def test_recovery_during_diagnostics_cancels_stale_exit(
    worker_state: dict[str, Any],
) -> None:
    value = worker_state

    def record(*_args: Any, **_kwargs: Any) -> None:
        value["progress"][0] = value["clock"][0]

    value["diagnostics"].record.side_effect = record
    assert observation.supervise_worker(
        value["worker"], value["progress"], value["diagnostics"], 119.5, None
    )
    value["diagnostics"].record.assert_called_once()


def test_actual_stall_is_not_hidden_by_diagnostic_capture(
    worker_state: dict[str, Any],
) -> None:
    value = worker_state
    with pytest.raises(ValueError, match="SPIRE_OBSERVATION_WORKER_STALLED"):
        observation.supervise_worker(
            value["worker"], value["progress"], value["diagnostics"], 119.5, None
        )


def test_changed_failure_cannot_publish_positive_heartbeat(
    worker_state: dict[str, Any],
) -> None:
    value = worker_state

    def record(*_args: Any, **_kwargs: Any) -> None:
        value["worker"].is_alive.return_value = False

    value["diagnostics"].record.side_effect = record
    assert not observation.supervise_worker(
        value["worker"], value["progress"], value["diagnostics"], 119.5, None
    )
    with pytest.raises(ValueError, match="SPIRE_OBSERVATION_WORKER_STOPPED"):
        observation.supervise_worker(
            value["worker"], value["progress"], value["diagnostics"], 119.5, None
        )


def test_several_completed_bounded_steps_are_not_one_long_stall(
    worker_state: dict[str, Any],
) -> None:
    value = worker_state
    for _ in range(6):
        value["progress"][0] = value["clock"][0]
        value["clock"][0] += 7
        assert observation.supervise_worker(
            value["worker"], value["progress"], value["diagnostics"], 119.5, None
        )
    value["diagnostics"].record.assert_not_called()
    value["clock"][0] += 8
    assert (
        observation.worker_failure(value["worker"], value["progress"][0])
        == "SPIRE_OBSERVATION_WORKER_STALLED"
    )


def test_dead_worker_is_failed_even_with_fresh_progress(
    worker_state: dict[str, Any],
) -> None:
    value = worker_state
    value["worker"].is_alive.return_value = False
    assert (
        observation.worker_failure(value["worker"], value["clock"][0])
        == "SPIRE_OBSERVATION_WORKER_STOPPED"
    )
