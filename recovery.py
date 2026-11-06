"""Explicitly enrolled, finite Docker recovery; never a discovery service."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import json
import os
import platform
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, IO

DELAYS = (30, 120, 300)
LIMIT = len(DELAYS)
AUTO_COOLDOWN = 900
AUTO_HEALTHY = 1800
RESUME_GAP = 120
RESUME_GRACE = 120
DEPENDENCY_RETRY = 120
HANDOFF_STABLE = 30
NO_RESTART = {"Name": "no", "MaximumRetryCount": 0}
CONTROL_EVENTS = {
    "die",
    "stop",
    "kill",
    "pause",
    "destroy",
    "rename",
    "update",
    "restart",
}
EVENT_FORMAT = (
    "{{.TimeNano}}|{{.Type}}|{{if or "
    + " ".join('(eq .Action "' + action + '")' for action in sorted(CONTROL_EVENTS))
    + '}}{{.Action}}{{else}}other{{end}}|{{.Actor.ID}}|{{if index .Actor.Attributes "exitCode"}}{{index .Actor.Attributes "exitCode"}}{{else}}-{{end}}'
)


def event_frame(raw: bytes) -> dict[str, Any]:
    """The CLI emits no labels, names, arguments, credentials or event bodies."""
    parts = raw.decode("ascii").strip().split("|")
    if len(parts) != 5 or not parts[0].isdigit():
        raise ValueError("EVENT_FRAME_INVALID")
    stamp = int(parts[0])
    if stamp <= 0 or len(parts[0]) > 20:
        raise ValueError("EVENT_FRAME_INVALID")
    code = parts[4]
    if code != "-" and not code.isdigit():
        raise ValueError("EVENT_FRAME_INVALID")
    return {
        "stamp": stamp,
        "type": parts[1],
        "action": parts[2],
        "id": parts[3],
        "exit_code": code,
    }


def event_scope(state: dict[str, Any]) -> set[str]:
    return {target["expected"]["id"] for target in state["targets"].values()}


def event_evidence(root: Path, daemon: str) -> dict[str, Any]:
    path = root / "docker-events.json"
    if not path.exists():
        return {"schema": 1, "daemon": daemon, "epochs": [], "events": []}
    if path.is_symlink() or path.stat().st_size > 16 * 1024**2:
        raise ValueError("EVENT_JOURNAL_INVALID")
    value: dict[str, Any] = json.loads(path.read_bytes())
    if value.get("schema") != 1 or value.get("daemon") != daemon:
        raise ValueError("EVENT_JOURNAL_SCOPE_CHANGED")
    return value


def event_record(
    value: dict[str, Any], frame: dict[str, Any], scope: set[str], *, first: bool
) -> None:
    """Reconnect only bridges epochs when retained history proves an overlap."""
    epochs = value["epochs"]
    stamp = frame["stamp"]
    if first and (not epochs or stamp > epochs[-1]["end"]):
        epochs.append({"start": stamp, "end": stamp})
    if not epochs:
        raise ValueError("EVENT_EPOCH_MISSING")
    epochs[-1]["end"] = max(stamp, epochs[-1]["end"])
    if (
        frame["type"] == "container"
        and frame["action"] in CONTROL_EVENTS
        and frame["id"] in scope
    ):
        record = {key: frame[key] for key in ("stamp", "action", "id", "exit_code")}
        if record not in value["events"]:
            value["events"].append(record)
    if len(value["events"]) > 50000 or len(epochs) > 10000:
        raise ValueError("EVENT_JOURNAL_CAPACITY")


def collect_events(
    root: Path, stop: threading.Event, processes: list[subprocess.Popen[bytes]]
) -> None:
    """Read-only observer inside the existing controller, never a second actuator."""
    with (root / ".events.lock").open("a") as barrier:
        lock(barrier)
        while not stop.is_set():
            process: subprocess.Popen[bytes] | None = None
            try:
                state = json.loads((root / "state.json").read_bytes())
                if (
                    docker(state, "info", "--format", "{{.ID}}").decode().strip()
                    != state["daemon"]
                ):
                    raise ValueError("DAEMON_CHANGED")
                value = event_evidence(root, state["daemon"])
                process = subprocess.Popen(
                    [
                        state["docker"],
                        "--host",
                        state["endpoint"],
                        "events",
                        "--since",
                        "0",
                        "--format",
                        EVENT_FORMAT,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                processes.append(process)
                assert process.stdout is not None
                first, saved = True, 0.0
                while not stop.is_set():
                    raw = process.stdout.readline(513)
                    if not raw or len(raw) > 512:
                        raise ValueError("EVENT_STREAM_INTERRUPTED")
                    frame = event_frame(raw)
                    # Re-read only for control events: replacements/enrollment are
                    # atomic owner writes, not dynamic discovery from Docker.
                    if frame["action"] in CONTROL_EVENTS:
                        state = json.loads((root / "state.json").read_bytes())
                    event_record(value, frame, event_scope(state), first=first)
                    first = False
                    if (
                        frame["action"] in CONTROL_EVENTS
                        or time.monotonic() - saved >= 1
                    ):
                        value.update(checked_at=time.time(), code="EVENT_STREAM_ACTIVE")
                        atomic(
                            root / "docker-events.json",
                            json.dumps(value, sort_keys=True).encode(),
                        )
                        saved = time.monotonic()
            except (
                OSError,
                ValueError,
                RuntimeError,
                subprocess.TimeoutExpired,
            ) as error:
                reason = (
                    str(error)
                    if isinstance(error, ValueError)
                    and str(error)
                    in {
                        "DAEMON_CHANGED",
                        "EVENT_FRAME_INVALID",
                        "EVENT_STREAM_INTERRUPTED",
                        "EVENT_JOURNAL_INVALID",
                        "EVENT_JOURNAL_SCOPE_CHANGED",
                        "EVENT_JOURNAL_CAPACITY",
                    }
                    else "EVENT_STREAM_UNAVAILABLE"
                )
                atomic(
                    root / "event-error.json",
                    json.dumps({"code": reason, "at": time.time()}).encode(),
                )
            finally:
                if process is not None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                    if process.stdout is not None:
                        process.stdout.close()
                    processes.remove(process)
            stop.wait(1)


def durable_events(
    root: Path, state: dict[str, Any], since: float, boundary: float
) -> list[dict[str, Any]] | None:
    """Join durable coverage to a fresh bounded tail; gaps never authorize start."""
    value = event_evidence(root, state["daemon"])
    tail = docker(
        state,
        "events",
        "--since",
        "0",
        "--until",
        f"{boundary:.9f}",
        "--format",
        EVENT_FORMAT,
    )
    if len(tail) > 256 * 512:
        raise ValueError("EVENT_TAIL_INVALID")
    frames = [event_frame(line) for line in tail.splitlines()]
    if not frames or len(frames) > 256:
        return None
    lower, upper = int(since * 1e9), int(boundary * 1e9)
    earliest = min(frame["stamp"] for frame in frames)
    if earliest <= lower:
        records = []  # Fresh Docker history alone already covers the interval.
    else:
        epochs = value["epochs"]
        if not epochs:
            return None
        covering = next(
            (epoch for epoch in epochs if epoch["start"] <= lower <= epoch["end"]), None
        )
        if covering is None:
            if lower < epochs[-1]["start"]:
                raise ValueError("EVENT_COVERAGE_GAP")
            return None
        if covering["end"] < earliest:
            if covering is not epochs[-1]:
                raise ValueError("EVENT_COVERAGE_GAP")
            return None  # Stream reader is behind; wait, never guess or restart.
        records = value["events"]
    return [record for record in records + frames if lower <= record["stamp"] <= upper]


def watch(root: Path) -> None:
    """One scheduler process: continuous evidence plus the original 15s actuator."""
    stop = threading.Event()
    state = json.loads((root / "state.json").read_bytes())
    atomic(
        root / "event-observation.json",
        json.dumps({"schema": 1, "daemon": state["daemon"]}).encode(),
    )

    def shutdown(_signum: int, _frame: Any) -> None:
        stop.set()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    processes: list[subprocess.Popen[bytes]] = []
    observer = threading.Thread(
        target=collect_events, args=(root, stop, processes), daemon=True
    )
    observer.start()
    try:
        while not stop.is_set():
            if not observer.is_alive():
                raise SystemExit(1)  # The same scheduler restarts its controller.
            try:
                pending = any(
                    (root / name).exists()
                    and json.loads((root / name).read_bytes()).get("phase")
                    != "COMPLETE"
                    for name in ("scheduler.json", "container-maintenance.json")
                )
                if not pending:
                    with (root / ".lock").open("a") as barrier:
                        lock(barrier)
                        once(root)
            except BlockingIOError:
                pass
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                atomic(
                    root / "controller-error.json",
                    json.dumps(
                        {"code": "CONTROLLER_FAILED", "at": time.time()}
                    ).encode(),
                )
            stop.wait(15)
    finally:
        stop.set()
        for process in list(processes):
            process.terminate()
        observer.join(timeout=5)


def lock(stream: IO[Any], *, timeout: float = 0) -> None:
    """Acquire the process lock, optionally waiting for the active bounded tick."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                msvcrt: Any = importlib.import_module("msvcrt")
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            busy = isinstance(error, BlockingIOError) or error.errno in (
                11,
                13,
                35,
                36,
            )
            if not busy or time.monotonic() >= deadline:
                if busy:
                    raise BlockingIOError("RECOVERY_LOCK_BUSY") from None
                raise
            time.sleep(0.05)


def powershell(script: str) -> bytes:
    """Fixed provider script, no profile, shell interpolation or credentials."""
    script = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); " + script
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    return execute(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    )


def ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def private(root: Path, *, create: bool = False) -> None:
    """Use the current user's protected ACL on NTFS, not meaningless POSIX bits."""
    if any(p.is_symlink() or p.is_junction() for p in (root, *root.parents)):
        raise ValueError("PRIVATE_OWNED_STATE_REQUIRED")
    if os.name != "nt":
        if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise ValueError("PRIVATE_OWNED_STATE_REQUIRED")
        return
    setup = ""
    if create:
        setup = """
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
$rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $path -AclObject $acl -ErrorAction Stop
$acl = Get-Acl -LiteralPath $path -ErrorAction Stop
"""
    value = powershell(
        "$ErrorActionPreference='Stop'; $path=" + ps_literal(str(root)) + "\n"
        "$drive=[IO.DriveInfo]::new([IO.Path]::GetPathRoot($path))\n"
        "if ($drive.DriveType -ne 'Fixed' -or $drive.DriveFormat -ne 'NTFS') { throw 'LOCAL_NTFS_REQUIRED' }\n"
        "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User\n"
        "$acl=Get-Acl -LiteralPath $path -ErrorAction Stop\n" + setup + """
if (!$acl.AreAccessRulesProtected -or $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { throw 'ACL' }
$rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
if ($rules.Count -ne 1 -or $rules[0].IdentityReference.Value -ne $sid.Value -or $rules[0].AccessControlType -ne 'Allow' -or $rules[0].FileSystemRights -ne 'FullControl') { throw 'ACL' }
[Console]::Write('PRIVATE')
"""
    )
    if value != b"PRIVATE":
        raise ValueError("PRIVATE_OWNED_STATE_REQUIRED")


def local_endpoint(endpoint: str) -> bool:
    return endpoint.startswith("unix:///") or (
        os.name == "nt"
        and endpoint
        in {
            "npipe:////./pipe/docker_engine",
            "npipe:////./pipe/dockerDesktopLinuxEngine",
        }
    )


def execute(command: list[str], *, timeout: float = 20) -> bytes:
    """Never surface subprocess output that may contain configuration."""
    result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("OPERATOR_COMMAND_FAILED")
    return result.stdout


def atomic(path: Path, payload: bytes) -> None:
    """Commit intent before a runtime action, including the directory entry."""
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        if os.name != "nt":
            os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name == "nt":
        ctypes: Any = importlib.import_module("ctypes")

        # WRITE_THROUGH is the native durable rename barrier on Windows;
        # opening/fsync'ing a directory is not supported there.
        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
        move.restype = ctypes.c_int
        if not move(str(temporary), str(path), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def commit(root: Path, state: dict[str, Any]) -> None:
    atomic(root / "state.json", json.dumps(state, sort_keys=True).encode())
    report = {
        "checked_at": state.get("checked_at"),
        "code": state.get("code", "ENROLLED"),
        "targets": {
            name: {
                key: target.get(key)
                for key in (
                    "attempts",
                    "code",
                    "next_due",
                    "last_action_at",
                    "last_failure_at",
                    "last_failure_code",
                    "last_failure_diagnostic",
                    "last_action_diagnostic",
                    "blocked",
                    "automatic",
                )
            }
            for name, target in state["targets"].items()
        },
    }
    if (root / "event-observation.json").exists():
        try:
            evidence = event_evidence(root, state["daemon"])
            report["event_observation"] = {
                "code": evidence.get("code", "EVENT_STREAM_STARTING"),
                "checked_at": evidence.get("checked_at"),
                "epochs": len(evidence["epochs"]),
                "saved_control_events": len(evidence["events"]),
            }
        except (OSError, ValueError):
            report["event_observation"] = {"code": "EVENT_JOURNAL_UNAVAILABLE"}
    atomic(root / "report.json", json.dumps(report, sort_keys=True).encode())


def docker(state: dict[str, Any], *args: str) -> bytes:
    return execute([state["docker"], "--host", state["endpoint"], *args])


def inspect(state: dict[str, Any], identity: str) -> dict[str, Any]:
    result = json.loads(docker(state, "inspect", identity))
    if not isinstance(result, list) or len(result) != 1:
        raise ValueError("TARGET_AMBIGUOUS")
    row: dict[str, Any] = result[0]
    return row


def fingerprint(row: dict[str, Any]) -> dict[str, Any]:
    labels = row["Config"].get("Labels") or {}
    project, service = (
        labels.get("com.docker.compose." + key) for key in ("project", "service")
    )
    if (
        not project
        or not service
        or labels.get("com.docker.compose.oneoff", "false").lower() != "false"
    ):
        raise ValueError("PERMANENT_COMPOSE_TARGET_REQUIRED")
    return {
        "id": row["Id"],
        "image": row["Image"],
        "project": project,
        "service": service,
        "config_hash": labels.get("com.docker.compose.config-hash"),
    }


def compose_dependency_spec(row: dict[str, Any]) -> list[tuple[str, str]]:
    """Read only the exact Compose graph already attached to this container."""
    labels = row.get("Config", {}).get("Labels") or {}
    raw = labels.get("com.docker.compose.depends_on", "")
    if not isinstance(raw, str) or len(raw) > 8192:
        raise ValueError("COMPOSE_DEPENDENCY_GRAPH_INVALID")
    result: list[tuple[str, str]] = []
    for item in filter(None, raw.split(",")):
        parts = item.split(":")
        if (
            len(parts) not in {2, 3}
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", parts[0])
            is None
            or parts[1]
            not in {
                "service_started",
                "service_healthy",
                "service_completed_successfully",
            }
            or (len(parts) == 3 and parts[2] not in {"true", "false"})
        ):
            raise ValueError("COMPOSE_DEPENDENCY_GRAPH_INVALID")
        result.append((parts[0], parts[1]))
    if len(result) > 64 or len({name for name, _ in result}) != len(result):
        raise ValueError("COMPOSE_DEPENDENCY_GRAPH_INVALID")
    return result


def compose_service(
    state: dict[str, Any], project: str, service: str
) -> dict[str, Any] | None:
    """Resolve one owner-declared Compose service without global discovery."""
    identifiers = (
        docker(
            state,
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            "label=com.docker.compose.project=" + project,
            "--filter",
            "label=com.docker.compose.service=" + service,
            "--format",
            "{{.ID}}",
        )
        .decode()
        .splitlines()
    )
    if not identifiers:
        return None
    if len(identifiers) != 1:
        raise ValueError("COMPOSE_DEPENDENCY_AMBIGUOUS")
    row = inspect(state, identifiers[0])
    current = fingerprint(row)
    if current["project"] != project or current["service"] != service:
        raise ValueError("COMPOSE_DEPENDENCY_SCOPE_CHANGED")
    return row


def dependencies_ready(state: dict[str, Any], row: dict[str, Any]) -> bool:
    """Follow the bounded Compose graph and wait without spending an attempt."""
    project = fingerprint(row)["project"]
    visited: set[tuple[str, str]] = set()

    def ready(current: dict[str, Any]) -> bool:
        service = fingerprint(current)["service"]
        key = (project, service)
        if key in visited:
            raise ValueError("COMPOSE_DEPENDENCY_CYCLE")
        visited.add(key)
        if len(visited) > 64:
            raise ValueError("COMPOSE_DEPENDENCY_GRAPH_INVALID")
        try:
            for dependency, condition in compose_dependency_spec(current):
                child = compose_service(state, project, dependency)
                if child is None or not ready(child):
                    return False
                process = child["State"]
                if condition == "service_started" and not process.get("Running"):
                    return False
                if condition == "service_healthy" and (
                    not process.get("Running")
                    or process.get("Health", {}).get("Status") != "healthy"
                    or bad_probe(child, time.time()) is not None
                ):
                    return False
                if condition == "service_completed_successfully" and not (
                    process.get("Status") == "exited"
                    and process.get("ExitCode") == 0
                ):
                    return False
        finally:
            visited.remove(key)
        return True

    return ready(row)


def enroll(root: Path, name: str, guard: Path | None) -> None:
    """Enroll only an explicit live incarnation; do not adopt replacements."""
    state_path = root / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
    else:
        binary = shutil.which("docker")
        if binary is None:
            raise ValueError("DOCKER_CLI_REQUIRED")
        endpoint = (
            execute(
                [binary, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
            )
            .decode()
            .strip()
        )
        if not local_endpoint(endpoint):
            raise ValueError("EXPLICIT_LOCAL_DAEMON_REQUIRED")
        state = {
            "schema_version": 1,
            "docker": binary,
            "endpoint": endpoint,
            "targets": {},
        }
        state["daemon"] = docker(state, "info", "--format", "{{.ID}}").decode().strip()
    if name in state["targets"]:
        raise ValueError("ALREADY_ENROLLED_USE_EXPLICIT_RESET")
    row = inspect(state, name)
    if not row["State"]["Running"] or "Health" not in row["State"]:
        raise ValueError("LIVE_HEALTHCHECK_TARGET_REQUIRED")
    native_count = row["RestartCount"]
    if type(native_count) is not int or native_count < 0:
        raise ValueError("NATIVE_HISTORY_INVALID")
    remaining = max(0, LIMIT - native_count)
    target = {
        "expected": fingerprint(row),
        "attempts": native_count,
        "native_count": native_count,
        "policy_limit": remaining,
        "code": "CIRCUIT_OPEN" if not remaining else "ENROLLED",
        "blocked": not remaining,
        "next_due": 0,
        "bad_since": None,
        "original_restart": row["HostConfig"]["RestartPolicy"],
        "guard": None,
    }
    if guard is not None:
        guard = guard.resolve(strict=True)
        target["guard"] = {
            "path": str(guard),
            "sha256": hashlib.sha256(guard.read_bytes()).hexdigest(),
        }
    state["targets"][name] = target
    commit(root, state)
    # Native crashes are finite too. Explicit Docker stop is never undone here.
    policy = f"on-failure:{remaining}" if remaining else "no"
    docker(state, "update", "--restart", policy, row["Id"])


def registration_check(
    state: dict[str, Any], name: str, expected: dict[str, str], row: dict[str, Any]
) -> None:
    """Validate an explicitly opted-in exact target, never discover or replace one."""
    if (
        fingerprint(row) != expected
        or row.get("Name") != "/" + name
        or row.get("Config", {}).get("Labels", {}).get("io.aelev.recovery.registration")
        != "unguarded-automatic-v1"
        or not isinstance(
            row.get("Config", {}).get("Healthcheck", {}).get("Test"), list
        )
        or not row["Config"]["Healthcheck"]["Test"]
        or row["Config"]["Healthcheck"]["Test"][0] not in {"CMD", "CMD-SHELL"}
        or type(row.get("RestartCount")) is not int
        or row["RestartCount"] < 0
    ):
        raise ValueError("RECOVERY_REGISTRATION_TARGET_INVALID")
    for other_name, target in state["targets"].items():
        previous = target.get("expected", {})
        if other_name != name and (
            previous.get("id") == expected["id"]
            or all(previous.get(k) == expected[k] for k in ("project", "service"))
        ):
            raise ValueError("RECOVERY_REGISTRATION_USE_REPLACEMENT")
    target = state["targets"].get(name)
    if target is not None:
        registration = target.get("registration", {})
        if (
            target["expected"] != expected
            or registration.get("version") != 1
            or registration.get("phase") not in {"PREPARED", "COMPLETE"}
        ):
            raise ValueError("RECOVERY_REGISTRATION_EXISTING_TARGET_PRESERVED")
        if registration["phase"] == "COMPLETE":
            if row["HostConfig"]["RestartPolicy"] != NO_RESTART:
                raise ValueError("RECOVERY_REGISTRATION_POLICY_CHANGED")
            return
    health = row["State"]
    policy = row["HostConfig"]["RestartPolicy"]
    if (
        health.get("Running") is not True
        or health.get("Paused")
        or health.get("Restarting")
        or health.get("OOMKilled")
        or health.get("Health", {}).get("Status") != "healthy"
        or not health.get("StartedAt")
        or not (
            policy == NO_RESTART
            or policy.get("Name") == "on-failure"
            and type(policy.get("MaximumRetryCount")) is int
            and 1 <= policy["MaximumRetryCount"] <= LIMIT
        )
    ):
        raise ValueError("RECOVERY_REGISTRATION_LIVE_TARGET_REQUIRED")


def register_exact(root: Path, name: str, expected: dict[str, str]) -> None:
    """Join the existing automatic controller without resetting or starting a target."""
    state = json.loads((root / "state.json").read_bytes())
    if docker(state, "info", "--format", "{{.ID}}").decode().strip() != state["daemon"]:
        raise ValueError("RECOVERY_DAEMON_MISMATCH")
    row = inspect(state, expected["id"])
    registration_check(state, name, expected, row)
    target = state["targets"].get(name)
    if target is not None and target["registration"]["phase"] == "COMPLETE":
        return  # In particular, never cancel a later suspend or manual stop.
    if target is None:
        count = row["RestartCount"]
        policy = row["HostConfig"]["RestartPolicy"]
        state["targets"][name] = {
            "expected": expected,
            "attempts": count,
            "native_count": count,
            "policy_limit": policy["MaximumRetryCount"],
            "code": "REGISTRATION_PENDING",
            "blocked": False,
            "next_due": 0,
            "bad_since": None,
            "original_restart": policy,
            "guard": None,
            "registration": {"version": 1, "phase": "PREPARED"},
        }
        state.setdefault("events", []).append(
            {"at": time.time(), "target": name, "code": "REGISTRATION_INTENT"}
        )
        commit(root, state)
    automatic_enable(root, name)
    state = json.loads((root / "state.json").read_bytes())
    state["targets"][name]["registration"]["phase"] = "COMPLETE"
    state.setdefault("events", []).append(
        {"at": time.time(), "target": name, "code": "REGISTRATION_COMPLETE"}
    )
    commit(root, state)


def adopt_stopped(
    root: Path, name: str, expected_id: str, native_count: int, image: str
) -> None:
    """Record an exhausted stopped predecessor without starting or enabling it."""
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) is None
        or re.fullmatch(r"[a-f0-9]{64}", expected_id) is None
        or re.fullmatch(r"sha256:[a-f0-9]{64}", image) is None
        or type(native_count) is not int
        or native_count < LIMIT
    ):
        raise ValueError("EXHAUSTED_PREDECESSOR_PROOF_REQUIRED")
    state: dict[str, Any] = json.loads((root / "state.json").read_text())
    if docker(state, "info", "--format", "{{.ID}}").decode().strip() != state["daemon"]:
        raise ValueError("DAEMON_CHANGED")

    def proof(row: dict[str, Any]) -> dict[str, Any]:
        current = fingerprint(row)
        process = row["State"]
        if (
            row.get("Name") != "/" + name
            or current["id"] != expected_id
            or current["image"] != image
            or not current["config_hash"]
            or type(row["RestartCount"]) is not int
            or row["RestartCount"] != native_count
            or process.get("Status") != "exited"
            or process.get("Running") is not False
            or process.get("Paused") is not False
            or process.get("Restarting") is not False
            or process.get("Dead") is not False
            or row["HostConfig"]["RestartPolicy"]
            != {"Name": "no", "MaximumRetryCount": 0}
        ):
            raise ValueError("STOPPED_PREDECESSOR_UNCONFIRMED")
        return {
            "expected": current,
            "native_count": native_count,
            "finished_at": process.get("FinishedAt"),
            "exit_code": process.get("ExitCode"),
        }

    observed = proof(inspect(state, name))
    if proof(inspect(state, expected_id)) != observed:
        raise ValueError("STOPPED_PREDECESSOR_CHANGED")
    existing = state["targets"].get(name)
    if existing is not None:
        if (
            existing.get("stopped_adoption") == observed
            and existing["expected"] == observed["expected"]
            and existing["attempts"] == native_count
            and existing["native_count"] == native_count
            and existing["blocked"] is True
            and existing["code"] == "CIRCUIT_OPEN"
            and existing["policy_limit"] == 0
            and "restart_intent" not in existing
        ):
            return  # The exact durable receipt survived a lost acknowledgement.
        raise ValueError("PREDECESSOR_ALREADY_REGISTERED")
    state["targets"][name] = {
        "expected": observed["expected"],
        "attempts": native_count,
        "native_count": native_count,
        "policy_limit": 0,
        "code": "CIRCUIT_OPEN",
        "blocked": True,
        "next_due": 0,
        "bad_since": None,
        "original_restart": {"Name": "no", "MaximumRetryCount": 0},
        "guard": None,
        "stopped_adoption": observed,
    }
    state.setdefault("events", []).append(
        {
            "at": time.time(),
            "target": name,
            "code": "OPERATOR_ADOPTED_EXHAUSTED_PREDECESSOR",
            "proof": observed,
        }
    )
    commit(root, state)


def import_history(root: Path, path: Path, proof: str) -> None:
    """Import an operator-reviewed owner receipt once; never replenish a budget."""
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("HISTORY_RECEIPT_INVALID")
    raw = path.read_bytes()
    if (
        not re.fullmatch(r"[a-f0-9]{64}", proof)
        or hashlib.sha256(raw).hexdigest() != proof
    ):
        raise ValueError("HISTORY_RECEIPT_CHANGED")
    value = json.loads(raw)
    fields = {
        "schema_version",
        "daemon_id",
        "name",
        "expected",
        "prior_attempts",
        "evidence_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["name"], str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,253}", value["name"]) is None
        or type(value["prior_attempts"]) is not int
        or value["prior_attempts"] < 0
        or not isinstance(value["evidence_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", value["evidence_sha256"]) is None
        or not isinstance(value["expected"], dict)
        or set(value["expected"])
        != {"id", "image", "project", "service", "config_hash"}
    ):
        raise ValueError("HISTORY_RECEIPT_INVALID")
    state = json.loads((root / "state.json").read_bytes())
    if (
        value["daemon_id"] != state["daemon"]
        or docker(state, "info", "--format", "{{.ID}}").decode().strip()
        != state["daemon"]
    ):
        raise ValueError("DAEMON_CHANGED")
    name, expected = value["name"], value["expected"]
    target = state["targets"].get(name)
    if target is not None:
        if (
            target.get("history_import", {}).get("sha256") == proof
            and target["expected"] == expected
        ):
            return  # A repeat never releases a later hold or starts a process.
        raise ValueError("HISTORY_EXISTING_REGISTRATION_PRESERVED")
    if any(
        other["expected"]["id"] == expected["id"]
        or all(other["expected"][k] == expected[k] for k in ("project", "service"))
        for other in state["targets"].values()
    ):
        raise ValueError("HISTORY_DUPLICATE_REGISTRATION")

    def checked() -> dict[str, Any]:
        row = inspect(state, name)
        health, policy = row["State"], row["HostConfig"]["RestartPolicy"]
        if (
            fingerprint(row) != expected
            or row.get("Name") != "/" + name
            or not expected.get("config_hash")
            or type(row["RestartCount"]) is not int
            or row["RestartCount"] < 0
            or health.get("Paused")
            or health.get("Restarting")
            or health.get("Dead")
            or row.get("Config", {}).get("Healthcheck", {}).get("Test", [None])[0]
            not in {"CMD", "CMD-SHELL"}
            or not (
                policy == NO_RESTART
                or policy.get("Name") == "on-failure"
                and type(policy.get("MaximumRetryCount")) is int
                and 1 <= policy["MaximumRetryCount"] <= LIMIT
            )
            or not (
                health.get("Running") is True
                and health.get("Health", {}).get("Status") == "healthy"
                and bad_probe(row, time.time()) is None
                or health.get("Running") is False
                and health.get("Status") == "exited"
                and health.get("ExitCode", 0) != 0
                and policy == NO_RESTART
            )
        ):
            raise ValueError("HISTORY_EXACT_TARGET_UNCONFIRMED")
        return row

    row = checked()
    again = checked()
    if (
        any(again[k] != row[k] for k in ("Id", "Image", "RestartCount", "HostConfig"))
        or started_at(again) != started_at(row)
        or again["State"]["Running"] != row["State"]["Running"]
    ):
        raise ValueError("HISTORY_TARGET_CHANGED")
    attempts = value["prior_attempts"] + row["RestartCount"]
    state["targets"][name] = {
        "expected": expected,
        "attempts": attempts,
        "native_count": row["RestartCount"],
        "policy_limit": row["HostConfig"]["RestartPolicy"]["MaximumRetryCount"],
        "code": "CIRCUIT_OPEN",
        "blocked": True,
        "next_due": 0,
        "bad_since": None,
        "guard": None,
        "original_restart": row["HostConfig"]["RestartPolicy"],
        "history_import": {"sha256": proof, "receipt": value},
    }
    state.setdefault("events", []).append(
        {
            "at": time.time(),
            "target": name,
            "code": "OPERATOR_HISTORY_IMPORTED",
            "sha256": proof,
            "attempts": attempts,
        }
    )
    commit(root, state)


def replacement_release_proof(target: dict[str, Any]) -> str:
    """Bind an operator review to one retained replacement block and budget."""
    value = {
        key: target.get(key)
        for key in (
            "expected",
            "attempts",
            "native_count",
            "guard",
            "replacement_from",
            "replacement_block",
        )
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def release_replacement(root: Path, name: str, identity: str, proof: str) -> None:
    """Release only a reviewed legacy TARGET_REPLACED, never start or reset."""
    if any(re.fullmatch(r"[a-f0-9]{64}", value) is None for value in (identity, proof)):
        raise ValueError("REPLACEMENT_RELEASE_PROOF_REQUIRED")
    state = json.loads((root / "state.json").read_bytes())
    if docker(state, "info", "--format", "{{.ID}}").decode().strip() != state["daemon"]:
        raise ValueError("DAEMON_CHANGED")
    target = state["targets"][name]
    if target["expected"]["id"] != identity:
        raise ValueError("REPLACEMENT_RELEASE_TARGET_CHANGED")
    previous = target.get("replacement_release")
    if previous is not None:
        if previous.get("proof") != proof:
            raise ValueError("REPLACEMENT_RELEASE_PROOF_CHANGED")
        return  # Do not clear any subsequent operator/protective hold.
    if (
        not target["blocked"]
        or target.get("replacement_block") != {"code": "TARGET_REPLACED"}
        or replacement_release_proof(target) != proof
        or "automatic" in target
        or "restart_intent" in target
        or target.get("replacement_policy_pending")
        or target.get("cancel_suspend", {}).get("phase", "COMPLETE") != "COMPLETE"
        or target.get("code") not in {"CIRCUIT_OPEN", "TARGET_REPLACED"}
        or not target.get("replacement_from", {}).get("id")
    ):
        raise ValueError("REPLACEMENT_RELEASE_UNCONFIRMED")

    def checked() -> dict[str, Any]:
        row = inspect(state, name)
        if (
            fingerprint(row) != target["expected"]
            or not row["State"]["Running"]
            or row["State"].get("Paused")
            or row["State"].get("Restarting")
            or row["State"].get("Health", {}).get("Status") != "healthy"
            or bad_probe(row, time.time()) is not None
            or row["RestartCount"] != target["native_count"]
            or row["HostConfig"]["RestartPolicy"] != NO_RESTART
        ):
            raise ValueError("REPLACEMENT_RELEASE_TARGET_NOT_HEALTHY")
        return row

    row = checked()
    if target["guard"] is not None:
        guard = target["guard"]
        path = Path(guard["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != guard["sha256"]:
            raise ValueError("GUARD_CHANGED")
        if (
            json.loads(execute([sys.executable, str(path), identity], timeout=50)).get(
                "allowed"
            )
            is not True
        ):
            raise ValueError("OWNER_GUARD_BLOCKED")
    if docker(
        state,
        "ps",
        "--all",
        "--no-trunc",
        "--filter",
        "id=" + target["replacement_from"]["id"],
        "--format",
        "{{.ID}}",
    ).strip():
        raise ValueError("REPLACEMENT_PREDECESSOR_NOT_RETIRED")
    if started_at(checked()) != started_at(row):
        raise ValueError("REPLACEMENT_RELEASE_TARGET_CHANGED")
    receipt = {
        "proof": proof,
        "at": time.time(),
        "previous_block": target.pop("replacement_block"),
        "attempts": target["attempts"],
    }
    target["replacement_release"] = receipt
    target.update(code="CIRCUIT_OPEN", blocked=True)
    state.setdefault("events", []).append(
        {
            "at": time.time(),
            "target": name,
            "code": "OPERATOR_REPLACEMENT_BLOCK_RELEASED",
            "receipt": receipt,
        }
    )
    commit(root, state)


def replace(
    root: Path,
    name: str,
    previous_id: str,
    previous_native_count: int,
    image: str,
    *,
    preserve_block: bool = False,
) -> None:
    """Explicit release handoff keeps the same lifetime budget, never resets it."""
    if (
        re.fullmatch(r"[a-f0-9]{64}", previous_id) is None
        or re.fullmatch(r"sha256:[a-f0-9]{64}", image) is None
        or type(previous_native_count) is not int
        or previous_native_count < 0
        or type(preserve_block) is not bool
    ):
        raise ValueError("EXACT_REPLACEMENT_PROOF_REQUIRED")
    state: dict[str, Any] = json.loads((root / "state.json").read_text())
    if docker(state, "info", "--format", "{{.ID}}").decode().strip() != state["daemon"]:
        raise ValueError("DAEMON_CHANGED")
    target = state["targets"][name]
    row = inspect(state, name)
    current = fingerprint(row)
    if (
        current["image"] != image
        or current["project"] != target["expected"]["project"]
        or current["service"] != target["expected"]["service"]
        or current["id"] == previous_id
        or not row["State"]["Running"]
        or row["State"].get("Health", {}).get("Status") != "healthy"
        or type(row["RestartCount"]) is not int
        or row["RestartCount"] < 0
    ):
        raise ValueError("REPLACEMENT_UNCONFIRMED")
    if preserve_block and (
        not target["blocked"]
        or row["HostConfig"]["RestartPolicy"] != {"Name": "no", "MaximumRetryCount": 0}
    ):
        raise ValueError("REPLACEMENT_BLOCK_NOT_PRESERVED")
    predecessor = target.get("replacement_from", {})
    if current == target["expected"]:
        if (target.get("replacement_block") is not None) != preserve_block:
            raise ValueError("REPLACEMENT_BLOCK_MODE_MISMATCH")
        if (
            predecessor.get("id") != previous_id
            or predecessor.get("native_count") != previous_native_count
        ):
            raise ValueError("REPLACEMENT_PREDECESSOR_MISMATCH")
        if not target.get("replacement_policy_pending"):
            return  # The exact completed handoff survived a lost acknowledgement.
        account_native(state, name, target, row, time.time())
        attempts = target["attempts"]
    else:
        if (
            target["expected"]["id"] != previous_id
            or previous_native_count < target["native_count"]
            or target["blocked"]
            and not preserve_block
            and target["code"]
            not in {"OPERATOR_SUSPENDED", "TARGET_REPLACED", "CIRCUIT_OPEN"}
        ):
            raise ValueError("REPLACEMENT_PREDECESSOR_MISMATCH")
        attempts = (
            target["attempts"]
            + previous_native_count
            - target["native_count"]
            + row["RestartCount"]
        )
        if "restart_intent" in target or "intent" in target.get("automatic", {}):
            # A replacement cannot inherit a command that may still restart its
            # predecessor. Only confirmed removal fences that old Docker ID.
            old = (
                docker(
                    state,
                    "ps",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    "id=" + previous_id,
                    "--format",
                    "{{.ID}}",
                )
                .decode()
                .splitlines()
            )
            if old:
                raise ValueError("REPLACEMENT_PENDING_RESTART_UNCONFIRMED")
            state.setdefault("events", []).append(
                {
                    "at": time.time(),
                    "target": name,
                    "code": "RELEASE_FENCED_RESTART_INTENT",
                    "intent": (
                        target.pop("restart_intent")
                        if "restart_intent" in target
                        else target["automatic"].pop("intent")
                    ),
                }
            )
        preserved_block = (
            target.get("replacement_block", {"code": target["code"]})
            if preserve_block
            else None
        )
        state.setdefault("events", []).append(
            {
                "at": time.time(),
                "target": name,
                "code": "RELEASE_REPLACEMENT_INTENT",
                "previous": target["expected"],
                "replacement": current,
                "attempts": attempts,
                "preserve_block": preserve_block,
                "previous_block_code": (
                    preserved_block["code"] if preserved_block is not None else None
                ),
            }
        )
        if preserve_block:
            target["replacement_block"] = preserved_block
        else:
            target.pop("replacement_block", None)
    remaining = max(0, LIMIT - attempts)
    automatic = target.get("automatic")
    native_limit = 0 if preserve_block or automatic is not None else remaining
    target.update(
        expected=current,
        attempts=attempts,
        native_count=row["RestartCount"],
        policy_limit=native_limit,
        blocked=True,
        code="CIRCUIT_OPEN" if not remaining else "REPLACEMENT_POLICY_PENDING",
        bad_since=None,
        replacement_from={"id": previous_id, "native_count": previous_native_count},
        replacement_policy_pending=True,
    )
    # Commit the remaining budget before changing native policy. A failed ACK
    # cannot authorize another replacement or a new three-attempt allowance.
    commit(root, state)
    policy = f"on-failure:{native_limit}" if native_limit else "no"
    docker(state, "update", "--restart", policy, row["Id"])
    checked = inspect(state, name)
    if fingerprint(checked) != current or checked["HostConfig"]["RestartPolicy"] != {
        "Name": "on-failure" if native_limit else "no",
        "MaximumRetryCount": native_limit,
    }:
        raise ValueError("REPLACEMENT_CHANGED_DURING_HANDOFF")
    target["code"] = (
        target["replacement_block"]["code"]
        if preserve_block
        else (
            "AUTOMATIC_REPLACED"
            if automatic is not None
            else "CIRCUIT_OPEN" if not remaining else "REPLACED_BUDGET_PRESERVED"
        )
    )
    target["replacement_policy_pending"] = False
    target["blocked"] = preserve_block or (not remaining and automatic is None)
    if automatic is not None:
        automatic.update(
            observed_at=time.time(),
            observed_started_at=started_at(checked),
            healthy_since=None,
            healthy_process=None,
        )
        automatic.pop("exit", None)
    commit(root, state)


def relocate(
    root: Path,
    name: str,
    previous_id: str,
    previous_native_count: int,
    replacement_name: str,
    replacement: dict[str, str],
    *,
    preserve_block: bool = False,
) -> None:
    """Move one journal identity across an explicit healthy Compose relocation."""
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,253}", name) is None
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,253}", replacement_name)
        is None
        or name == replacement_name
        or re.fullmatch(r"[a-f0-9]{64}", previous_id) is None
        or type(previous_native_count) is not int
        or previous_native_count < 0
        or type(preserve_block) is not bool
        or set(replacement) != {"id", "image", "project", "service", "config_hash"}
        or re.fullmatch(r"[a-f0-9]{64}", replacement.get("id", "")) is None
        or re.fullmatch(r"sha256:[a-f0-9]{64}", replacement.get("image", ""))
        is None
        or re.fullmatch(r"[a-f0-9]{64}", replacement.get("config_hash", ""))
        is None
        or any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", replacement.get(key, ""))
            is None
            for key in ("project", "service")
        )
    ):
        raise ValueError("EXACT_RELOCATION_PROOF_REQUIRED")
    state: dict[str, Any] = json.loads((root / "state.json").read_text())
    if docker(state, "info", "--format", "{{.ID}}").decode().strip() != state["daemon"]:
        raise ValueError("DAEMON_CHANGED")
    target = state["targets"].get(name)
    completed = state["targets"].get(replacement_name)
    if target is None and isinstance(completed, dict):
        receipt = completed.get("relocation", {})
        row = inspect(state, replacement_name)
        if (
            receipt.get("phase") == "COMPLETE"
            and receipt.get("previous_name") == name
            and receipt.get("previous", {}).get("id") == previous_id
            and receipt.get("replacement_name") == replacement_name
            and receipt.get("replacement") == replacement
            and receipt.get("previous_native_count") == previous_native_count
            and receipt.get("preserve_block") is preserve_block
            and completed.get("expected") == replacement
            and fingerprint(row) == replacement
            and row.get("Name") == "/" + replacement_name
        ):
            return
    if not isinstance(target, dict) or replacement_name in state["targets"]:
        raise ValueError("RELOCATION_TARGET_CONFLICT")
    row = inspect(state, replacement_name)
    current = fingerprint(row)
    pending = target.get("relocation")
    replay = bool(
        isinstance(pending, dict)
        and pending.get("phase") == "POLICY_PENDING"
        and pending.get("previous_name") == name
        and pending.get("previous", {}).get("id") == previous_id
        and pending.get("replacement_name") == replacement_name
        and pending.get("replacement") == replacement
        and pending.get("previous_native_count") == previous_native_count
        and pending.get("preserve_block") is preserve_block
        and target.get("expected") == replacement
    )
    old = (
        docker(
            state,
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            "id=" + previous_id,
            "--format",
            "{{.ID}}",
        )
        .decode()
        .splitlines()
    )
    if (
        (not replay and target["expected"]["id"] != previous_id)
        or previous_native_count < target["native_count"]
        or current != replacement
        or row.get("Name") != "/" + replacement_name
        or old
        or not row["State"].get("Running")
        or row["State"].get("Paused")
        or row["State"].get("Restarting")
        or row["State"].get("Health", {}).get("Status") != "healthy"
        or bad_probe(row, time.time()) is not None
        or type(row.get("RestartCount")) is not int
        or row["RestartCount"] < 0
        or (preserve_block and not target.get("blocked"))
        or (target.get("blocked") and not replay and not preserve_block)
        or target.get("replacement_policy_pending")
        or "restart_intent" in target
        or "intent" in target.get("automatic", {})
        or any(
            other_name != name
            and (
                other.get("expected", {}).get("id") == current["id"]
                or all(
                    other.get("expected", {}).get(key) == current[key]
                    for key in ("project", "service")
                )
            )
            for other_name, other in state["targets"].items()
        )
    ):
        raise ValueError("RELOCATION_UNCONFIRMED")
    attempts = (
        pending["attempts"]
        if replay
        else target["attempts"]
        + previous_native_count
        - target["native_count"]
        + row["RestartCount"]
    )
    automatic = target.get("automatic")
    remaining = max(0, LIMIT - attempts)
    native_limit = 0 if automatic is not None or preserve_block else remaining
    if not replay:
        target["relocation"] = {
            "phase": "POLICY_PENDING",
            "previous_name": name,
            "previous": target["expected"],
            "replacement_name": replacement_name,
            "replacement": current,
            "previous_native_count": previous_native_count,
            "attempts": attempts,
            "preserve_block": preserve_block,
            "preserved_block": (
                {"code": target["code"]} if preserve_block else None
            ),
        }
        target.update(
            expected=current,
            attempts=attempts,
            native_count=row["RestartCount"],
            policy_limit=native_limit,
            blocked=True,
            code="RELOCATION_POLICY_PENDING",
            bad_since=None,
        )
        state.setdefault("events", []).append(
            {
                "at": time.time(),
                "target": name,
                "code": "RELOCATION_INTENT",
                "previous_name": name,
                "replacement_name": replacement_name,
                "previous": target["relocation"]["previous"],
                "replacement": current,
                "attempts": attempts,
            }
        )
        commit(root, state)
    policy = f"on-failure:{native_limit}" if native_limit else "no"
    docker(state, "update", "--restart", policy, row["Id"])
    checked = inspect(state, replacement_name)
    desired = {
        "Name": "on-failure" if native_limit else "no",
        "MaximumRetryCount": native_limit,
    }
    if (
        fingerprint(checked) != current
        or checked.get("Name") != "/" + replacement_name
        or checked["HostConfig"].get("RestartPolicy") != desired
    ):
        raise ValueError("RELOCATION_CHANGED_DURING_HANDOFF")
    target["relocation"]["phase"] = "COMPLETE"
    target.update(
        blocked=preserve_block or (not remaining and automatic is None),
        code=(
            target["relocation"]["preserved_block"]["code"]
            if preserve_block
            else "AUTOMATIC_RELOCATED"
            if automatic is not None
            else "CIRCUIT_OPEN" if not remaining else "RELOCATED_BUDGET_PRESERVED"
        ),
    )
    if automatic is not None:
        automatic.update(
            observed_at=time.time(),
            observed_started_at=started_at(checked),
            healthy_since=None,
            healthy_process=None,
        )
        automatic.pop("exit", None)
    state["targets"][replacement_name] = state["targets"].pop(name)
    state.setdefault("events", []).append(
        {
            "at": time.time(),
            "target": replacement_name,
            "code": "RELOCATION_COMPLETE",
            "previous_name": name,
            "replacement_name": replacement_name,
            "attempts": attempts,
        }
    )
    commit(root, state)


def relocation_release_proof(target: dict[str, Any]) -> str:
    """Bind one reviewed release to the exact preserved relocation block."""
    scope = {
        "expected": target.get("expected"),
        "attempts": target.get("attempts"),
        "native_count": target.get("native_count"),
        "code": target.get("code"),
        "blocked": target.get("blocked"),
        "relocation": target.get("relocation"),
        "automatic": target.get("automatic"),
    }
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def release_relocation(root: Path, name: str, identity: str, proof: str) -> None:
    """Release only the exact block retained by a completed relocation."""
    if any(re.fullmatch(r"[a-f0-9]{64}", value) is None for value in (identity, proof)):
        raise ValueError("RELOCATION_RELEASE_PROOF_REQUIRED")
    state: dict[str, Any] = json.loads((root / "state.json").read_text())
    if docker(state, "info", "--format", "{{.ID}}").decode().strip() != state["daemon"]:
        raise ValueError("DAEMON_CHANGED")
    target = state["targets"][name]
    relocation = target.get("relocation", {})
    preserved = relocation.get("preserved_block")
    if (
        target.get("expected", {}).get("id") != identity
        or relocation.get("phase") != "COMPLETE"
        or relocation.get("preserve_block") is not True
        or not isinstance(preserved, dict)
        or target.get("blocked") is not True
        or target.get("code") != preserved.get("code")
        or relocation_release_proof(target) != proof
        or "restart_intent" in target
        or "intent" in target.get("automatic", {})
        or target.get("replacement_policy_pending")
    ):
        raise ValueError("RELOCATION_RELEASE_CHANGED")
    row = inspect(state, name)
    if (
        fingerprint(row) != target["expected"]
        or not row["State"].get("Running")
        or row["State"].get("Health", {}).get("Status") != "healthy"
        or bad_probe(row, time.time()) is not None
        or row["HostConfig"].get("RestartPolicy") != NO_RESTART
    ):
        raise ValueError("RELOCATION_RELEASE_TARGET_NOT_HEALTHY")
    relocation["release"] = {
        "proof": proof,
        "previous_block": preserved,
        "released_at": time.time(),
    }
    automatic = target.get("automatic")
    remaining = max(0, LIMIT - target["attempts"])
    target.update(
        blocked=automatic is None and not remaining,
        code=(
            "AUTOMATIC_RELOCATED"
            if automatic is not None
            else "CIRCUIT_OPEN" if not remaining else "RELOCATED_BUDGET_PRESERVED"
        ),
    )
    state.setdefault("events", []).append(
        {
            "at": time.time(),
            "target": name,
            "code": "OPERATOR_RELOCATION_BLOCK_RELEASED",
            "receipt": relocation["release"],
        }
    )
    commit(root, state)


def failed(target: dict[str, Any], code: str, now: float) -> None:
    target.update(code=code, last_failure_code=code, last_failure_at=now)


def diagnostic(
    row: dict[str, Any] | None,
    phase: str,
    since: float,
    code: str,
    error: Exception | None = None,
) -> dict[str, Any]:
    """Bounded structured facts only; never argv, probe output or error text."""
    result: dict[str, Any] = {
        "version": 1,
        "phase": phase,
        "code": code,
        "recorded_at": time.time(),
        "elapsed_seconds": round(max(0, time.monotonic() - since), 6),
    }
    if error is not None:
        result["error_type"] = type(error).__name__[:64]
    if row is None:
        return result
    process = row.get("State", {})
    if not isinstance(process, dict):
        return result
    result["running"] = process.get("Running") is True
    result["oom_killed"] = process.get("OOMKilled") is True
    for key, value in (
        ("restart_count", row.get("RestartCount")),
        ("process_exit_code", process.get("ExitCode")),
    ):
        if type(value) is int:
            result[key] = value
    status = process.get("Status")
    if isinstance(status, str) and status in {
        "created",
        "running",
        "paused",
        "restarting",
        "removing",
        "exited",
        "dead",
    }:
        result["process_status"] = status
    health = process.get("Health", {})
    if not isinstance(health, dict):
        return result
    status = health.get("Status")
    if isinstance(status, str) and status in {"starting", "healthy", "unhealthy"}:
        result["probe_status"] = status
    records = health.get("Log", [])
    if not isinstance(records, list):
        return result
    probes = []
    for record in records[-3:]:
        if not isinstance(record, dict):
            continue
        sample: dict[str, Any] = {}
        if type(record.get("ExitCode")) is int:
            sample["exit_code"] = record["ExitCode"]
        times: dict[str, datetime] = {}
        for key in ("Start", "End"):
            stamp = record.get(key)
            if not isinstance(stamp, str) or len(stamp) > 64:
                continue
            try:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                times[key] = parsed
                sample[key.lower()] = parsed.isoformat()
        if len(times) == 2 and times["End"] >= times["Start"]:
            sample["seconds"] = (times["End"] - times["Start"]).total_seconds()
        probes.append(sample)
    result["probes"] = probes
    return result


def account_native(
    state: dict[str, Any],
    name: str,
    target: dict[str, Any],
    row: dict[str, Any],
    now: float,
) -> None:
    """Count native exits exactly once without replenishing an observed budget."""
    delta = max(0, row["RestartCount"] - target["native_count"])
    target["native_count"] = row["RestartCount"]
    if delta:
        target["attempts"] += delta
        failed(target, "NATIVE_RESTART_OBSERVED", now)
        target["last_failure_diagnostic"] = diagnostic(
            row, "NATIVE_ACCOUNTING", time.monotonic(), "NATIVE_RESTART_OBSERVED"
        )
        state.setdefault("events", []).append(
            {
                "at": now,
                "target": name,
                "code": "NATIVE_RESTART_OBSERVED",
                "count": delta,
            }
        )


def bad_probe(row: dict[str, Any], now: float) -> str | None:
    """Use the current observation, including a check that never completed."""
    health = row["State"]["Health"]
    if health["Status"] == "unhealthy":
        return "UNHEALTHY"
    checks = row["Config"]["Healthcheck"]
    interval = checks.get("Interval", 30_000_000_000) / 1e9
    log = health.get("Log", [])
    stamp = log[-1]["End"] if log else row["State"].get("StartedAt")
    if stamp:
        age = now - datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        grace = (
            max(45, interval * 3)
            if log
            else max(60, interval * 3 + checks.get("StartPeriod", 0) / 1e9)
        )
        if age > grace:
            return "PROBE_STALE"
    return None


def started_at(row: dict[str, Any]) -> str:
    """A restart proof needs the process incarnation, not only container ID."""
    stamp = row["State"].get("StartedAt")
    if not isinstance(stamp, str) or not stamp or stamp.startswith("0001-"):
        raise ValueError("PROCESS_INCARNATION_REQUIRED")
    if datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("PROCESS_INCARNATION_REQUIRED")
    return stamp


def restart_phase(root: Path, state: dict[str, Any], name: str, phase: str) -> None:
    """Persist each external-effect boundary without spending another attempt."""
    target = state["targets"][name]
    intent = target["restart_intent"]
    intent["phase"] = phase
    state.setdefault("events", []).append(
        {
            "at": time.time(),
            "target": name,
            "code": "RESTART_" + phase,
            "intent_id": intent["id"],
            "attempt": intent["attempt"],
        }
    )
    commit(root, state)


def reconcile_restart(
    root: Path, state: dict[str, Any], name: str, row: dict[str, Any]
) -> None:
    """Reconcile one dispatched restart; never issue start/restart from here."""
    target = state["targets"][name]
    intent = target["restart_intent"]
    if (
        not isinstance(intent, dict)
        or type(intent.get("version")) is not int
        or intent["version"] != 1
        or intent.get("expected") != target["expected"]
        or type(intent.get("attempt")) is not int
        or intent["attempt"] < 1
        or intent["attempt"] > target["attempts"]
        or not isinstance(intent.get("id"), str)
        or re.fullmatch(r"[a-f0-9]{64}", intent["id"]) is None
        or not isinstance(intent.get("source_started_at"), str)
        or not isinstance(intent.get("original_policy"), dict)
        or intent["original_policy"].get("Name") != "on-failure"
        or not isinstance(intent.get("phase"), str)
        or intent.get("phase") not in {"PREPARED", "DISPATCH_INTENT", "POLICY_PENDING"}
    ):
        target["blocked"] = True
        failed(target, "RESTART_INTENT_INVALID", time.time())
        return
    if intent["phase"] == "POLICY_PENDING" and (
        not isinstance(intent.get("outcome"), str)
        or intent["outcome"] not in {"PROCESS_OBSERVED", "NOT_DISPATCHED"}
        or not isinstance(intent.get("observed_started_at"), str)
        or type(intent.get("observed_native_count")) is not int
        or intent["observed_native_count"] < 0
        or not isinstance(intent.get("restore_policy"), dict)
    ):
        target["blocked"] = True
        failed(target, "RESTART_INTENT_INVALID", time.time())
        return
    if target["blocked"] and target["code"] not in {
        "RESTART_RESULT_UNCONFIRMED",
        "CIRCUIT_OPEN",
        "RUNTIME_CHECK_FAILED",
    }:
        return  # Suspension, security/guard blocks and old ambiguity stay closed.
    current_stamp = started_at(row)
    if intent["phase"] == "DISPATCH_INTENT":
        if current_stamp == intent["source_started_at"]:
            # The command may still be executing. Even a stopped process is not
            # authority to dispatch a second command or start a manual stop.
            target["blocked"] = True
            failed(target, "RESTART_RESULT_UNCONFIRMED", time.time())
            return
        intent["outcome"] = "PROCESS_OBSERVED"
    elif intent["phase"] == "PREPARED":
        if current_stamp != intent["source_started_at"]:
            target["blocked"] = True
            failed(target, "RESTART_PROCESS_CHANGED", time.time())
            return
        # No dispatch intent exists: do not restart now using a stale guard.
        # The reserved attempt stays spent; a later new attempt needs its guard.
        intent["outcome"] = "NOT_DISPATCHED"
    elif current_stamp != intent["observed_started_at"]:
        # Native restarts under an already-restored finite policy are accounted
        # before this function; an unexplained new process is never adopted.
        if row["RestartCount"] <= intent["observed_native_count"]:
            target["blocked"] = True
            failed(target, "RESTART_PROCESS_CHANGED", time.time())
            return
    policy = row["HostConfig"]["RestartPolicy"]
    remaining = max(0, LIMIT - target["attempts"])
    desired = {
        "Name": "on-failure" if remaining else "no",
        "MaximumRetryCount": remaining,
    }
    allowed = [{"Name": "no", "MaximumRetryCount": 0}, desired]
    if intent["phase"] == "PREPARED":
        allowed.append(intent["original_policy"])
    elif intent["phase"] == "POLICY_PENDING":
        allowed.append(intent["restore_policy"])
    if policy not in allowed:
        target["blocked"] = True
        failed(target, "RESTART_POLICY_CHANGED", time.time())
        commit(root, state)
        docker(state, "update", "--restart", "no", row["Id"])
        return
    intent.update(
        observed_started_at=current_stamp,
        observed_native_count=row["RestartCount"],
        restore_policy=desired,
    )
    target["policy_limit"] = remaining
    # Commit the reduced budget before native policy can start retrying.
    restart_phase(root, state, name, "POLICY_PENDING")
    if policy != desired:
        docker(
            state,
            "update",
            "--restart",
            f"on-failure:{remaining}" if remaining else "no",
            row["Id"],
        )
    checked = inspect(state, name)
    if (
        fingerprint(checked) != target["expected"]
        or started_at(checked) != current_stamp
    ):
        raise ValueError("RESTART_CHANGED_DURING_CONFIRMATION")
    if checked["HostConfig"]["RestartPolicy"] != desired:
        raise ValueError("RESTART_POLICY_UNCONFIRMED")
    # A native retry during policy confirmation is observed on the next tick;
    # never reset its counter to an older sampled value here.
    intent["phase"] = "COMPLETE"
    target["last_restart"] = target.pop("restart_intent")
    target["blocked"] = target["attempts"] >= LIMIT
    target["next_due"] = time.time() + DELAYS[min(target["attempts"], LIMIT - 1)]
    target["code"] = (
        "CIRCUIT_OPEN"
        if target["blocked"]
        else (
            "STOPPED_NO_AUTOSTART"
            if not checked["State"]["Running"]
            else (
                "RESTART_NOT_DISPATCHED"
                if intent["outcome"] == "NOT_DISPATCHED"
                else "RESTARTED"
            )
        )
    )
    state.setdefault("events", []).append(
        {
            "at": time.time(),
            "target": name,
            "code": "RESTART_CONFIRMED",
            "intent_id": intent["id"],
            "attempt": target["attempts"],
            "outcome": intent["outcome"],
            "started_at": current_stamp,
        }
    )
    commit(root, state)


def restart(root: Path, state: dict[str, Any], name: str, row: dict[str, Any]) -> None:
    """One guarded dispatch, with a durable reservation and phase boundaries."""
    target = state["targets"][name]
    source = started_at(row)
    now = time.time()
    target["last_action_diagnostic"] = diagnostic(
        row,
        "BEFORE_RESTART",
        time.monotonic(),
        target.get("last_failure_code", "UNHEALTHY"),
    )
    state.setdefault("events", []).append(
        {
            "at": now,
            "target": name,
            "code": "PRE_ACTION_DIAGNOSTIC",
            "diagnostic": target["last_action_diagnostic"],
        }
    )
    target["attempts"] += 1
    target.update(
        last_action_at=now,
        next_due=now + DELAYS[min(target["attempts"], LIMIT - 1)],
        code="RESTART_INTENT",
    )
    target["restart_intent"] = {
        "version": 1,
        "id": hashlib.sha256(
            f"{row['Id']}:{source}:{target['attempts']}:{now}".encode()
        ).hexdigest(),
        "expected": fingerprint(row),
        "source_started_at": source,
        "attempt": target["attempts"],
        "original_policy": row["HostConfig"]["RestartPolicy"],
    }
    restart_phase(root, state, name, "PREPARED")
    docker(state, "update", "--restart", "no", row["Id"])
    checked = inspect(state, name)
    if (
        fingerprint(checked) != target["expected"]
        or started_at(checked) != source
        or not checked["State"]["Running"]
        or checked["State"].get("Restarting", False)
        or checked["HostConfig"]["RestartPolicy"]
        != {"Name": "no", "MaximumRetryCount": 0}
    ):
        target["blocked"] = True
        failed(target, "TARGET_CHANGED_BEFORE_ACTION", time.time())
        commit(root, state)
        return
    restart_phase(root, state, name, "DISPATCH_INTENT")
    docker(state, "restart", "--timeout", "10", row["Id"])
    checked = inspect(state, name)
    if fingerprint(checked) != target["expected"]:
        raise ValueError("RESTART_TARGET_CHANGED")
    account_native(state, name, target, checked, time.time())
    reconcile_restart(root, state, name, checked)


def automatic_enable(root: Path, name: str) -> None:
    """Explicitly migrate a live exact target; never adopt a stopped process."""
    state: dict[str, Any] = json.loads((root / "state.json").read_bytes())
    if docker(state, "info", "--format", "{{.ID}}").decode().strip() != state["daemon"]:
        raise ValueError("DAEMON_CHANGED")
    target = state["targets"][name]
    row = inspect(state, name)
    if (
        fingerprint(row) != target["expected"]
        or not row["State"]["Running"]
        or row["State"].get("Paused")
        or "Health" not in row["State"]
        or "restart_intent" in target
        or target.get("replacement_policy_pending")
        or target.get("cancel_suspend", {}).get("phase", "COMPLETE") != "COMPLETE"
    ):
        raise ValueError("AUTOMATIC_LIVE_EXACT_TARGET_REQUIRED")
    auto = target.get("automatic")
    if auto is not None and target["blocked"] and target["code"] != "CIRCUIT_OPEN":
        raise ValueError("AUTOMATIC_PROTECTIVE_BLOCK_PRESERVED")
    if auto is None:
        if (
            target.get("replacement_block", {}).get("code", "CIRCUIT_OPEN")
            != "CIRCUIT_OPEN"
        ):
            raise ValueError("AUTOMATIC_PROTECTIVE_BLOCK_PRESERVED")
        if target["blocked"] and target["code"] != "CIRCUIT_OPEN":
            raise ValueError("AUTOMATIC_PROTECTIVE_BLOCK_PRESERVED")
        expected = {
            "Name": "on-failure" if target["policy_limit"] else "no",
            "MaximumRetryCount": target["policy_limit"],
        }
        if row["HostConfig"]["RestartPolicy"] != expected:
            raise ValueError("AUTOMATIC_POLICY_CHANGED")
        account_native(state, name, target, row, time.time())
        now = time.time()
        auto = {
            "version": 1,
            "phase": "ENABLE_PENDING",
            "burst_attempts": min(LIMIT, target["attempts"]),
            "healthy_since": None,
            "healthy_process": None,
            "observed_at": now,
            "observed_started_at": started_at(row),
            "enabled_at": now,
            "previous_policy": expected,
        }
        target["automatic"] = auto
        target["next_due"] = max(
            target.get("next_due", 0),
            now + (AUTO_COOLDOWN if target["attempts"] >= LIMIT else DELAYS[0]),
        )
        state["schema_version"] = 2
        if "cancel_suspend" in target:
            state.setdefault("events", []).append(
                {
                    "at": now,
                    "target": name,
                    "code": "AUTOMATIC_ARCHIVED_CANCELLATION",
                    "receipt": target.pop("cancel_suspend"),
                }
            )
        state.setdefault("events", []).append(
            {
                "at": now,
                "target": name,
                "code": "AUTOMATIC_ENABLE_INTENT",
                "lifetime_attempts": target["attempts"],
            }
        )
        commit(root, state)
    if auto["phase"] == "ACTIVE":
        if row["HostConfig"]["RestartPolicy"] != NO_RESTART:
            raise ValueError("AUTOMATIC_POLICY_CHANGED")
        return
    if (
        auto["phase"] != "ENABLE_PENDING"
        or started_at(row) != auto["observed_started_at"]
        or row["HostConfig"]["RestartPolicy"]
        not in (NO_RESTART, auto["previous_policy"])
    ):
        raise ValueError("AUTOMATIC_ENABLE_UNCONFIRMED")
    docker(state, "update", "--restart", "no", row["Id"])
    checked = inspect(state, name)
    if (
        fingerprint(checked) != target["expected"]
        or not checked["State"]["Running"]
        or started_at(checked) != auto["observed_started_at"]
        or checked["HostConfig"]["RestartPolicy"] != NO_RESTART
    ):
        raise ValueError("AUTOMATIC_ENABLE_UNCONFIRMED")
    auto["phase"] = "ACTIVE"
    target.update(
        blocked=False, code="AUTOMATIC_ENABLED", policy_limit=0, bad_since=None
    )
    commit(root, state)


def automatic_block(target: dict[str, Any], code: str, now: float) -> None:
    target["blocked"] = True
    failed(target, code, now)


def automatic_stamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("AUTOMATIC_TIMESTAMP_INVALID")
    return parsed.timestamp()


def automatic_exit_records(
    auto: dict[str, Any],
    row: dict[str, Any],
    pending: dict[str, Any],
    records: list[dict[str, Any]],
    boundary: float,
) -> bool | None:
    """Prove one crash in a fully covered interval of the observed process."""
    since = pending["since"]
    finished = automatic_stamp(pending["finished_at"])
    if not since <= finished <= boundary:
        return False  # Never repair an older, already advanced proof window.
    deaths: set[tuple[int, str]] = set()
    for record in records:
        if record["id"] != row["Id"]:
            continue
        stamp = record["stamp"] / 1e9
        action = record["action"]
        if not since <= stamp <= boundary:
            return False
        if action in CONTROL_EVENTS - {"die"}:
            own = auto.get("last_action", {})
            if not (
                action == "restart"
                and own.get("verb") == "restart"
                and own.get("observed_started_at") == started_at(row)
                and own["dispatched_at"] <= stamp <= own["confirmed_at"]
            ):
                return False
        if action == "die":
            if stamp < finished or record["exit_code"] != str(pending["exit_code"]):
                return False
            deaths.add((record["stamp"], record["exit_code"]))
    if len(deaths) > 1:
        return False
    if not deaths:
        return None  # The event can arrive after FinishedAt; retain the window.
    pending["proven"] = True
    # Keep the original lower bound through backoff and dispatch. Every check
    # must still prove one death and no intervening operator control, even when
    # the durable journal and fresh Docker tail overlap.
    return True


def automatic_exit(
    state: dict[str, Any],
    target: dict[str, Any],
    row: dict[str, Any],
    now: float,
    root: Path | None = None,
) -> bool | None:
    """Read bounded event evidence, including proof that Docker history covers it."""
    auto = target["automatic"]
    process = row["State"]
    if (
        process.get("Status") != "exited"
        or process.get("Paused")
        or process.get("Dead")
        or process.get("Restarting")
        or type(process.get("ExitCode")) is not int
        or process["ExitCode"] == 0
        or started_at(row) != auto["observed_started_at"]
    ):
        return False
    finished = automatic_stamp(process["FinishedAt"])
    pending = auto.get("exit")
    if pending is None:
        pending = {
            "finished_at": process["FinishedAt"],
            "exit_code": process["ExitCode"],
            "since": auto["observed_at"],
            "proven": False,
        }
        auto["exit"] = pending
    if (
        pending["finished_at"] != process["FinishedAt"]
        or pending["exit_code"] != process["ExitCode"]
    ):
        return False
    # Docker retains only 256 events. Anonymous timestamps prove coverage; a
    # matching die alone cannot prove that an earlier operator kill was retained.
    boundary = now
    since = pending["since"]
    if boundary == since:
        return pending["proven"] is True
    if boundary < since or finished > boundary:
        return False
    if root is not None and (root / "event-observation.json").exists():
        try:
            records = durable_events(root, state, since, boundary)
        except ValueError:
            return False
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return None
        if records is None:
            return None
        return automatic_exit_records(auto, row, pending, records, boundary)
    coverage = docker(
        state,
        "events",
        "--since",
        "0",
        "--until",
        f"{boundary:.9f}",
        "--format",
        "{{.TimeNano}}",
    )
    if len(coverage) > 32768:
        return False
    stamps = [int(item) / 1e9 for item in coverage.splitlines()]
    if not stamps or len(stamps) > 256 or min(stamps) > since:
        return False
    raw = docker(
        state,
        "events",
        "--since",
        f"{since:.9f}",
        "--until",
        f"{boundary:.9f}",
        "--filter",
        "type=container",
        "--filter",
        "container=" + row["Id"],
        "--format",
        "json",
    )
    if len(raw) > 2 * 1024**2:
        return False
    records = []
    for line in raw.splitlines():
        event = json.loads(line)
        if event.get("Actor", {}).get("ID") != row["Id"]:
            return False
        records.append(
            {
                "id": row["Id"],
                "stamp": event["timeNano"],
                "action": event.get("Action", ""),
                "exit_code": event.get("Actor", {})
                .get("Attributes", {})
                .get("exitCode"),
            }
        )
    return automatic_exit_records(auto, row, pending, records, boundary)


def automatic_reconcile(
    root: Path, state: dict[str, Any], name: str, row: dict[str, Any]
) -> None:
    """Confirm a reserved effect, never reissue an ambiguous command."""
    target = state["targets"][name]
    auto = target["automatic"]
    intent = auto["intent"]
    if intent.get("expected") != target["expected"] or intent.get("verb") not in {
        "start",
        "restart",
    }:
        automatic_block(target, "AUTOMATIC_INTENT_INVALID", time.time())
        return
    if intent["phase"] == "PREPARED":
        auto["last_action"] = auto.pop("intent")
        target["code"] = "AUTOMATIC_NOT_DISPATCHED"
        commit(root, state)
        return
    if (
        intent["phase"] != "DISPATCH_INTENT"
        or started_at(row) == intent["source_started_at"]
    ):
        automatic_block(target, "AUTOMATIC_RESULT_UNCONFIRMED", time.time())
        return
    intent.update(
        phase="COMPLETE", observed_started_at=started_at(row), confirmed_at=time.time()
    )
    auto["last_action"] = auto.pop("intent")
    auto.update(
        observed_started_at=started_at(row),
        observed_at=automatic_stamp(started_at(row)),
        healthy_since=None,
        healthy_process=None,
    )
    auto.pop("exit", None)
    target.update(code="AUTOMATIC_RESTARTED", blocked=False, bad_since=None)
    state.setdefault("events", []).append(
        {
            "at": time.time(),
            "target": name,
            "code": "AUTOMATIC_CONFIRMED",
            "intent": auto["last_action"],
        }
    )
    commit(root, state)


def automatic_dispatch(
    root: Path, state: dict[str, Any], name: str, row: dict[str, Any]
) -> None:
    target = state["targets"][name]
    auto = target["automatic"]
    now = time.time()
    verb = "restart" if row["State"]["Running"] else "start"
    target["last_action_diagnostic"] = diagnostic(
        row, "BEFORE_AUTOMATIC_" + verb.upper(), time.monotonic(), target["code"]
    )
    target["attempts"] += 1
    auto["burst_attempts"] += 1
    target["next_due"] = now + (
        AUTO_COOLDOWN
        if auto["burst_attempts"] >= LIMIT
        else DELAYS[auto["burst_attempts"]]
    )
    auto["intent"] = {
        "phase": "PREPARED",
        "verb": verb,
        "source_started_at": started_at(row),
        "attempt": target["attempts"],
        "expected": target["expected"],
        "prepared_at": now,
    }
    state.setdefault("events", []).append(
        {
            "at": now,
            "target": name,
            "code": "AUTOMATIC_ACTION_INTENT",
            "intent": dict(auto["intent"]),
            "diagnostic": target["last_action_diagnostic"],
        }
    )
    commit(root, state)
    checked = inspect(state, name)
    if (
        fingerprint(checked) != target["expected"]
        or started_at(checked) != started_at(row)
        or checked["HostConfig"]["RestartPolicy"] != NO_RESTART
        or checked["State"]["Running"] != row["State"]["Running"]
    ):
        automatic_block(target, "AUTOMATIC_TARGET_CHANGED_BEFORE_ACTION", time.time())
        commit(root, state)
        return
    if not checked["State"]["Running"]:
        evidence = automatic_exit(state, target, checked, time.time(), root)
        if evidence is None:
            # No command was dispatched. Preserve the reserved attempt in the
            # audit, but do not turn a lagging reader into an uncertain effect.
            abandoned = auto.pop("intent")
            state.setdefault("events", []).append(
                {
                    "at": time.time(),
                    "target": name,
                    "code": "AUTOMATIC_OBSERVATION_DEFERRED",
                    "intent": abandoned,
                }
            )
            target["code"] = "AUTOMATIC_OBSERVATION_WAIT"
            commit(root, state)
            return
        if not evidence:
            automatic_block(target, "AUTOMATIC_EXIT_UNCONFIRMED", time.time())
            commit(root, state)
            return
    auto["intent"].update(phase="DISPATCH_INTENT", dispatched_at=time.time())
    commit(root, state)
    arguments = (
        ("restart", "--timeout", "10", row["Id"])
        if verb == "restart"
        else ("start", row["Id"])
    )
    docker(state, *arguments)
    checked = inspect(state, name)
    if fingerprint(checked) != target["expected"]:
        automatic_block(target, "TARGET_REPLACED", time.time())
        return
    automatic_reconcile(root, state, name, checked)


def automatic_tick(
    root: Path,
    state: dict[str, Any],
    name: str,
    row: dict[str, Any],
    sampled: float,
    acted: bool,
) -> bool:
    target = state["targets"][name]
    auto = target["automatic"]
    now = time.time()
    if auto.get("version") != 1 or auto.get("phase") != "ACTIVE":
        return False
    protective = target.get("replacement_block", {}).get("code")
    if protective is not None and protective != "CIRCUIT_OPEN":
        if not target["blocked"]:
            automatic_block(target, protective, now)
        return False
    if row["HostConfig"]["RestartPolicy"] != NO_RESTART:
        automatic_block(target, "AUTOMATIC_POLICY_CHANGED", now)
        return False
    if target["blocked"] and target["code"] == "AUTOMATIC_EXIT_UNCONFIRMED":
        # A confirmed external start rearms observation, never starts a stopped
        # process here. This includes the owner's already guarded fencing return.
        if (
            "intent" not in auto
            and row["State"]["Running"]
            and started_at(row) != auto["observed_started_at"]
            and row["State"]["Health"]["Status"] == "healthy"
            and bad_probe(row, now) is None
        ):
            target["blocked"] = False
            auto.update(healthy_since=None, healthy_process=None)
            auto.pop("exit", None)
            state.setdefault("events", []).append(
                {
                    "at": now,
                    "target": name,
                    "code": "AUTOMATIC_EXTERNAL_START_OBSERVED",
                    "started_at": started_at(row),
                }
            )
    if target["blocked"] and target["code"] != "AUTOMATIC_RESULT_UNCONFIRMED":
        return False
    if "intent" in auto:
        automatic_reconcile(root, state, name, row)
        # Reconciliation only reads and persists the result of an already
        # dispatched command. It must not consume this tick's single Docker
        # action or starve recovery of an unrelated target.
        return False
    account_native(state, name, target, row, now)
    if row["State"].get("Paused") or row["State"].get("Restarting"):
        auto["healthy_since"] = None
        return False
    running = row["State"]["Running"]
    reason = bad_probe(row, now) if running else "PROCESS_EXITED"
    if running and reason is None and row["State"]["Health"]["Status"] == "healthy":
        continuous = (
            auto["healthy_since"] is not None
            and auto["healthy_process"] == started_at(row)
            and 0 <= sampled - auto["observed_at"] <= 120
        )
        if not continuous:
            auto.update(healthy_since=sampled, healthy_process=started_at(row))
        elif now - auto["healthy_since"] >= AUTO_HEALTHY and auto["burst_attempts"]:
            auto["burst_attempts"] = 0
            target["next_due"] = now
            state.setdefault("events", []).append(
                {
                    "at": now,
                    "target": name,
                    "code": "AUTOMATIC_STABLE_RECOVERY",
                    "lifetime_attempts": target["attempts"],
                }
            )
        auto.update(observed_at=sampled, observed_started_at=started_at(row))
        auto.pop("exit", None)
        target.update(code="HEALTHY", bad_since=None)
        return False
    auto.update(healthy_since=None, healthy_process=None)
    if reason is None:
        target["code"] = "STARTING"
        return False
    if not running:
        evidence = automatic_exit(state, target, row, now, root)
        if evidence is None:
            target["code"] = "AUTOMATIC_OBSERVATION_WAIT"
            return False
        if not evidence:
            automatic_block(target, "AUTOMATIC_EXIT_UNCONFIRMED", now)
            return False
    elif started_at(row) != auto["observed_started_at"]:
        auto.update(observed_started_at=started_at(row), observed_at=sampled)
    if target["bad_since"] is None or not 0 <= sampled - auto["observed_at"] <= 120:
        target["bad_since"] = now
    auto["observed_at"] = sampled
    failed(target, reason, now)
    target["last_failure_diagnostic"] = diagnostic(
        row, "AUTOMATIC_PROBE", time.monotonic(), reason
    )
    due = max(target["next_due"], target["bad_since"] + DELAYS[0])
    target["code"] = (
        "AUTOMATIC_COOLDOWN" if auto["burst_attempts"] >= LIMIT else "AUTOMATIC_BACKOFF"
    )
    target["next_due"] = due
    if now < due or acted:
        return False
    if not dependencies_ready(state, row):
        target.update(code="AUTOMATIC_DEPENDENCY_WAIT", next_due=now + DEPENDENCY_RETRY)
        return False
    guard = target["guard"]
    if guard is not None:
        path = Path(guard["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != guard["sha256"]:
            automatic_block(target, "GUARD_CHANGED", now)
            return False
        answer = json.loads(execute([sys.executable, str(path), row["Id"]], timeout=50))
        if answer.get("allowed") is not True:
            target.update(code="OWNER_GUARD_BLOCKED", next_due=now + DELAYS[1])
            return False
    current = inspect(state, name)
    if (
        fingerprint(current) != target["expected"]
        or current["State"]["Running"] != running
    ):
        return False
    if running and bad_probe(current, time.time()) is None:
        return False
    automatic_dispatch(root, state, name, current)
    return True


def automatic_handoff(
    root: Path,
    state: dict[str, Any],
    name: str,
    row: dict[str, Any],
    now: float,
) -> str | None:
    """Transfer an opted-in automatic target after a stable Compose replacement."""
    target = state["targets"][name]
    auto = target.get("automatic")
    current = fingerprint(row)
    previous = target["expected"]
    if (
        not isinstance(auto, dict)
        or auto.get("version") != 1
        or auto.get("phase") != "ACTIVE"
        or target.get("blocked") and target.get("code") != "TARGET_REPLACED"
        or target.get("code")
        in {
            "OPERATOR_SUSPENDED",
            "GUARD_CHANGED",
            "OWNER_GUARD_BLOCKED",
            "AUTOMATIC_RESULT_UNCONFIRMED",
        }
        or "restart_intent" in target
        or "intent" in auto
        or target.get("replacement_policy_pending")
        or target.get("cancel_suspend", {}).get("phase", "COMPLETE") != "COMPLETE"
        or current["project"] != previous["project"]
        or current["service"] != previous["service"]
        or current["id"] == previous["id"]
        or re.fullmatch(r"sha256:[a-f0-9]{64}", current["image"]) is None
        or re.fullmatch(r"[a-f0-9]{64}", current.get("config_hash") or "") is None
        or row.get("Name") != "/" + name
        or not row["State"].get("Running")
        or row["State"].get("Paused")
        or row["State"].get("Restarting")
        or row["State"].get("Health", {}).get("Status") != "healthy"
        or bad_probe(row, now) is not None
        or type(row.get("RestartCount")) is not int
        or row["RestartCount"] < 0
        or row["HostConfig"].get("RestartPolicy")
        not in (
            NO_RESTART,
            {"Name": "on-failure", "MaximumRetryCount": 1},
            {"Name": "on-failure", "MaximumRetryCount": 2},
            {"Name": "on-failure", "MaximumRetryCount": 3},
        )
    ):
        target.pop("automatic_handoff", None)
        return None
    old = (
        docker(
            state,
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            "id=" + previous["id"],
            "--format",
            "{{.ID}}",
        )
        .decode()
        .splitlines()
    )
    if old:
        target.pop("automatic_handoff", None)
        return None
    candidate = target.get("automatic_handoff")
    if not isinstance(candidate, dict) or candidate.get("expected") != current:
        target["automatic_handoff"] = {"expected": current, "first_seen": now}
        target.update(code="AUTOMATIC_HANDOFF_WAIT", blocked=False)
        state.setdefault("events", []).append(
            {
                "at": now,
                "target": name,
                "code": "AUTOMATIC_HANDOFF_CANDIDATE",
                "previous": previous,
                "replacement": current,
            }
        )
        return "waiting"
    first_seen = candidate.get("first_seen")
    if not isinstance(first_seen, (int, float)) or now - first_seen < HANDOFF_STABLE:
        target.update(code="AUTOMATIC_HANDOFF_WAIT", blocked=False)
        return "waiting"
    commit(root, state)
    replace(
        root,
        name,
        previous["id"],
        target["native_count"],
        current["image"],
    )
    updated = json.loads((root / "state.json").read_bytes())
    updated_target = updated["targets"][name]
    receipt = updated_target.pop("automatic_handoff", candidate)
    updated.setdefault("events", []).append(
        {
            "at": time.time(),
            "target": name,
            "code": "AUTOMATIC_HANDOFF_COMPLETE",
            "receipt": receipt,
        }
    )
    commit(root, updated)
    return "complete"


def once(root: Path) -> dict[str, Any]:
    """At most one restart per tick, with durable accounting and no auto reset."""
    state: dict[str, Any] = json.loads((root / "state.json").read_text())
    now = time.time()
    previous = state.get("checked_at", now)
    state.update(checked_at=now, code="OK")
    if docker(state, "info", "--format", "{{.ID}}").decode().strip() != state["daemon"]:
        state["code"] = "DAEMON_CHANGED"
        commit(root, state)
        return state
    if now < previous or now - previous > RESUME_GAP:
        state["resume"] = {
            "detected_at": now,
            "previous_checked_at": previous,
            "until": now + RESUME_GRACE,
        }
        state.setdefault("events", []).append(
            {
                "at": now,
                "code": "HOST_RESUME_GRACE_STARTED",
                "previous_checked_at": previous,
                "until": now + RESUME_GRACE,
            }
        )
    resume = state.get("resume")
    if isinstance(resume, dict) and now < resume.get("until", 0):
        for target in state["targets"].values():
            auto = target.get("automatic")
            if isinstance(auto, dict):
                auto.update(healthy_since=None, healthy_process=None)
                target.pop("automatic_handoff", None)
                if not target.get("blocked"):
                    target["code"] = "AUTOMATIC_RESUME_GRACE"
        commit(root, state)
        return state
    if isinstance(resume, dict):
        state.setdefault("events", []).append(
            {"at": now, "code": "HOST_RESUME_GRACE_COMPLETE"}
        )
        state.pop("resume", None)
    acted = False
    for name, target in state["targets"].items():
        row = None
        phase = "INSPECT"
        since = time.monotonic()

        def fail(code: str, observed: float, error: Exception | None = None) -> None:
            previous_code = target["code"]
            failed(target, code, observed)
            if (
                code == "RUNTIME_CHECK_FAILED"
                and "automatic" in target
                and target["blocked"]
            ):
                # A transient read cannot erase a hold or the durable intent's
                # reconciliation state. Keep the read error as separate evidence.
                target["code"] = previous_code
            target["last_failure_diagnostic"] = diagnostic(
                row, phase, since, code, error
            )

        try:
            sampled = time.time()
            row = inspect(state, name)
            now = time.time()
            phase = "IDENTITY_CHECK"
            if fingerprint(row) != target["expected"]:
                handoff = automatic_handoff(root, state, name, row, now)
                if handoff == "complete":
                    return once(root)
                if handoff == "waiting":
                    continue
                if target.get("blocked") and target.get("code") != "TARGET_REPLACED":
                    target["last_failure_at"] = now
                    target["last_failure_code"] = "TARGET_REPLACED"
                    target["last_failure_diagnostic"] = diagnostic(
                        row, phase, since, "TARGET_REPLACED"
                    )
                    continue
                target["blocked"] = True
                fail("TARGET_REPLACED", now)
                continue
            if "automatic" in target:
                phase = "AUTOMATIC_RECOVERY"
                acted = automatic_tick(root, state, name, row, sampled, acted) or acted
                continue
            phase = "NATIVE_ACCOUNTING"
            account_native(state, name, target, row, now)
            if "restart_intent" in target:
                # Our persisted effect must be reconciled before a temporary
                # native policy can be mistaken for somebody else's change.
                phase = "RESTART_RECONCILIATION"
                reconcile_restart(root, state, name, row)
                acted = True
                continue
            if target["attempts"] >= LIMIT:
                target["blocked"] = True
                # Keep the original failure evidence; repeated budget checks
                # must not replace it with an uninformative circuit snapshot.
                failed(target, "CIRCUIT_OPEN", now)
                commit(root, state)
                if row["HostConfig"]["RestartPolicy"]["Name"] != "no":
                    docker(state, "update", "--restart", "no", row["Id"])
                continue
            if target["blocked"]:
                continue
            if not row["State"]["Running"]:
                target["code"] = "STOPPED_NO_AUTOSTART"
                continue
            # A deployment must explicitly re-enroll changed runtime policy.
            policy = row["HostConfig"]["RestartPolicy"]
            phase = "POLICY_CHECK"
            if policy != {
                "Name": "on-failure",
                "MaximumRetryCount": target["policy_limit"],
            }:
                target["blocked"] = True
                fail("RESTART_POLICY_CHANGED", now)
                commit(root, state)
                docker(state, "update", "--restart", "no", row["Id"])
                continue
            phase = "PROBE_CHECK"
            health = row["State"]["Health"]
            reason = bad_probe(row, now)
            if reason is None:
                target.update(
                    code="HEALTHY" if health["Status"] == "healthy" else "STARTING",
                    bad_since=None,
                )
                continue
            if now - previous > 120 or now < previous or target["bad_since"] is None:
                target["bad_since"] = now
            fail(reason, now)
            due = max(target["bad_since"] + DELAYS[0], target["next_due"])
            target["next_due"] = due
            if now < due or acted:
                continue
            phase = "DEPENDENCY_CHECK"
            if not dependencies_ready(state, row):
                target.update(code="DEPENDENCY_WAIT", next_due=now + DEPENDENCY_RETRY)
                continue
            guard = target["guard"]
            if guard is not None:
                phase = "OWNER_GUARD"
                path = Path(guard["path"])
                if hashlib.sha256(path.read_bytes()).hexdigest() != guard["sha256"]:
                    target["blocked"] = True
                    fail("GUARD_CHANGED", now)
                    continue
                try:
                    answer = json.loads(
                        execute([sys.executable, str(path), row["Id"]], timeout=50)
                    )
                    if answer.get("allowed") is not True:
                        reason = answer.get("code", "UNAVAILABLE")
                        if not isinstance(reason, str) or not re.fullmatch(
                            r"[A-Z_]{1,64}", reason
                        ):
                            reason = "INVALID_RESPONSE"
                        fail("OWNER_GUARD_" + reason, now)
                        target["next_due"] = now + DELAYS[1]
                        continue
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                    fail("OWNER_GUARD_BLOCKED", now, error)
                    target["next_due"] = now + DELAYS[1]
                    continue
            # Guard latency is not authority to restart a replaced/stopped target.
            phase = "PRE_ACTION_RECHECK"
            current = inspect(state, name)
            now = time.time()
            if (
                fingerprint(current) != target["expected"]
                or not current["State"]["Running"]
            ):
                fail("TARGET_CHANGED_BEFORE_ACTION", now)
                continue
            account_native(state, name, target, current, now)
            if target["attempts"] >= LIMIT:
                target["blocked"] = True
                failed(target, "CIRCUIT_OPEN", now)
                commit(root, state)
                docker(state, "update", "--restart", "no", row["Id"])
                continue
            if bad_probe(current, now) is None:
                target["bad_since"] = None
                continue
            acted = True
            phase = "RESTART_DISPATCH"
            restart(root, state, name, current)
        except (
            OSError,
            ValueError,
            KeyError,
            RuntimeError,
            subprocess.TimeoutExpired,
        ) as error:
            fail("RUNTIME_CHECK_FAILED", now, error)
    commit(root, state)
    return state


def scheduler_label(root: Path) -> str:
    return "aelev-recovery-" + hashlib.sha256(str(root).encode()).hexdigest()[:16]


def unit_quote(value: str) -> str:
    """systemd argv syntax is not shell syntax; escape specifiers as well."""
    if any(c in value for c in "\r\n\x00"):
        raise ValueError("RECOVERY_TIMER_PATH_INVALID")
    return (
        '"'
        + value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
        + '"'
    )


def scheduler_settings(root: Path, script: Path) -> dict[str, bytes]:
    """One controller process, identical evidence and actuation on each host."""
    label = scheduler_label(root)
    if platform.system() == "Linux":
        argv = " ".join(
            unit_quote(v)
            for v in [sys.executable, str(script), "watch", "--state", str(root)]
        )
        return {
            label
            + ".service": (
                "[Unit]\nDescription=AELEV bounded recovery\n[Service]\nType=oneshot\n"
                "ExecStart="
                + argv
                + "\nNoNewPrivileges=yes\nTimeoutStartSec=infinity\n"
            ).encode(),
            label
            + ".timer": (
                "[Unit]\nDescription=AELEV bounded recovery timer\n[Timer]\n"
                "OnActiveSec=15s\nOnUnitInactiveSec=15s\nAccuracySec=1s\nUnit="
                + label
                + ".service\n[Install]\nWantedBy=timers.target\n"
            ).encode(),
        }
    if platform.system() != "Windows":
        raise ValueError("RECOVERY_HOST_TIMER_UNSUPPORTED")
    sid = powershell(
        "[Console]::Write([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)"
    ).decode()
    if re.fullmatch(r"S-1-[0-9-]{1,160}", sid) is None:
        raise ValueError("RECOVERY_TIMER_USER_UNCONFIRMED")
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", ns)
    task = ET.Element("{" + ns + "}Task", version="1.4")

    def child(parent: ET.Element, name: str, value: str | None = None) -> ET.Element:
        node = ET.SubElement(parent, "{" + ns + "}" + name)
        node.text = value
        return node

    principal = child(child(task, "Principals"), "Principal")
    principal.set("id", "Author")
    child(principal, "UserId", sid)
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    triggers = child(task, "Triggers")
    logon = child(triggers, "LogonTrigger")
    child(logon, "Enabled", "true")
    child(logon, "ExecutionTimeLimit", "PT0S")
    child(logon, "UserId", sid)
    trigger = child(triggers, "TimeTrigger")
    child(trigger, "Enabled", "true")
    child(trigger, "StartBoundary", "2026-01-01T00:00:00")
    repetition = child(trigger, "Repetition")
    child(repetition, "Interval", "PT1M")
    child(repetition, "StopAtDurationEnd", "false")
    child(trigger, "ExecutionTimeLimit", "PT0S")
    settings = child(task, "Settings")
    for key, value in {
        "MultipleInstancesPolicy": "IgnoreNew",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "AllowHardTerminate": "false",
        "StartWhenAvailable": "true",
        "Enabled": "true",
        "ExecutionTimeLimit": "PT0S",
    }.items():
        child(settings, key, value)
    actions = child(task, "Actions")
    actions.set("Context", "Author")
    action = child(actions, "Exec")
    child(action, "Command", sys.executable)
    child(
        action,
        "Arguments",
        subprocess.list2cmdline([str(script), "watch", "--state", str(root)]),
    )
    return {label + ".xml": ET.tostring(task, encoding="utf-8", xml_declaration=True)}


def task_script(label: str) -> str:
    return (
        "$ErrorActionPreference='Stop'; $name="
        + ps_literal(label)
        + "; $task=Get-ScheduledTask -TaskPath '\\AELEV\\' -TaskName $name -ErrorAction Stop; "
    )


def task_export(label: str) -> bytes:
    return powershell(
        task_script(label)
        + "[Console]::Write((Export-ScheduledTask -TaskPath '\\AELEV\\' -TaskName $name -ErrorAction Stop))"
    )


def task_digest(raw: bytes) -> str:
    """Enabled changes during our window; every other XML field is pinned."""
    tree = ET.fromstring(raw.decode("utf-8-sig"))
    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    enabled = tree.find("t:Settings/t:Enabled", namespace)
    if enabled is None:
        raise ValueError("RECOVERY_TIMER_INVALID")
    enabled.text = "true"
    # The scheduler may change indentation when Enabled is changed.
    for node in tree.iter():
        if node.text is not None and not node.text.strip():
            node.text = None
        node.tail = None
    return hashlib.sha256(ET.tostring(tree)).hexdigest()


def task_exists(label: str) -> bool:
    raw = powershell(
        "$ErrorActionPreference='Stop'; try { $task=Get-ScheduledTask -TaskPath '\\AELEV\\' -TaskName "
        + ps_literal(label)
        + " -ErrorAction Stop; [Console]::Write('PRESENT') } catch { if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { [Console]::Write('ABSENT') } else { throw } }"
    )
    if raw not in (b"PRESENT", b"ABSENT"):
        raise ValueError("RECOVERY_TIMER_UNCONFIRMED")
    return raw == b"PRESENT"


def verify_task(expected: bytes, actual: bytes) -> None:
    """Allow scheduler defaults, never a different action, principal or trigger."""
    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

    def shape(node: ET.Element | None) -> Any:
        if node is None:
            return None
        return (
            node.tag,
            node.attrib,
            (node.text or "").strip(),
            sorted([shape(c) for c in node]),
        )

    left, right = ET.fromstring(expected), ET.fromstring(actual.decode("utf-8-sig"))
    for name in ("Actions", "Principals", "Triggers"):
        if shape(left.find("t:" + name, namespace)) != shape(
            right.find("t:" + name, namespace)
        ):
            raise ValueError("RECOVERY_TIMER_REGISTRATION_UNCONFIRMED")
    for node in left.findall("t:Settings/*", namespace):
        other = right.find("t:Settings/" + node.tag, namespace)
        if other is None or other.text != node.text:
            raise ValueError("RECOVERY_TIMER_REGISTRATION_UNCONFIRMED")
    if right.find("t:Settings/t:RestartOnFailure", namespace) is not None:
        raise ValueError("RECOVERY_TIMER_REGISTRATION_UNCONFIRMED")


def scheduler(root: Path) -> tuple[str, Path, str]:
    """Verify the exact installed task and immutable script before any pause."""
    marker = root / "scheduler.json"
    if marker.is_symlink() or marker.stat().st_size > 65536:
        raise ValueError("RECOVERY_TIMER_INVALID")
    value = json.loads(marker.read_bytes())
    system = platform.system()
    if (
        value.get("system") != system
        or value.get("label") != scheduler_label(root)
        or value.get("phase") != "COMPLETE"
    ):
        raise ValueError("RECOVERY_TIMER_INVALID")
    script = root / ("recovery-" + value["release"] + ".py")
    if (
        script.is_symlink()
        or hashlib.sha256(script.read_bytes()).hexdigest() != value["release"]
    ):
        raise ValueError("RECOVERY_TIMER_SCRIPT_CHANGED")
    for name, digest in value["files"].items():
        if Path(name).name != name:
            raise ValueError("RECOVERY_TIMER_INVALID")
        path = Path(value["directory"]) / name
        if (
            path.is_symlink()
            or os.name != "nt"
            and (path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022)
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise ValueError("RECOVERY_TIMER_CHANGED")
    label = value["label"]
    if system == "Linux":
        expected = Path.home() / ".config/systemd/user"
        if Path(value["directory"]) != expected or set(value["files"]) != {
            label + ".service",
            label + ".timer",
        }:
            raise ValueError("RECOVERY_TIMER_INVALID")
        for suffix in ("service", "timer"):
            manager = execute(
                [
                    "systemctl",
                    "--user",
                    "show",
                    label + "." + suffix,
                    "--property=FragmentPath,NeedDaemonReload",
                ]
            ).decode()
            fields = dict(
                line.split("=", 1) for line in manager.splitlines() if "=" in line
            )
            if fields != {
                "FragmentPath": str(expected / (label + "." + suffix)),
                "NeedDaemonReload": "no",
            }:
                raise ValueError("RECOVERY_TIMER_MANAGER_CHANGED")
    elif system == "Windows":
        if (
            Path(value["directory"]) != root
            or set(value["files"]) != {label + ".xml"}
            or task_digest(task_export(label)) != value["registered_digest"]
        ):
            raise ValueError("RECOVERY_TIMER_MANAGER_CHANGED")
    else:
        raise ValueError("RECOVERY_HOST_TIMER_UNSUPPORTED")
    return system + ":" + label, marker, hashlib.sha256(marker.read_bytes()).hexdigest()


def scheduler_status(root: Path) -> dict[str, Any]:
    if platform.system() == "Darwin":
        label = (
            "com.aelev.recovery." + hashlib.sha256(str(root).encode()).hexdigest()[:16]
        )
        domain = f"gui/{os.getuid()}/{label}"
        active = timer_loaded(domain)
    else:
        domain, _, _ = scheduler(root)
        active = scheduler_loaded(domain)
    return {
        "code": "RECOVERY_TIMER_ACTIVE" if active else "RECOVERY_TIMER_INACTIVE",
        "timer": domain,
        "active": active,
    }


def scheduler_loaded(domain: str) -> bool:
    system, label = domain.split(":", 1)
    if system == "Linux":
        raw = execute(
            [
                "systemctl",
                "--user",
                "show",
                label + ".timer",
                "--property=LoadState,ActiveState,UnitFileState",
            ]
        ).decode()
        fields = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        if (
            fields.get("LoadState") != "loaded"
            or fields.get("UnitFileState") != "enabled"
            or fields.get("ActiveState") not in {"active", "inactive"}
        ):
            raise ValueError("RECOVERY_TIMER_UNCONFIRMED")
        return fields["ActiveState"] == "active"
    if system == "Windows":
        enabled = powershell(
            task_script(label) + "[Console]::Write([bool]$task.Settings.Enabled)"
        )
        if enabled not in (b"True", b"False"):
            raise ValueError("RECOVERY_TIMER_UNCONFIRMED")
        return enabled == b"True"
    raise ValueError("RECOVERY_HOST_TIMER_UNSUPPORTED")


def scheduler_switch(domain: str, *, enabled: bool) -> None:
    system, label = domain.split(":", 1)
    if system == "Linux":
        execute(
            ["systemctl", "--user", "start" if enabled else "stop", label + ".timer"]
        )
    elif system == "Windows":
        powershell(
            task_script(label)
            + ("Enable" if enabled else "Disable")
            + "-ScheduledTask -InputObject $task -ErrorAction Stop | Out-Null"
        )
    else:
        raise ValueError("RECOVERY_HOST_TIMER_UNSUPPORTED")
    if scheduler_loaded(domain) != enabled:
        raise ValueError("RECOVERY_TIMER_SWITCH_UNCONFIRMED")


def install_scheduler(root: Path) -> None:
    """Install only a private user task, with durable intent and no adoption."""
    system = platform.system()
    if system not in {"Linux", "Windows"}:
        raise ValueError("RECOVERY_HOST_TIMER_UNSUPPORTED")
    marker = root / "scheduler.json"
    previous: dict[str, Any] | None = None
    if marker.exists() or marker.is_symlink():
        if marker.is_symlink() or marker.stat().st_size > 65536:
            raise ValueError("RECOVERY_TIMER_INVALID")
        previous = json.loads(marker.read_bytes())
        # Never enable a manually stopped task, including an identical retry.
        if previous.get("phase") == "COMPLETE":
            domain, _, _ = scheduler(root)
            if not scheduler_loaded(domain):
                raise ValueError("RECOVERY_TIMER_MANUALLY_STOPPED")
            return
    body = Path(__file__).read_bytes()
    release = hashlib.sha256(body).hexdigest()
    script = root / ("recovery-" + release + ".py")
    if script.exists() and script.read_bytes() != body or script.is_symlink():
        raise ValueError("RECOVERY_RELEASE_CHANGED")
    if not script.exists():
        atomic(script, body)
    label = scheduler_label(root)
    directory = Path.home() / ".config/systemd/user" if system == "Linux" else root
    directory.mkdir(parents=True, exist_ok=True)
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ValueError("RECOVERY_TIMER_OWNER_INVALID")
    settings = scheduler_settings(root, script)
    if previous is None and any(
        (directory / name).exists() or (directory / name).is_symlink()
        for name in settings
    ):
        raise ValueError("TIMER_ALREADY_EXISTS")
    if system == "Windows" and previous is None and task_exists(label):
        raise ValueError("TIMER_ALREADY_EXISTS")
    if system == "Linux" and previous is None:
        for suffix in ("service", "timer"):
            manager = execute(
                [
                    "systemctl",
                    "--user",
                    "show",
                    label + "." + suffix,
                    "--property=LoadState,FragmentPath",
                ]
            ).decode()
            fields = dict(
                line.split("=", 1) for line in manager.splitlines() if "=" in line
            )
            if fields != {"LoadState": "not-found", "FragmentPath": ""}:
                raise ValueError("TIMER_ALREADY_EXISTS")
    value = {
        "schema_version": 1,
        "system": system,
        "label": label,
        "release": release,
        "directory": str(directory),
        "files": {
            name: hashlib.sha256(raw).hexdigest() for name, raw in settings.items()
        },
        "phase": "PREPARED",
    }
    if previous is not None:
        if any(
            previous.get(k) != value[k] for k in value if k != "phase"
        ) or previous.get("phase") not in {
            "PREPARED",
            "REGISTER_INTENT",
            "REGISTERED",
            "ENABLE_INTENT",
        }:
            raise ValueError("RECOVERY_PREVIOUS_INSTALL_REQUIRES_REVIEW")
        value = previous
    else:
        atomic(marker, json.dumps(value, sort_keys=True).encode())
    for name, raw in settings.items():
        path = directory / name
        if path.is_symlink() or path.exists() and path.read_bytes() != raw:
            raise ValueError("RECOVERY_TIMER_CHANGED")
        if not path.exists():
            atomic(path, raw)
    if system == "Linux":
        execute(["systemctl", "--user", "daemon-reload"])
        execute(["systemctl", "--user", "enable", label + ".timer"])
    else:
        xml = settings[label + ".xml"]
        if not task_exists(label):
            if value["phase"] not in {"PREPARED", "REGISTER_INTENT"}:
                raise ValueError("RECOVERY_TIMER_DISAPPEARED")
            value["phase"] = "REGISTER_INTENT"
            atomic(marker, json.dumps(value, sort_keys=True).encode())
            powershell(
                "$ErrorActionPreference='Stop'; Register-ScheduledTask -TaskPath '\\AELEV\\' -TaskName "
                + ps_literal(label)
                + " -Xml "
                + ps_literal(xml.decode())
                + " -ErrorAction Stop | Out-Null"
            )
        elif value["phase"] == "PREPARED":
            raise ValueError("RECOVERY_TIMER_REGISTRATION_UNCONFIRMED")
        exported = task_export(label)
        # Enabled alone may differ during our own pending activation.
        verify_task(xml, exported)
        if (
            "registered_digest" in value
            and task_digest(exported) != value["registered_digest"]
        ):
            raise ValueError("RECOVERY_TIMER_MANAGER_CHANGED")
        value["registered_digest"] = task_digest(exported)
    value["phase"] = "ENABLE_INTENT"
    atomic(marker, json.dumps(value, sort_keys=True).encode())
    domain = system + ":" + label
    if not scheduler_loaded(domain):
        scheduler_switch(domain, enabled=True)
    value["phase"] = "COMPLETE"
    atomic(marker, json.dumps(value, sort_keys=True).encode())
    scheduler(root)


def install(root: Path) -> None:
    """Install an immutable user LaunchAgent; no sudo or runtime socket mount."""
    if platform.system() != "Darwin":
        install_scheduler(root)
        return
    source = Path(__file__).read_bytes()
    release = hashlib.sha256(source).hexdigest()
    script = root / ("recovery-" + release + ".py")
    atomic(script, source)
    label = "com.aelev.recovery." + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / (label + ".plist")
    if plist.exists():
        raise ValueError("TIMER_ALREADY_EXISTS")
    atomic(
        plist,
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": [
                    sys.executable,
                    str(script),
                    "watch",
                    "--state",
                    str(root),
                ],
                "RunAtLoad": True,
                "StartInterval": 15,
                "ProcessType": "Background",
                "EnvironmentVariables": {
                    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
                },
                "StandardOutPath": "/dev/null",
                "StandardErrorPath": "/dev/null",
            }
        ),
    )
    execute(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])


def timer_loaded(domain: str) -> bool:
    """Only an exact absent-service response proves this timer is not loaded."""
    result = subprocess.run(
        ["launchctl", "print", domain], capture_output=True, timeout=10, check=False
    )
    if result.returncode:
        if b"Could not find service" not in result.stderr:
            raise ValueError("RECOVERY_TIMER_UNCONFIRMED")
        return False
    return True


def upgrade(root: Path) -> dict[str, Any]:
    """Update only the existing user timer, never reset or rewrite its journal."""
    if platform.system() != "Darwin":
        raise ValueError("USE_PLATFORM_TIMER_FOR_ONCE")
    label = "com.aelev.recovery." + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    plist = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
    if plist.is_symlink() or plist.stat().st_uid != os.getuid():
        raise ValueError("RECOVERY_TIMER_OWNER_INVALID")
    raw = plist.read_bytes()
    if len(raw) > 16384:
        raise ValueError("RECOVERY_TIMER_INVALID")
    settings = plistlib.loads(raw)
    args = settings.get("ProgramArguments", [])
    if (
        settings.get("Label") != label
        or len(args) != 5
        or args[2] not in {"once", "watch"}
        or args[3:] != ["--state", str(root)]
        or Path(args[1]).parent != root
        or re.fullmatch(r"recovery-[a-f0-9]{64}\.py", Path(args[1]).name) is None
    ):
        raise ValueError("RECOVERY_TIMER_INVALID")
    old_script = Path(args[1])
    if (
        old_script.is_symlink()
        or old_script.stat().st_uid != os.getuid()
        or hashlib.sha256(old_script.read_bytes()).hexdigest()
        != old_script.stem.removeprefix("recovery-")
    ):
        raise ValueError("RECOVERY_TIMER_SCRIPT_CHANGED")
    body = Path(__file__).read_bytes()
    release = hashlib.sha256(body).hexdigest()
    script = root / ("recovery-" + release + ".py")
    domain = f"gui/{os.getuid()}/{label}"
    marker = root / "upgrade-intent.json"
    descriptor = os.open(
        root / ".container-operation.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "r+") as window:
        lock(window)
        maintenance = root / "container-maintenance.json"
        if (
            maintenance.exists()
            and json.loads(maintenance.read_text()).get("phase") != "COMPLETE"
        ):
            raise ValueError("RECOVERY_CONTAINER_MAINTENANCE_PENDING")
        current: dict[str, Any] = json.loads((root / "state.json").read_text())
        if (
            docker(current, "info", "--format", "{{.ID}}").decode().strip()
            != current["daemon"]
        ):
            raise ValueError("DAEMON_CHANGED")
        previous = None
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink() or marker.stat().st_size > 16384:
                raise ValueError("RECOVERY_UPGRADE_INTENT_INVALID")
            previous = json.loads(marker.read_text())
            if (
                previous.get("release") != release
                and previous.get("phase") != "COMPLETE"
            ):
                raise ValueError("RECOVERY_PREVIOUS_UPGRADE_REQUIRES_REVIEW")
            if (
                previous.get("daemon") != current["daemon"]
                or previous.get("label") != label
            ):
                raise ValueError("RECOVERY_UPGRADE_SCOPE_CHANGED")
        if (
            previous is not None
            and previous.get("phase") == "COMPLETE"
            and old_script == script
            and not timer_loaded(domain)
        ):
            raise ValueError("RECOVERY_TIMER_MANUALLY_STOPPED")
        if (
            previous is not None
            and previous.get("release") == release
            and hashlib.sha256(raw).hexdigest()
            not in {
                previous["previous_plist_digest"],
                previous.get("configured_plist_digest"),
            }
        ):
            raise ValueError("RECOVERY_TIMER_CHANGED_DURING_UPGRADE")
        if old_script == script and timer_loaded(domain):
            if previous is not None and previous.get("release") == release:
                previous["phase"] = "COMPLETE"
                atomic(marker, json.dumps(previous, sort_keys=True).encode())
            return {
                "code": "RECOVERY_TIMER_CURRENT",
                "release": release,
                "label": label,
            }
        if previous is not None and previous.get("release") == release:
            intent = previous
            if hashlib.sha256(raw).hexdigest() not in {
                intent["previous_plist_digest"],
                intent.get("configured_plist_digest"),
            }:
                raise ValueError("RECOVERY_TIMER_CHANGED_DURING_UPGRADE")
        else:
            if not timer_loaded(domain):
                raise ValueError("RECOVERY_TIMER_MANUALLY_STOPPED")
            history = root / "launchagent-history"
            if history.is_symlink():
                raise ValueError("RECOVERY_UPGRADE_HISTORY_INVALID")
            history.mkdir(mode=0o700, exist_ok=True)
            if previous is not None:
                destination = history / (previous["release"] + ".json")
                payload = json.dumps(previous, sort_keys=True).encode()
                if destination.exists() and destination.read_bytes() != payload:
                    raise ValueError("RECOVERY_UPGRADE_HISTORY_CHANGED")
                if not destination.exists():
                    atomic(destination, payload)
            digest = hashlib.sha256(raw).hexdigest()
            destination = history / (digest + ".plist")
            if destination.exists() and destination.read_bytes() != raw:
                raise ValueError("RECOVERY_UPGRADE_HISTORY_CHANGED")
            if not destination.exists():
                atomic(destination, raw)
            intent = {
                "schema_version": 1,
                "daemon": current["daemon"],
                "label": label,
                "release": release,
                "previous_plist_digest": digest,
                "phase": "PREPARED",
            }
            atomic(marker, json.dumps(intent, sort_keys=True).encode())
        descriptor = os.open(
            root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "r+") as barrier:
            deadline = time.monotonic() + 60
            while True:
                try:
                    lock(barrier)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError("RECOVERY_HOST_LOCK_DEADLINE") from None
                    time.sleep(0.05)
            # Drain the old tick before bootout. Killing an active controller
            # could itself turn an in-flight Docker command into a lost ACK.
            if timer_loaded(domain):
                execute(["launchctl", "bootout", domain])
            stopping = time.monotonic() + 10
            while timer_loaded(domain):
                if time.monotonic() >= stopping:
                    raise ValueError("RECOVERY_TIMER_STOP_UNCONFIRMED")
                time.sleep(0.05)
            frozen = hashlib.sha256((root / "state.json").read_bytes()).hexdigest()
            if script.exists() or script.is_symlink():
                if script.is_symlink() or script.read_bytes() != body:
                    raise ValueError("RECOVERY_RELEASE_CHANGED")
            else:
                atomic(script, body)
            settings["ProgramArguments"] = [
                args[0],
                str(script),
                "watch",
                "--state",
                str(root),
            ]
            configured = plistlib.dumps(settings)
            intent.update(
                phase="CONFIGURE_INTENT",
                configured_plist_digest=hashlib.sha256(configured).hexdigest(),
                state_digest=frozen,
            )
            atomic(marker, json.dumps(intent, sort_keys=True).encode())
            atomic(plist, configured)
            if hashlib.sha256((root / "state.json").read_bytes()).hexdigest() != frozen:
                raise ValueError("RECOVERY_JOURNAL_CHANGED_DURING_UPGRADE")
            intent["phase"] = "CONFIGURED"
            atomic(marker, json.dumps(intent, sort_keys=True).encode())
        if not timer_loaded(domain):
            execute(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        if not timer_loaded(domain):
            raise ValueError("RECOVERY_TIMER_RESTORE_UNCONFIRMED")
        intent["phase"] = "COMPLETE"
        atomic(marker, json.dumps(intent, sort_keys=True).encode())
        return {
            "code": "RECOVERY_TIMER_UPGRADED",
            "release": release,
            "label": label,
            "journal_preserved": True,
        }


def suspend(root: Path, name: str) -> None:
    """Preserve the target and budget under the caller's existing operator lock."""
    state = json.loads((root / "state.json").read_text())
    target = state["targets"][name]
    if "cancel_suspend" in target:
        state.setdefault("events", []).append(
            {
                "at": time.time(),
                "target": name,
                "code": "CANCEL_SUSPEND_SUPERSEDED",
                "receipt": target.pop("cancel_suspend"),
            }
        )
    target.update(
        blocked=True, code="OPERATOR_SUSPENDED", suspension_id=os.urandom(32).hex()
    )
    commit(root, state)


def suspension_proof(target: dict[str, Any]) -> str:
    """Bind an operator cancellation to this suspension, not a later hold."""
    scope = {
        key: target.get(key)
        for key in ("expected", "attempts", "native_count", "guard", "suspension_id")
    }
    if "automatic" in target:
        scope["automatic"] = target["automatic"]
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def cancel_suspend(root: Path, name: str, identity: str, proof: str) -> None:
    """Cancel one exact healthy hold without starting or resetting anything."""
    if any(re.fullmatch(r"[a-f0-9]{64}", value) is None for value in (identity, proof)):
        raise ValueError("EXACT_SUSPENSION_PROOF_REQUIRED")
    state = json.loads((root / "state.json").read_text())
    if docker(state, "info", "--format", "{{.ID}}").decode().strip() != state["daemon"]:
        raise ValueError("DAEMON_CHANGED")
    target = state["targets"][name]
    intent = target.get("cancel_suspend")
    if (
        target["expected"]["id"] != identity
        or "restart_intent" in target
        or "intent" in target.get("automatic", {})
        or target.get("replacement_policy_pending")
        or (
            intent is None
            and (
                not target["blocked"]
                or target["code"] != "OPERATOR_SUSPENDED"
                or suspension_proof(target) != proof
            )
        )
        or (intent is not None and intent.get("proof") != proof)
    ):
        raise ValueError("SUSPENSION_CHANGED_OR_RESTART_PENDING")

    def checked() -> dict[str, Any]:
        row = inspect(state, name)
        if (
            fingerprint(row) != target["expected"]
            or not row["State"]["Running"]
            or row["State"].get("Health", {}).get("Status") != "healthy"
            or bad_probe(row, time.time()) is not None
            or type(row["RestartCount"]) is not int
            or row["RestartCount"] < target["native_count"]
        ):
            raise ValueError("SUSPENSION_TARGET_NOT_HEALTHY_OR_CHANGED")
        return row

    row = checked()
    guard = target["guard"]
    if guard is not None:
        path = Path(guard["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != guard["sha256"]:
            raise ValueError("GUARD_CHANGED")
        answer = json.loads(execute([sys.executable, str(path), identity], timeout=50))
        if answer.get("allowed") is not True:
            raise ValueError("OWNER_GUARD_BLOCKED")
        row = checked()
    if intent is None:
        account_native(state, name, target, row, time.time())
        remaining = 0 if "automatic" in target else max(0, LIMIT - target["attempts"])
        intent = {
            "proof": proof,
            "phase": "PREPARED",
            "remaining": remaining,
            "native_count": row["RestartCount"],
            "started_at": started_at(row),
            "previous_policy": row["HostConfig"]["RestartPolicy"],
        }
        target.update(
            cancel_suspend=intent, blocked=True, code="CANCEL_SUSPEND_PENDING"
        )
        commit(root, state)
    remaining = intent["remaining"]
    desired = {
        "Name": "on-failure" if remaining else "no",
        "MaximumRetryCount": remaining,
    }
    if (
        row["RestartCount"] != intent["native_count"]
        or started_at(row) != intent["started_at"]
        or (0 if "automatic" in target else max(0, LIMIT - target["attempts"]))
        != remaining
    ):
        raise ValueError("SUSPENSION_PROCESS_OR_BUDGET_CHANGED")
    if intent["phase"] == "COMPLETE":
        if row["HostConfig"]["RestartPolicy"] != desired or (
            target["blocked"] is not (remaining == 0 and "automatic" not in target)
        ):
            raise ValueError("COMPLETED_CANCELLATION_CHANGED")
        return
    if intent["phase"] not in {"PREPARED", "DISPATCH_INTENT"}:
        raise ValueError("CANCEL_SUSPEND_PHASE_INVALID")
    if row["HostConfig"]["RestartPolicy"] != desired:
        if intent["phase"] != "PREPARED":
            raise ValueError("CANCEL_SUSPEND_RESULT_UNCONFIRMED")
        if row["HostConfig"]["RestartPolicy"] != intent["previous_policy"]:
            raise ValueError("SUSPENSION_POLICY_CHANGED")
        row = checked()
        if (
            row["RestartCount"] != intent["native_count"]
            or started_at(row) != intent["started_at"]
            or row["HostConfig"]["RestartPolicy"] != intent["previous_policy"]
        ):
            raise ValueError("SUSPENSION_CHANGED_BEFORE_ACTION")
        intent["phase"] = "DISPATCH_INTENT"
        commit(root, state)
        docker(
            state,
            "update",
            "--restart",
            f"on-failure:{remaining}" if remaining else "no",
            identity,
        )
    row = checked()
    if (
        row["HostConfig"]["RestartPolicy"] != desired
        or row["RestartCount"] != intent["native_count"]
        or started_at(row) != intent["started_at"]
    ):
        raise ValueError("CANCEL_SUSPEND_RESULT_UNCONFIRMED")
    intent["phase"] = "COMPLETE"
    target.update(
        policy_limit=remaining,
        blocked=not remaining and "automatic" not in target,
        bad_since=None,
        code=(
            "SUSPENSION_CANCELLED"
            if remaining or "automatic" in target
            else "CIRCUIT_OPEN"
        ),
    )
    state.setdefault("events", []).append(
        {
            "at": time.time(),
            "target": name,
            "code": "SUSPENSION_CANCELLED",
            "proof": proof,
            "attempts": target["attempts"],
            "remaining": remaining,
        }
    )
    commit(root, state)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "enroll",
            "adopt-stopped",
            "once",
            "watch",
            "status",
            "reset",
            "suspend",
            "cancel-suspend",
            "install",
            "replace",
            "upgrade",
            "timer-status",
            "auto-enable",
            "import-history",
            "release-replacement",
        ),
    )
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--container")
    parser.add_argument("--guard", type=Path)
    parser.add_argument("--previous-id")
    parser.add_argument("--expected-id")
    parser.add_argument("--suspension-proof")
    parser.add_argument("--history", type=Path)
    parser.add_argument("--history-sha256")
    parser.add_argument("--replacement-proof")
    parser.add_argument("--previous-native-count", type=int)
    parser.add_argument("--image")
    parser.add_argument("--preserve-block", action="store_true")
    args = parser.parse_args()
    if args.preserve_block and args.action != "replace":
        raise ValueError("REPLACEMENT_BLOCK_MODE_INVALID")
    root = args.state.resolve()
    existed = root.exists()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    private(root, create=not existed)
    if args.action == "status":
        # The report is replaced atomically; readers need not wait for Docker I/O.
        sys.stdout.write((root / "report.json").read_text() + "\n")
        return
    if args.action == "timer-status":
        sys.stdout.write(json.dumps(scheduler_status(root), sort_keys=True) + "\n")
        return
    if args.action == "upgrade":
        sys.stdout.write(json.dumps(upgrade(root), sort_keys=True) + "\n")
        return
    if args.action == "watch":
        watch(root)
        return
    if args.action == "once":
        for name in ("scheduler.json", "container-maintenance.json"):
            pending = root / name
            if (
                pending.exists()
                and json.loads(pending.read_bytes()).get("phase") != "COMPLETE"
            ):
                return  # An unconfirmed operator owns this journal, not the timer.
    with (root / ".lock").open("a") as barrier:
        lock(barrier, timeout=120)
        if args.action == "enroll":
            if not args.container:
                raise ValueError("EXACT_CONTAINER_REQUIRED")
            enroll(root, args.container, args.guard)
        elif args.action == "import-history":
            if not args.history or not args.history_sha256 or args.guard is not None:
                raise ValueError("HISTORY_RECEIPT_REQUIRED")
            import_history(root, args.history, args.history_sha256)
        elif args.action == "release-replacement":
            if not args.container or not args.expected_id or not args.replacement_proof:
                raise ValueError("REPLACEMENT_RELEASE_PROOF_REQUIRED")
            release_replacement(
                root, args.container, args.expected_id, args.replacement_proof
            )
        elif args.action == "auto-enable":
            if not args.container:
                raise ValueError("EXACT_CONTAINER_REQUIRED")
            automatic_enable(root, args.container)
        elif args.action == "adopt-stopped":
            if (
                not args.container
                or not args.expected_id
                or args.previous_native_count is None
                or not args.image
                or args.guard is not None
            ):
                raise ValueError("EXHAUSTED_PREDECESSOR_PROOF_REQUIRED")
            adopt_stopped(
                root,
                args.container,
                args.expected_id,
                args.previous_native_count,
                args.image,
            )
        elif args.action == "once":
            try:
                once(root)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                atomic(
                    root / "controller-error.json",
                    json.dumps(
                        {"code": "CONTROLLER_FAILED", "at": time.time()}
                    ).encode(),
                )
                raise SystemExit(1) from None
        elif args.action == "install":
            install(root)
        elif args.action == "suspend":
            suspend(root, args.container)
        elif args.action == "cancel-suspend":
            if not args.container or not args.expected_id or not args.suspension_proof:
                raise ValueError("EXACT_SUSPENSION_PROOF_REQUIRED")
            cancel_suspend(
                root, args.container, args.expected_id, args.suspension_proof
            )
        elif args.action == "replace":
            if (
                not args.container
                or not args.previous_id
                or args.previous_native_count is None
                or not args.image
            ):
                raise ValueError("EXACT_REPLACEMENT_PROOF_REQUIRED")
            replace(
                root,
                args.container,
                args.previous_id,
                args.previous_native_count,
                args.image,
                preserve_block=args.preserve_block,
            )
        else:
            if not args.container:
                raise ValueError("EXACT_CONTAINER_REQUIRED")
            state = json.loads((root / "state.json").read_text())
            target = state["targets"][args.container]
            if "automatic" in target:
                raise ValueError("AUTOMATIC_HISTORY_CANNOT_BE_RESET")
            state["targets"].pop(args.container)
            state.setdefault("events", []).append(
                {
                    "at": time.time(),
                    "target": args.container,
                    "code": "OPERATOR_RESET",
                    "previous": target,
                }
            )
            commit(root, state)
            guard = Path(target["guard"]["path"]) if target["guard"] else None
            enroll(root, args.container, guard)


if __name__ == "__main__":
    main()
