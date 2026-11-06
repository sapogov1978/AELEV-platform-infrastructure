"""Opt-in real Docker proof using one disposable target and no working data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    journal = output / "journal"
    journal.mkdir(mode=0o700)
    token = uuid4().hex
    project = "aelev-auto-test-" + token[:12]
    name = project + "-fault-1"

    def docker(*arguments: str) -> bytes:
        completed = subprocess.run(
            ["docker", *arguments], capture_output=True, check=False, timeout=30
        )
        if completed.returncode:
            recovery.atomic(output / "command-failure.stderr", completed.stderr[:16384])
            raise RuntimeError("AUTOMATIC_SANDBOX_DOCKER_FAILED")
        return completed.stdout

    def row() -> dict[str, Any]:
        current: dict[str, Any] = json.loads(docker("inspect", name))[0]
        assert current["Config"]["Labels"]["io.aelev.recovery-test"] == token
        return current

    def wait(
        predicate: Any, *, tick: bool = True, external_boot: bool = False
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            state = recovery.once(journal) if tick else {}
            target = state.get("targets", {}).get(name, {})
            if predicate(target):
                return cast(dict[str, Any], target)
            if target.get("blocked") and not (
                external_boot and target.get("code") == "AUTOMATIC_EXIT_UNCONFIRMED"
            ):
                raise RuntimeError("AUTOMATIC_SANDBOX_BLOCKED_" + target["code"])
            time.sleep(0.5)
        raise RuntimeError("AUTOMATIC_SANDBOX_DEADLINE")

    original = {
        "delays": recovery.DELAYS,
        "cooldown": recovery.AUTO_COOLDOWN,
        "healthy": recovery.AUTO_HEALTHY,
    }
    recovery.DELAYS = (1, 1, 1)
    recovery.AUTO_COOLDOWN = 3
    recovery.AUTO_HEALTHY = 3
    body = (
        "import time; from pathlib import Path\n"
        "while True:\n"
        " p=Path('/tmp/crash-once')\n"
        " if p.exists():\n"
        "  p.unlink(); raise SystemExit(7)\n"
        " time.sleep(.05)\n"
    )
    created = False
    try:
        assert not docker("ps", "-aq", "--filter", "name=^/" + name + "$").strip()
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "--memory",
            "64m",
            "--cpus",
            "0.5",
            "--pids-limit",
            "32",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--restart",
            "no",
            "--label",
            "io.aelev.recovery-test=" + token,
            "--label",
            "com.docker.compose.project=" + project,
            "--label",
            "com.docker.compose.service=fault",
            "--label",
            "com.docker.compose.config-hash="
            + hashlib.sha256(body.encode()).hexdigest(),
            "--entrypoint",
            "python",
            "--health-cmd",
            "python -S -c \"from pathlib import Path; raise SystemExit(Path('/tmp/bad').exists())\"",
            "--health-interval",
            "1s",
            "--health-timeout",
            "1s",
            "--health-retries",
            "1",
            args.image,
            "-S",
            "-c",
            body,
        )
        created = True
        wait(
            lambda _target: row()["State"].get("Health", {}).get("Status") == "healthy",
            tick=False,
        )
        recovery.enroll(journal, name, None)
        recovery.automatic_enable(journal, name)
        docker(
            "exec",
            name,
            "python",
            "-S",
            "-c",
            "from pathlib import Path; Path('/tmp/bad').touch()",
        )
        print("AUTOMATIC_SANDBOX_WAITING_FOUR_ATTEMPTS", flush=True)
        target = wait(lambda value: value.get("attempts", 0) >= 4)
        assert not target["blocked"] and target["automatic"]["burst_attempts"] >= 4
        docker(
            "exec",
            name,
            "python",
            "-S",
            "-c",
            "from pathlib import Path; Path('/tmp/bad').unlink()",
        )
        target = wait(
            lambda value: value.get("code") == "HEALTHY"
            and value["automatic"]["burst_attempts"] == 0
        )
        lifetime = target["attempts"]
        print("AUTOMATIC_SANDBOX_RECOVERED_WITHOUT_RESET", flush=True)
        docker(
            "exec",
            name,
            "python",
            "-S",
            "-c",
            "from pathlib import Path; Path('/tmp/crash-once').touch()",
        )
        target = wait(
            lambda value: value.get("attempts", 0) > lifetime
            and value.get("code") in {"AUTOMATIC_RESTARTED", "HEALTHY"}
        )
        assert target["automatic"]["last_action"]["verb"] == "start"
        wait(lambda value: value.get("code") == "HEALTHY")
        before_stop = row()["State"]["StartedAt"]
        docker("stop", "--time", "2", name)
        target = recovery.once(journal)["targets"][name]
        assert target["blocked"] and target["code"] == "AUTOMATIC_EXIT_UNCONFIRMED"
        time.sleep(4)
        recovery.once(journal)
        assert (
            not row()["State"]["Running"] and row()["State"]["StartedAt"] == before_stop
        )
        docker("start", name)  # Explicit external resume, not recovery authority.
        wait(
            lambda value: value.get("code") == "HEALTHY" and not value["blocked"],
            external_boot=True,
        )
        result = {
            "result": "PASS",
            "real_restarts": lifetime,
            "automatic_crash_start": True,
            "recovered_without_reset": True,
            "manual_stop_preserved": True,
            "external_start_rearms_observation": True,
            "production_timing": original,
            "test_timing_seconds": [1, 1, 1, 3, 3],
            "working_data_used": False,
            "volumes_created": 0,
        }
        recovery.atomic(
            output / "result.json", json.dumps(result, sort_keys=True).encode()
        )
        print(json.dumps(result), flush=True)
    finally:
        if created:
            row()
            docker("rm", "--force", name)


if __name__ == "__main__":
    main()
