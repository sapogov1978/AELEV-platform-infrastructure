"""Read-only HA observation and positively confirmed Patroni fault injection."""

from __future__ import annotations

from collections.abc import Callable
import itertools
import json
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any

import psycopg


def observe_roles(connect: Callable[[int], Any]) -> dict[int, bool]:
    """Keep connections open; a fenced process cannot reconnect as a successor."""
    connections: dict[int, Any] = {}
    roles: dict[int, bool] = {}

    def read(node: int) -> bool | None:
        try:
            row = connections[node].execute("SELECT pg_is_in_recovery()").fetchone()
            if row is None or type(row[0]) is not bool:
                raise ValueError("INVALID_SQL_ROLE")
            roles[node] = row[0]
            return bool(row[0])
        except (psycopg.Error, OSError, ValueError):
            roles.pop(node, None)
            return None

    try:
        for node in range(1, 4):
            try:
                connections[node] = connect(node)
                connections[node].autocommit = True
            except (psycopg.Error, OSError):
                continue
            read(node)
        candidates = [node for node, recovery in roles.items() if not recovery]
        for first, second in itertools.combinations(candidates, 2):
            # A primary cannot demote and return on the same live connection.
            # The first process must remain writable across the second sample.
            if read(first) is False and read(second) is False and read(first) is False:
                raise AssertionError("MULTIPLE_SQL_WRITERS_ON_LIVE_CONNECTIONS")
        return roles
    finally:
        for connection in connections.values():
            connection.close()


def listener_owner(port: int, proc: Path = Path("/proc")) -> int:
    inodes: set[str] = set()
    for name in ("tcp", "tcp6"):
        path = proc / "net" / name
        if not path.exists():
            continue
        for row in path.read_text().splitlines()[1:]:
            fields = row.split()
            if (
                len(fields) > 9
                and fields[3] == "0A"
                and int(fields[1].rsplit(":", 1)[1], 16) == port
            ):
                inodes.add(fields[9])
    owners: set[int] = set()
    sockets = {"socket:[" + inode + "]" for inode in inodes}
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            descriptors = list((process / "fd").iterdir())
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
            if target in sockets:
                owners.add(int(process.name))
    if len(owners) != 1 or 1 in owners:
        raise ValueError("PATRONI_LISTENER_OWNER_NOT_UNIQUE_CHILD")
    return owners.pop()


def suspend_listener(port: int, proc: Path = Path("/proc")) -> dict[str, Any]:
    pid = listener_owner(port, proc)
    legacy = []
    for process in proc.iterdir():
        if process.name.isdigit() and process.name != "1":
            try:
                if (process / "comm").read_text().strip() == "patroni":
                    legacy.append(int(process.name))
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
    os.kill(pid, signal.SIGSTOP)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = next(
            line.split()[1]
            for line in (proc / str(pid) / "status").read_text().splitlines()
            if line.startswith("State:")
        )
        if state == "T":
            return {
                "target_pid": pid,
                "selection": "exact REST listener owner",
                "kernel_stop_confirmed": True,
                "legacy_name_selector_matched": pid in legacy,
                "legacy_name_match_count": len(legacy),
            }
        time.sleep(0.01)
    raise ValueError("PATRONI_LISTENER_STOP_UNCONFIRMED")


def hold_listener(port: int, marker: Path, proc: Path = Path("/proc")) -> None:
    """Keep the fault injector present until fencing, never issue SIGCONT."""
    os.umask(0o077)
    result = suspend_listener(port, proc)
    result.update(
        phase="HELD",
        injector_pid=os.getpid(),
        injector_group=os.getpgrp(),
        target_group=os.getpgid(result["target_pid"]),
    )

    def save() -> None:
        pending = marker.with_suffix(".pending")
        pending.write_text(json.dumps(result))
        os.replace(pending, marker)

    save()
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            state = next(
                line.split()[1]
                for line in (proc / str(result["target_pid"]) / "status")
                .read_text()
                .splitlines()
                if line.startswith("State:")
            )
        except FileNotFoundError:
            return
        if state != "T":
            result.update(phase="FAULT_LOST", observed_state=state)
            save()
            raise ValueError("PATRONI_FAULT_DID_NOT_PERSIST")
        time.sleep(0.1)
    result["phase"] = "HOLD_TIMEOUT"
    save()
    raise TimeoutError("PATRONI_FAULT_HOLD_TIMEOUT")


def main() -> None:
    if sys.argv[1] == "suspend":
        result = suspend_listener(int(sys.argv[2]))
    elif sys.argv[1] == "hold":
        hold_listener(int(sys.argv[2]), Path(sys.argv[3]))
        return
    elif sys.argv[1] == "observe":
        from aelev_postgresql.__main__ import SECRETS, secret, setting

        def connect(node: int) -> psycopg.Connection[Any]:
            return psycopg.connect(
                host=setting(f"NODE_{node}_HOST"),
                port=setting(f"NODE_{node}_PORT"),
                user=setting("ADMIN_USER"),
                password=secret("admin-password"),
                dbname="postgres",
                sslmode="verify-full",
                sslrootcert=str(SECRETS / "ca.crt"),
                sslcert=str(SECRETS / "admin.crt"),
                sslkey=str(SECRETS / "admin.key"),
                connect_timeout=2,
                options="-c statement_timeout=2000",
            )

        result = {"roles": observe_roles(connect)}
    else:
        raise ValueError("UNKNOWN_HA_OBSERVER_ACTION")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
