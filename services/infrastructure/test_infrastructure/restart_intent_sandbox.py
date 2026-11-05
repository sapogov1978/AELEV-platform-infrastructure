"""Isolated Docker/LaunchAgent fault proof; never open the working journal."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
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


def prove(root: Path, image: str, endpoint: str, predecessor: Path) -> dict[str, Any]:
    if root.exists() or not endpoint.startswith("unix:///"):
        raise ValueError("FRESH_PRIVATE_PROOF_ROOT_REQUIRED")
    source = predecessor.read_bytes()
    if predecessor.name != "recovery-" + hashlib.sha256(source).hexdigest() + ".py":
        raise ValueError("IMMUTABLE_PREDECESSOR_REQUIRED")
    root.mkdir(mode=0o700)
    transport: dict[str, Any] = {
        "schema_version": 1,
        "docker": shutil.which("docker"),
        "endpoint": endpoint,
        "targets": {},
    }
    transport["daemon"] = (
        recovery.docker(transport, "info", "--format", "{{.ID}}").decode().strip()
    )
    results: list[dict[str, Any]] = []
    original = recovery.docker
    for boundary in ("disable", "restart", "restore"):
        journal = root / boundary
        journal.mkdir(mode=0o700)
        recovery.commit(journal, {**transport, "targets": {}})
        name = "aelev-restart-intent-proof-" + uuid.uuid4().hex[:12]
        recovery.atomic(
            journal / "resource.json",
            json.dumps({"name": name, "image": image}).encode(),
        )
        identity = (
            original(
                transport,
                "create",
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
                "exit 1",
                "--health-interval",
                "1s",
                "--health-timeout",
                "1s",
                "--health-retries",
                "1",
                "--entrypoint",
                "/bin/sleep",
                image,
                "900",
            )
            .decode()
            .strip()
        )
        recovery.atomic(
            journal / "resource.json",
            json.dumps({"name": name, "image": image, "id": identity}).encode(),
        )
        original(transport, "start", identity)
        deadline = time.monotonic() + 90
        while True:
            row = recovery.inspect(transport, identity)
            if row["State"].get("Health", {}).get("Status") == "unhealthy":
                break
            if time.monotonic() >= deadline:
                raise ValueError("PROOF_HEALTH_DEADLINE")
            time.sleep(0.5)
        recovery.enroll(journal, name, None)
        state = json.loads((journal / "state.json").read_text())
        state["checked_at"] = time.time()
        state["targets"][name]["bad_since"] = time.time() - 31
        recovery.commit(journal, state)
        dispatched = 0
        lost = False

        def interrupted(state: dict[str, Any], *arguments: str) -> bytes:
            nonlocal lost, dispatched
            if arguments[0] == "restart":
                dispatched += 1
            answer = original(state, *arguments)
            selected = (
                boundary == "restart"
                and arguments[0] == "restart"
                or boundary == "disable"
                and arguments[:3] == ("update", "--restart", "no")
                or boundary == "restore"
                and arguments[:3] == ("update", "--restart", "on-failure:2")
            )
            if selected and not lost:
                lost = True
                raise RuntimeError("ISOLATED_LOST_ACK_AFTER_CONFIRMED_EFFECT")
            return answer

        recovery.docker = interrupted
        try:
            recovery.once(journal)
            pending = json.loads((journal / "state.json").read_text())["targets"][name]
            assert lost and pending["attempts"] == 1 and "restart_intent" in pending
            phase = pending["restart_intent"]["phase"]
            recovery.once(journal)
            settled = json.loads((journal / "state.json").read_text())["targets"][name]
            assert settled["attempts"] == 1 and settled["policy_limit"] == 2
            assert "restart_intent" not in settled and not settled["blocked"]
            assert dispatched == (0 if boundary == "disable" else 1)
            row = recovery.inspect(transport, identity)
            assert row["RestartCount"] == 0
            assert row["HostConfig"]["RestartPolicy"] == {
                "Name": "on-failure",
                "MaximumRetryCount": 2,
            }
            assert (
                row["Image"] == image
                and not row["Mounts"]
                and row["Name"] == "/" + name
            )
            result = {
                "boundary": boundary,
                "id": identity,
                "phase": phase,
                "attempts": 1,
                "remaining": 2,
                "restart_dispatches": dispatched,
                "native_count": 0,
            }
            recovery.atomic(
                journal / "verified.json", json.dumps(result, sort_keys=True).encode()
            )
            results.append(result)
        finally:
            recovery.docker = original
        # Only this freshly created no-volume fixture, after confirmed effects.
        original(transport, "stop", "--timeout", "2", identity)
        original(transport, "rm", identity)
        recovery.atomic(journal / "removed.json", json.dumps({"id": identity}).encode())

    journal = root / "timer"
    journal.mkdir(mode=0o700)
    recovery.commit(journal, {**transport, "targets": {}})
    spec = importlib.util.spec_from_file_location("proof_predecessor", predecessor)
    if spec is None or spec.loader is None:
        raise ValueError("PREDECESSOR_MODULE_INVALID")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    label = (
        "com.aelev.recovery." + hashlib.sha256(str(journal).encode()).hexdigest()[:16]
    )
    plist = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    if plist.exists():
        raise ValueError("PROOF_TIMER_ALREADY_EXISTS")
    module.install(journal)
    domain = f"gui/{os.getuid()}/{label}"
    try:
        answer = recovery.upgrade(journal)
        assert answer["code"] == "RECOVERY_TIMER_UPGRADED"
        assert recovery.timer_loaded(domain)
        assert json.loads((journal / "state.json").read_text())["targets"] == {}
        assert recovery.upgrade(journal)["code"] == "RECOVERY_TIMER_CURRENT"
        recovery.atomic(
            journal / "verified.json", json.dumps(answer, sort_keys=True).encode()
        )
    finally:
        if recovery.timer_loaded(domain):
            recovery.execute(["launchctl", "bootout", domain])
        if recovery.timer_loaded(domain):
            raise ValueError("PROOF_TIMER_STOP_UNCONFIRMED")
        plist.unlink()
    result = {"daemon": transport["daemon"], "cases": results, "timer_upgrade": answer}
    recovery.atomic(root / "verified.json", json.dumps(result, sort_keys=True).encode())
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--predecessor", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            prove(args.root, args.image, args.endpoint, args.predecessor),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
