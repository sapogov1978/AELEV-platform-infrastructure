"""Only the cell's own Prometheus child may be stopped; every wait is bounded."""

from __future__ import annotations

import subprocess
from unittest.mock import Mock, call

import pytest

from delivery import failure_reason, stop_prometheus


def test_pressure_shutdown_escalates_when_sigterm_times_out() -> None:
    child = Mock(spec=subprocess.Popen)
    child.poll.return_value = None
    child.wait.side_effect = [subprocess.TimeoutExpired("prometheus", 30), 0]
    stop_prometheus(child)
    child.terminate.assert_called_once_with()
    child.kill.assert_called_once_with()
    assert child.wait.call_args_list == [call(timeout=30), call(timeout=5)]


def test_graceful_shutdown_never_kills() -> None:
    child = Mock(spec=subprocess.Popen)
    child.poll.return_value = None
    stop_prometheus(child)
    child.terminate.assert_called_once_with()
    child.kill.assert_not_called()


def test_already_exited_child_is_not_signalled() -> None:
    child = Mock(spec=subprocess.Popen)
    child.poll.return_value = 0
    stop_prometheus(child)
    child.terminate.assert_not_called()
    child.kill.assert_not_called()


def test_even_forced_shutdown_has_a_deadline() -> None:
    child = Mock(spec=subprocess.Popen)
    child.poll.return_value = None
    child.wait.side_effect = subprocess.TimeoutExpired("prometheus", 30)
    with pytest.raises(subprocess.TimeoutExpired):
        stop_prometheus(child)
    child.kill.assert_called_once_with()


def test_failure_category_never_contains_exception_text() -> None:
    assert failure_reason(TimeoutError("private endpoint")) == "transport_timeout"
    assert failure_reason(ValueError("private payload")) == "integrity_failure"
