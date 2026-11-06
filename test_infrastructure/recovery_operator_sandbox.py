"""Explicit isolated Mac proof; never load the working recovery journal."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery
import recovery_host


def prove(image: str, endpoint: str, root: Path) -> dict[str, Any]:
    """Create one disposable target and a separate user timer with no volumes."""
    if root.exists() or not endpoint.startswith("unix:///"):
        raise ValueError("FRESH_ISOLATED_ROOT_REQUIRED")
    root.mkdir(mode=0o700)
    name = "aelev-recovery-proof-" + uuid.uuid4().hex[:12]
    current: dict[str, Any] = {
        "schema_version": 1,
        "docker": shutil.which("docker"),
        "endpoint": endpoint,
        "targets": {},
    }
    daemon = recovery.docker(current, "info", "--format", "{{.ID}}").decode().strip()
    current["daemon"] = daemon
    recovery.commit(root, current)
    timer_label = (
        "com.aelev.recovery." + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    )
    plist = Path.home() / "Library" / "LaunchAgents" / (timer_label + ".plist")
    timer_created = False
    identifiers: list[str] = []

    def create() -> dict[str, Any]:
        identifier = (
            recovery.docker(
                current,
                "run",
                "--detach",
                "--name",
                name,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--memory",
                "64m",
                "--pids-limit",
                "16",
                "--label",
                "com.docker.compose.project=" + name,
                "--label",
                "com.docker.compose.service=proof",
                "--label",
                "com.docker.compose.config-hash=" + "a" * 64,
                "--health-cmd",
                "exit 0",
                "--health-interval",
                "1s",
                "--health-timeout",
                "1s",
                "--entrypoint",
                "/bin/sleep",
                image,
                "900",
            )
            .decode()
            .strip()
        )
        identifiers.append(identifier)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            row = recovery.inspect(current, identifier)
            if row["State"].get("Health", {}).get("Status") == "healthy":
                return row
            time.sleep(0.2)
        raise ValueError("PROOF_HEALTH_DEADLINE")

    try:
        old = create()
        recovery.enroll(root, name, None)
        saved = json.loads((root / "state.json").read_text())
        # Deliberate fixture-only lifetime history, not a production reset.
        saved["targets"][name].update(attempts=2, policy_limit=1)
        saved["events"] = [{"code": "ISOLATED_TEST_HISTORY", "attempts": 2}]
        recovery.commit(root, saved)
        recovery.docker(current, "update", "--restart", "on-failure:1", old["Id"])
        recovery.docker(current, "stop", "--timeout", "2", old["Id"])
        # Preserve the predecessor and avoid waiting for Docker layer removal.
        recovery.docker(current, "rename", old["Id"], name + "-predecessor")
        new = create()
        request = {
            "schema_version": 1,
            "daemon_id": daemon,
            "action": "replace",
            "targets": [
                {
                    "name": name,
                    "expected": recovery.fingerprint(old),
                    "previous_native_count": 0,
                    "replacement_image": image,
                }
            ],
        }
        if plist.exists():
            raise ValueError("PROOF_TIMER_ALREADY_EXISTS")
        recovery.install(root)
        timer_created = True
        answer = recovery_host.maintain(root, image, json.dumps(request).encode())
        first = json.loads((root / "state.json").read_text())
        again = recovery_host.maintain(root, image, json.dumps(request).encode())
        assert again == answer
        assert answer["targets"][name]["attempts"] == 2
        assert answer["targets"][name]["policy_limit"] == 1
        assert answer["targets"][name]["expected"]["id"] == new["Id"]
        assert (
            first["docker"] == saved["docker"]
            and first["endpoint"] == saved["endpoint"]
        )
        after = recovery.inspect(current, new["Id"])
        assert after["RestartCount"] == 0
        assert after["HostConfig"]["RestartPolicy"] == {
            "Name": "on-failure",
            "MaximumRetryCount": 1,
        }
        return {
            "image": image,
            "daemon": daemon,
            "target": name,
            "previous_id": old["Id"],
            "replacement_id": new["Id"],
            "attempts": 2,
            "remaining": 1,
            "timer_restored": True,
            "repeat_dispatches": 0,
            "root": str(root),
        }
    finally:
        marker = root / "container-maintenance.json"
        if marker.exists():
            operation = json.loads(marker.read_text())
            helper_id = operation.get("container_id")
            if helper_id:
                helper = recovery.inspect(current, helper_id)
                if helper["State"]["Running"]:
                    # Do not change targets or erase the timer while a prior
                    # operator may still be writing. Resume this exact fixture.
                    raise ValueError("PROOF_OPERATOR_STILL_RUNNING")
        if timer_created:
            domain = f"gui/{os.getuid()}/{timer_label}"
            if recovery_host.loaded(domain):
                recovery.execute(["launchctl", "bootout", domain])
            if recovery_host.loaded(domain):
                raise ValueError("PROOF_TIMER_STOP_UNCONFIRMED")
            # Only the exact timer created above, never the real controller.
            plist.unlink()
        for identifier in identifiers:
            listing = (
                recovery.docker(
                    current,
                    "ps",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    "id=" + identifier,
                    "--format",
                    "{{.ID}}",
                )
                .decode()
                .splitlines()
            )
            if listing == [identifier]:
                row = recovery.inspect(current, identifier)
                if (
                    row["Name"] not in {"/" + name, "/" + name + "-predecessor"}
                    or row["Image"] != image
                    or row["Mounts"]
                ):
                    raise ValueError("PROOF_CLEANUP_SCOPE_CHANGED")
                if row["State"]["Running"]:
                    recovery.docker(current, "stop", "--timeout", "2", identifier)
                recovery.docker(current, "rm", identifier)


def resume(image: str, root: Path) -> dict[str, Any]:
    """Reconcile only the fixture whose create acknowledgement was lost."""
    current = json.loads((root / "state.json").read_text())
    if len(current["targets"]) != 1:
        raise ValueError("EXACT_PROOF_SCOPE_REQUIRED")
    name, target = next(iter(current["targets"].items()))
    if not name.startswith("aelev-recovery-proof-") or target["attempts"] != 2:
        raise ValueError("EXACT_PROOF_SCOPE_REQUIRED")
    row = recovery.inspect(current, name)
    if (
        row["Image"] != image
        or row["Mounts"]
        or row["Config"]["Labels"]["com.docker.compose.project"] != name
    ):
        raise ValueError("EXACT_PROOF_SCOPE_REQUIRED")
    value = {
        "schema_version": 1,
        "daemon_id": current["daemon"],
        "action": "replace",
        "targets": [
            {
                "name": name,
                "expected": target["expected"],
                "previous_native_count": target["native_count"],
                "replacement_image": image,
            }
        ],
    }
    label = "com.aelev.recovery." + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    plist = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
    if not plist.exists():
        recovery.install(root)
    answer = recovery_host.maintain(root, image, json.dumps(value).encode())
    assert recovery_host.maintain(root, image, json.dumps(value).encode()) == answer
    assert answer["targets"][name]["attempts"] == 2
    assert answer["targets"][name]["policy_limit"] == 1
    domain = f"gui/{os.getuid()}/{label}"
    assert recovery_host.loaded(domain)
    recovery.execute(["launchctl", "bootout", domain])
    assert not recovery_host.loaded(domain)
    plist.unlink()
    recovery.docker(current, "stop", "--timeout", "2", row["Id"])
    recovery.docker(current, "rm", row["Id"])
    return {
        "image": image,
        "previous_id": target["expected"]["id"],
        "replacement_id": row["Id"],
        "attempts": 2,
        "remaining": 1,
        "timer_restored": True,
        "repeat_dispatches": 0,
        "root": str(root),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    original = recovery.execute

    def observed(command: list[str], *, timeout: float = 20) -> bytes:
        # This is an observation allowance for the isolated cold-start proof,
        # not a change to the production controller's Docker deadlines.
        return original(command, timeout=max(timeout, 120))

    recovery.execute = observed
    result = (
        resume(args.image, args.root)
        if args.resume
        else prove(args.image, args.endpoint, args.root)
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
