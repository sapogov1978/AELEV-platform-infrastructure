"""Host-held maintenance window for the existing recovery CLI container."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import recovery
from recovery_cli import LIMIT, apply, preflight, request, require, result, state


def timer(root: Path) -> tuple[str, Path, str]:
    """Accept only the exact existing timer produced by recovery.install."""
    if platform.system() != "Darwin":
        return recovery.scheduler(root)
    label = "com.aelev.recovery." + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    path = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
    require(
        not path.is_symlink() and path.stat().st_uid == os.getuid(),
        "RECOVERY_TIMER_OWNER_INVALID",
    )
    raw = path.read_bytes()
    require(len(raw) <= 16384, "RECOVERY_TIMER_INVALID")
    value = plistlib.loads(raw)
    arguments = value.get("ProgramArguments", [])
    require(
        value.get("Label") == label
        and len(arguments) == 5
        and arguments[2] in {"once", "watch"}
        and arguments[3:] == ["--state", str(root)]
        and isinstance(arguments[1], str)
        and Path(arguments[1]).parent == root
        and re.fullmatch(r"recovery-[a-f0-9]{64}\.py", Path(arguments[1]).name)
        is not None,
        "RECOVERY_TIMER_INVALID",
    )
    script = Path(arguments[1])
    require(
        not script.is_symlink()
        and script.stat().st_uid == os.getuid()
        and hashlib.sha256(script.read_bytes()).hexdigest()
        == script.stem.removeprefix("recovery-"),
        "RECOVERY_TIMER_SCRIPT_CHANGED",
    )
    return f"gui/{os.getuid()}/{label}", path, hashlib.sha256(raw).hexdigest()


def loaded(domain: str) -> bool:
    if domain.startswith(("Linux:", "Windows:")):
        return recovery.scheduler_loaded(domain)
    value = subprocess.run(
        ["launchctl", "print", domain], capture_output=True, timeout=10, check=False
    )
    if value.returncode:
        # Only launchd's exact absent-service diagnostic authorizes absence.
        require(b"Could not find service" in value.stderr, "RECOVERY_TIMER_UNCONFIRMED")
        return False
    return True


def switch(domain: str, path: Path, *, enabled: bool) -> None:
    if domain.startswith(("Linux:", "Windows:")):
        recovery.scheduler_switch(domain, enabled=enabled)
    elif enabled:
        recovery.execute(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
    else:
        recovery.execute(["launchctl", "bootout", domain])


def save(root: Path, value: dict[str, Any]) -> None:
    recovery.atomic(
        root / "container-maintenance.json", json.dumps(value, sort_keys=True).encode()
    )


def socket_group(current: dict[str, Any], image: str) -> str:
    """Read VM socket metadata without opening the daemon or journal in root."""
    endpoint = current["endpoint"]
    require(
        isinstance(endpoint, str)
        and endpoint.startswith("unix:///")
        and "," not in endpoint,
        "RECOVERY_LOCAL_SOCKET_REQUIRED",
    )
    raw = recovery.docker(
        current,
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--memory",
        "32m",
        "--pids-limit",
        "16",
        "--mount",
        f"type=bind,source={endpoint.removeprefix('unix://')},target=/var/run/docker.sock,readonly",
        "--entrypoint",
        "python",
        image,
        "-S",
        "-c",
        "import os; print(os.stat('/var/run/docker.sock').st_gid)",
    )
    value = raw.decode().strip()
    require(
        re.fullmatch(r"[0-9]{1,10}", value) is not None and int(value) < 2**31,
        "RECOVERY_SOCKET_GROUP_UNCONFIRMED",
    )
    return value


def inspect_container(current: dict[str, Any], name: str) -> dict[str, Any] | None:
    identifiers = (
        recovery.docker(
            current,
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            "name=^/" + name + "$",
            "--format",
            "{{.ID}}",
        )
        .decode()
        .splitlines()
    )
    require(len(identifiers) <= 1, "RECOVERY_OPERATOR_AMBIGUOUS")
    return recovery.inspect(current, identifiers[0]) if identifiers else None


def completed_receipt(current: dict[str, Any], marker: dict[str, Any]) -> bool:
    """Accept a removed one-shot helper only after its durable result committed."""
    result = marker.get("result")
    targets = result.get("targets") if isinstance(result, dict) else None
    return bool(
        marker.get("phase") == "COMPLETE"
        and isinstance(result, dict)
        and result.get("schema_version") == 1
        and result.get("daemon_id") == current["daemon"]
        and isinstance(targets, dict)
        and 1 <= len(targets) <= 64
        and all(
            isinstance(name, str)
            and isinstance(receipt, dict)
            and re.fullmatch(r"[a-f0-9]{64}", receipt.get("target_digest", ""))
            is not None
            for name, receipt in targets.items()
        )
    )


def operator_failure(current: dict[str, Any], identity: str) -> dict[str, Any]:
    """Read one bounded JSON error without exposing arbitrary helper output."""
    process = subprocess.run(
        [current["docker"], "--host", current["endpoint"], "logs", identity],
        capture_output=True,
        timeout=10,
        check=False,
    )
    payload = process.stdout + process.stderr
    require(
        process.returncode == 0 and 0 < len(payload) <= 4096,
        "RECOVERY_FAILED_OPERATOR_RESULT_CHANGED",
    )
    lines = [line for line in payload.splitlines() if line.strip()]
    require(len(lines) == 1, "RECOVERY_FAILED_OPERATOR_RESULT_CHANGED")
    value: Any = json.loads(lines[0])
    require(isinstance(value, dict), "RECOVERY_FAILED_OPERATOR_RESULT_CHANGED")
    return cast(dict[str, Any], value)


def maintain(root: Path, image: str, raw: bytes) -> dict[str, Any]:
    """Never resume a timer while an unconfirmed container may still write."""
    require(
        re.fullmatch(r"sha256:[a-f0-9]{64}", image) is not None,
        "RECOVERY_PINNED_IMAGE_REQUIRED",
    )
    value = request(raw)
    require(value["action"] != "status", "RECOVERY_MAINTENANCE_WRITE_REQUIRED")
    root = root.resolve(strict=True)
    current = state(root)
    require(
        recovery.docker(current, "info", "--format", "{{.ID}}").decode().strip()
        == value["daemon_id"]
        == current["daemon"],
        "RECOVERY_DAEMON_MISMATCH",
    )
    details = json.loads(recovery.docker(current, "image", "inspect", image))[0]
    require(
        details["Id"] == image
        and details["Config"].get("Labels", {}).get("io.aelev.recovery.operator") == "1"
        and (
            value["action"] != "register"
            or details["Config"].get("Labels", {}).get("io.aelev.recovery.registration")
            == "1"
        ),
        "RECOVERY_OPERATOR_IMAGE_INVALID",
    )
    if value["action"] == "register":
        preflight(current, value, native=True)
    domain, plist, timer_digest = timer(root)
    identifier = hashlib.sha256(
        json.dumps(value, sort_keys=True).encode() + image.encode()
    ).hexdigest()
    marker_path = root / "container-maintenance.json"
    marker = {
        "schema_version": 1,
        "request_digest": identifier,
        "image": image,
        "daemon_id": current["daemon"],
        "timer": domain,
        "timer_digest": timer_digest,
        "container_name": "aelev-recovery-operator-" + identifier[:24],
        "phase": "INTENT",
        "container_id": None,
    }
    with (root / ".container-operation.lock").open("a") as window:
        recovery.lock(window)
        if marker_path.exists():
            require(not marker_path.is_symlink(), "RECOVERY_MAINTENANCE_INVALID")
            previous = json.loads(marker_path.read_text())
            same = all(
                previous.get(k) == marker[k]
                for k in (
                    "schema_version",
                    "request_digest",
                    "image",
                    "daemon_id",
                    "timer",
                    "timer_digest",
                    "container_name",
                )
            )
            if not same:
                require(
                    previous.get("phase") == "COMPLETE" and loaded(domain),
                    "RECOVERY_PREVIOUS_MAINTENANCE_REQUIRES_REVIEW",
                )
                if not domain.startswith(("Linux:", "Windows:")):
                    old = inspect_container(current, previous["container_name"])
                    if old is None:
                        require(
                            completed_receipt(current, previous),
                            "RECOVERY_PREVIOUS_OPERATOR_UNCONFIRMED",
                        )
                    else:
                        require(
                            old["Id"] == previous["container_id"]
                            and not old["State"]["Running"],
                            "RECOVERY_PREVIOUS_OPERATOR_UNCONFIRMED",
                        )
                history = root / "container-maintenance-history"
                require(not history.is_symlink(), "RECOVERY_MAINTENANCE_INVALID")
                history.mkdir(mode=0o700, exist_ok=True)
                destination = history / (previous["request_digest"] + ".json")
                payload = json.dumps(previous, sort_keys=True).encode()
                if destination.exists():
                    require(
                        not destination.is_symlink()
                        and destination.read_bytes() == payload,
                        "RECOVERY_MAINTENANCE_HISTORY_CHANGED",
                    )
                else:
                    recovery.atomic(destination, payload)
                save(root, marker)
            else:
                marker = previous
            if marker["phase"] == "COMPLETE":
                require(loaded(domain), "RECOVERY_TIMER_NOT_RESTORED")
                return cast(dict[str, Any], marker["result"])
        else:
            require(loaded(domain), "RECOVERY_TIMER_NOT_RUNNING")
            save(root, marker)
        # Drain the existing tick before stopping its timer, never kill an
        # active controller halfway through a Docker command.
        with (root / ".lock").open("a") as lock:
            deadline = time.monotonic() + 60
            while True:
                try:
                    recovery.lock(lock)
                    break
                except BlockingIOError:
                    require(time.monotonic() < deadline, "RECOVERY_HOST_LOCK_DEADLINE")
                    time.sleep(0.05)
            if loaded(domain):
                switch(domain, plist, enabled=False)
            stopping = time.monotonic() + 10
            while loaded(domain):
                require(time.monotonic() < stopping, "RECOVERY_TIMER_STOP_UNCONFIRMED")
                time.sleep(0.05)
            marker["phase"] = "TIMER_STOPPED"
            save(root, marker)
            if domain.startswith(("Linux:", "Windows:")):
                # Native OS locks and NTFS ACLs stay on the host. Execute the
                # same pinned public CLI functions under this existing barrier,
                # not a container's unrelated lock/UID namespace.
                preflight(state(root), value, native=True)
                marker["phase"] = "DISPATCH_INTENT"
                save(root, marker)
                apply(root, value)
                answer = result(state(root), value)
                marker.update(phase="VERIFIED", result=answer)
                save(root, marker)
            else:
                answer = container_operation(
                    root, image, value, current, marker, identifier
                )
        # Never bootstrap the timer under its own lock: RunAtLoad may race it.
        require(
            timer(root) == (domain, plist, timer_digest),
            "RECOVERY_TIMER_CHANGED",
        )
        if not loaded(domain):
            switch(domain, plist, enabled=True)
        require(loaded(domain), "RECOVERY_TIMER_RESTORE_UNCONFIRMED")
        marker["phase"] = "COMPLETE"
        save(root, marker)
        return answer


def container_operation(
    root: Path,
    image: str,
    value: dict[str, Any],
    current: dict[str, Any],
    marker: dict[str, Any],
    identifier: str,
) -> dict[str, Any]:
    """macOS keeps its existing container transport and durable reconciliation."""
    row = inspect_container(current, marker["container_name"])
    if row is None:
        require(marker.get("container_id") is None, "RECOVERY_OPERATOR_DISAPPEARED")
        endpoint = current["endpoint"]
        require(
            isinstance(endpoint, str)
            and endpoint.startswith("unix:///")
            and "," not in endpoint
            and "," not in str(root),
            "RECOVERY_LOCAL_SOCKET_REQUIRED",
        )
        command = [
            "create",
            "--name",
            marker["container_name"],
            "--interactive",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "128m",
            "--pids-limit",
            "32",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--group-add",
            socket_group(current, image),
            "--label",
            "io.aelev.recovery.request=" + identifier,
            "--mount",
            f"type=bind,source={root},target=/var/lib/aelev-recovery",
            "--mount",
            f"type=bind,source={endpoint.removeprefix('unix://')},target=/var/run/docker.sock",
            image,
            "--host-timer-quiesced",
        ]
        recovery.docker(current, *command)
        row = inspect_container(current, marker["container_name"])
        require(row is not None, "RECOVERY_OPERATOR_CREATE_UNCONFIRMED")
    require(row is not None, "RECOVERY_OPERATOR_MISSING")
    assert row is not None
    require(
        row["Image"] == image
        and row["Config"].get("Labels", {}).get("io.aelev.recovery.request")
        == identifier
        and marker.get("container_id") in (None, row["Id"]),
        "RECOVERY_OPERATOR_CHANGED",
    )
    marker["container_id"] = row["Id"]
    save(root, marker)
    if row["State"]["Status"] == "created":
        marker["phase"] = "DISPATCH_INTENT"
        save(root, marker)
        process = subprocess.run(
            [
                current["docker"],
                "--host",
                current["endpoint"],
                "start",
                "--attach",
                "--interactive",
                row["Id"],
            ],
            input=json.dumps(value, sort_keys=True).encode() + b"\n",
            capture_output=True,
            check=False,
            timeout=120,
        )
        require(process.returncode == 0, "RECOVERY_OPERATOR_RESULT_UNCONFIRMED")
        row = recovery.inspect(current, row["Id"])
    require(
        not row["State"]["Running"]
        and row["State"]["Status"] == "exited"
        and row["State"]["ExitCode"] == 0
        and not row["State"].get("OOMKilled"),
        "RECOVERY_OPERATOR_RESULT_UNCONFIRMED",
    )
    answer: dict[str, Any] = json.loads(recovery.docker(current, "logs", row["Id"]))
    require(
        answer.get("daemon_id") == current["daemon"]
        and set(answer.get("targets", {}))
        == {
            v["replacement_name"] if value["action"] == "relocate" else v["name"]
            for v in value["targets"]
        },
        "RECOVERY_OPERATOR_RECEIPT_INVALID",
    )
    marker.update(phase="VERIFIED", result=answer)
    save(root, marker)
    return answer


def repair_failed_relocation(
    root: Path, image: str, raw: bytes
) -> dict[str, Any]:
    """Continue one exact pre-write relocation failure without erasing its intent."""
    value = request(raw)
    require(value["action"] == "relocate", "RECOVERY_RELOCATION_REQUEST_REQUIRED")
    root = root.resolve(strict=True)
    current = state(root)
    details = json.loads(recovery.docker(current, "image", "inspect", image))[0]
    require(
        details.get("Id") == image
        and details.get("Config", {})
        .get("Labels", {})
        .get("io.aelev.recovery.operator")
        == "1",
        "RECOVERY_OPERATOR_IMAGE_INVALID",
    )
    marker_path = root / "container-maintenance.json"
    require(marker_path.exists() and not marker_path.is_symlink(), "RECOVERY_MAINTENANCE_INVALID")
    marker = json.loads(marker_path.read_bytes())
    identifier = hashlib.sha256(
        json.dumps(value, sort_keys=True).encode() + marker["image"].encode()
    ).hexdigest()
    domain, plist, timer_digest = timer(root)
    require(
        marker.get("phase") in {"DISPATCH_INTENT", "REPAIR_PREPARED", "VERIFIED"}
        and marker.get("request_digest") == identifier
        and marker.get("daemon_id") == current["daemon"] == value["daemon_id"]
        and marker.get("timer") == domain
        and marker.get("timer_digest") == timer_digest
        and not loaded(domain),
        "RECOVERY_FAILED_RELOCATION_CHANGED",
    )
    helper = inspect_container(current, marker["container_name"])
    require(
        helper is not None
        and helper["Id"] == marker.get("container_id")
        and helper["Image"] == marker["image"]
        and not helper["State"].get("Running")
        and helper["State"].get("Status") == "exited"
        and helper["State"].get("ExitCode") == 1
        and not helper["State"].get("OOMKilled"),
        "RECOVERY_FAILED_OPERATOR_CHANGED",
    )
    failure = operator_failure(current, helper["Id"])
    require(
        failure == {"code": "RELOCATION_UNCONFIRMED"},
        "RECOVERY_FAILED_OPERATOR_RESULT_CHANGED",
    )
    source_digests = {
        name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
        for name in ("recovery.py", "recovery_cli.py", "recovery_host.py")
    }
    repair = marker.get("repair")
    expected_repair = {
        "image": image,
        "failure": failure,
        "source_digests": source_digests,
    }
    if repair is None:
        repair = {
            **expected_repair,
            "phase": "PREPARED",
            "before_state_digest": hashlib.sha256(
                (root / "state.json").read_bytes()
            ).hexdigest(),
        }
        marker.update(phase="REPAIR_PREPARED", repair=repair)
        save(root, marker)
    else:
        require(
            all(repair.get(key) == expected_repair[key] for key in expected_repair)
            and repair.get("phase") in {"PREPARED", "COMPLETE"},
            "RECOVERY_FAILED_RELOCATION_REPAIR_CHANGED",
        )
    with (root / ".container-operation.lock").open("a") as window:
        recovery.lock(window)
        with (root / ".lock").open("a") as barrier:
            recovery.lock(barrier, timeout=120)
            current = state(root)
            preflight(current, value, native=True)
            if repair["phase"] != "COMPLETE":
                for row in value["targets"]:
                    recovery.relocate(
                        root,
                        row["name"],
                        row["expected"]["id"],
                        row["previous_native_count"],
                        row["replacement_name"],
                        row["replacement"],
                        preserve_block=True,
                    )
                answer = result(state(root), value)
                repair.update(
                    phase="COMPLETE",
                    after_state_digest=hashlib.sha256(
                        (root / "state.json").read_bytes()
                    ).hexdigest(),
                )
                marker.update(phase="VERIFIED", result=answer)
                save(root, marker)
            else:
                answer = result(state(root), value)
    require(timer(root) == (domain, plist, timer_digest), "RECOVERY_TIMER_CHANGED")
    if not loaded(domain):
        switch(domain, plist, enabled=True)
    require(loaded(domain), "RECOVERY_TIMER_RESTORE_UNCONFIRMED")
    marker["phase"] = "COMPLETE"
    save(root, marker)
    return answer


def registration_timer(root: Path) -> dict[str, Any]:
    """Install the existing provider task; never re-enable a stopped existing task."""
    if platform.system() == "Darwin":
        label = (
            "com.aelev.recovery." + hashlib.sha256(str(root).encode()).hexdigest()[:16]
        )
        path = Path.home() / "Library/LaunchAgents" / (label + ".plist")
        if not path.exists() and not path.is_symlink():
            recovery.install(root)
        domain, _, _ = timer(root)
        require(loaded(domain), "RECOVERY_TIMER_NOT_RUNNING")
    else:
        # Existing COMPLETE scheduler state is verification-only in this API.
        recovery.install(root)
    return recovery.scheduler_status(root)


def register(root: Path, image: str, endpoint: str, raw: bytes) -> dict[str, Any]:
    """Initialize only a fresh exact local installation, or use the old host barrier."""
    value = request(raw)
    require(value["action"] == "register", "RECOVERY_REGISTRATION_REQUEST_REQUIRED")
    require(recovery.local_endpoint(endpoint), "RECOVERY_LOCAL_SOCKET_REQUIRED")
    require(
        re.fullmatch(r"sha256:[a-f0-9]{64}", image) is not None,
        "RECOVERY_PINNED_IMAGE_REQUIRED",
    )
    binary = shutil.which("docker")
    require(
        binary is not None and Path(binary).is_absolute(), "RECOVERY_DOCKER_REQUIRED"
    )
    require(
        not any(p.is_symlink() or p.is_junction() for p in (root, *root.parents)),
        "RECOVERY_PRIVATE_JOURNAL_REQUIRED",
    )
    existed = root.exists()
    if not existed:
        root.mkdir(mode=0o700)
    recovery.private(root, create=not existed)
    root = root.resolve(strict=True)
    initial = root / "registration-bootstrap.json"
    if (root / "state.json").exists() and not initial.exists():
        current = state(root)
        require(current["endpoint"] == endpoint, "RECOVERY_DAEMON_MISMATCH")
        return maintain(root, image, raw)
    descriptor = os.open(
        root / ".registration-bootstrap.lock",
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "r+") as window:
        recovery.lock(window)
        marker: dict[str, Any] = {
            "schema_version": 1,
            "request_digest": hashlib.sha256(
                json.dumps(value, sort_keys=True).encode()
            ).hexdigest(),
            "daemon_id": value["daemon_id"],
            "endpoint": endpoint,
            "image": image,
            "phase": "INTENT",
        }
        if initial.exists() or initial.is_symlink():
            require(
                not initial.is_symlink() and initial.stat().st_size <= 65536,
                "RECOVERY_REGISTRATION_BOOTSTRAP_INVALID",
            )
            previous = json.loads(initial.read_bytes())
            require(
                all(previous.get(k) == v for k, v in marker.items() if k != "phase")
                and previous.get("phase") in {"INTENT", "INSTALL_INTENT", "COMPLETE"},
                "RECOVERY_REGISTRATION_BOOTSTRAP_CHANGED",
            )
            marker = previous
        else:
            require(
                {p.name for p in root.iterdir()} == {".registration-bootstrap.lock"},
                "RECOVERY_REGISTRATION_UNKNOWN_STATE_PRESERVED",
            )
        current = (
            state(root)
            if (root / "state.json").exists()
            else {
                "schema_version": 1,
                "docker": binary,
                "endpoint": endpoint,
                "daemon": value["daemon_id"],
                "targets": {},
            }
        )
        require(current["endpoint"] == endpoint, "RECOVERY_DAEMON_MISMATCH")
        preflight(current, value, native=True)
        details = json.loads(recovery.docker(current, "image", "inspect", image))[0]
        require(
            details.get("Id") == image
            and details.get("Config", {})
            .get("Labels", {})
            .get("io.aelev.recovery.registration")
            == "1",
            "RECOVERY_REGISTRATION_OPERATOR_UNSUPPORTED",
        )
        if not initial.exists():
            recovery.atomic(initial, json.dumps(marker, sort_keys=True).encode())
        if not (root / "state.json").exists():
            recovery.commit(root, current)
        if marker["phase"] == "COMPLETE":
            return {**result(current, value), "timer": recovery.scheduler_status(root)}
        with (root / ".lock").open("a") as lock:
            recovery.lock(lock)
            current = state(root)
            preflight(current, value, native=True)
            apply(root, value)
        marker["phase"] = "INSTALL_INTENT"
        recovery.atomic(initial, json.dumps(marker, sort_keys=True).encode())
        timer_result = registration_timer(root)
        require(timer_result.get("active") is True, "RECOVERY_TIMER_NOT_RUNNING")
        current = state(root)
        preflight(current, value, native=True)
        marker["phase"] = "COMPLETE"
        recovery.atomic(initial, json.dumps(marker, sort_keys=True).encode())
        return {**result(current, value), "timer": timer_result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--endpoint")
    parser.add_argument("--repair-failed-relocation", action="store_true")
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(LIMIT + 1)
        value = request(raw)
        if args.repair_failed_relocation:
            answer = repair_failed_relocation(args.state, args.image, raw)
        elif value["action"] == "register":
            require(args.endpoint is not None, "RECOVERY_LOCAL_SOCKET_REQUIRED")
            answer = register(args.state, args.image, args.endpoint, raw)
        elif value["action"] == "status":
            # Native ACLs and Docker endpoint remain on the host. This branch
            # must neither inspect/suspend a timer nor create maintenance state.
            current = state(args.state)
            preflight(current, value, native=True)
            answer = result(current, value)
        else:
            answer = maintain(args.state, args.image, raw)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ) as error:
        code = str(error)
        if re.fullmatch(r"RECOVERY_[A-Z0-9_]{1,96}", code) is None:
            code = "RECOVERY_HOST_MAINTENANCE_UNCONFIRMED"
        sys.stderr.write(json.dumps({"code": code}) + "\n")
        raise SystemExit(1) from None
    sys.stdout.write(json.dumps(answer, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
