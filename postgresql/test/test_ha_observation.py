"""Distinguish a fenced connection from split brain and verify the fault target."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

import ha_observation as observation


class Connection:
    def __init__(self, values: list[Any]) -> None:
        self.values = list(values)
        self.closed = False

    def execute(self, query: str) -> Connection:
        assert query == "SELECT pg_is_in_recovery()"
        return self

    def fetchone(self) -> tuple[Any]:
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return (value,)

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "first",
    [
        [False, psycopg.OperationalError("fenced")],
        [False, False, psycopg.OperationalError("fenced between samples")],
    ],
)
def test_old_then_new_primary_does_not_reconnect_or_report_split_brain(
    first: list[Any],
) -> None:
    connections = {
        1: Connection(first),
        2: Connection([False, False]),
        3: Connection([True]),
    }
    calls: list[int] = []

    def connect(node: int) -> Connection:
        calls.append(node)
        return connections[node]

    assert observation.observe_roles(connect) == {2: False, 3: True}
    assert calls == [1, 2, 3]
    assert all(connection.closed for connection in connections.values())


def test_two_primary_processes_still_raise_on_the_same_live_connections() -> None:
    connections = {
        1: Connection([False, False, False]),
        2: Connection([False, False]),
        3: Connection([True]),
    }
    with pytest.raises(
        AssertionError, match="MULTIPLE_SQL_WRITERS_ON_LIVE_CONNECTIONS"
    ):
        observation.observe_roles(connections.__getitem__)
    assert all(connection.closed for connection in connections.values())


def proc(root: Path, owners: list[int]) -> Path:
    (root / "net").mkdir()
    (root / "net/tcp").write_text(
        "header\n0: 00000000:2328 00000000:0000 0A 0:0 0:0 0 70 0 9910\n"
    )
    for pid in owners:
        directory = root / str(pid)
        (directory / "fd").mkdir(parents=True)
        (directory / "fd/3").symlink_to("socket:[9910]")
        (directory / "comm").write_text("python\n")
        (directory / "status").write_text("State:\tT (stopped)\n")
    return root


def test_listener_owner_wins_over_a_decoy_process_name(tmp_path: Path) -> None:
    root = proc(tmp_path, [7])
    (root / "8/fd").mkdir(parents=True)
    (root / "8/comm").write_text("patroni\n")
    assert observation.listener_owner(9000, root) == 7


def test_suspend_requires_the_selected_listener_to_be_kernel_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = proc(tmp_path, [7])
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        observation.os, "kill", lambda pid, sig: sent.append((pid, sig))
    )
    result = observation.suspend_listener(9000, root)
    assert sent == [(7, observation.signal.SIGSTOP)]
    assert result["kernel_stop_confirmed"] is True
    assert result["legacy_name_selector_matched"] is False


@pytest.mark.parametrize("owners", [[], [1], [7, 8]])
def test_ambiguous_missing_or_init_listener_is_never_signalled(
    tmp_path: Path, owners: list[int]
) -> None:
    with pytest.raises(ValueError, match="PATRONI_LISTENER_OWNER_NOT_UNIQUE_CHILD"):
        observation.listener_owner(9000, proc(tmp_path, owners))


def test_unconfirmed_stop_does_not_count_as_injected_fault(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = proc(tmp_path, [7])
    (root / "7/status").write_text("State:\tS (sleeping)\n")
    clock = iter([0.0, 0.0, 3.0])
    monkeypatch.setattr(observation.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(observation.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(observation.time, "sleep", lambda duration: None)
    with pytest.raises(ValueError, match="PATRONI_LISTENER_STOP_UNCONFIRMED"):
        observation.suspend_listener(9000, root)
