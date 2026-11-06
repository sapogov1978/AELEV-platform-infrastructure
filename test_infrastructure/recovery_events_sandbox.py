"""Real Docker proof: evicted crash recovers, persisted manual stop never does."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.output.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    token = uuid4().hex
    names = [
        "aelev-events-test-" + token[:12] + "-" + role for role in ("fault", "noise")
    ]
    made: dict[str, str] = {}
    stopped = threading.Event()
    processes: list[subprocess.Popen[bytes]] = []
    observer: threading.Thread | None = None
    result: dict[str, Any] = {"scope": token, "checks": {}}

    def docker(*arguments: str) -> bytes:
        return recovery.execute(["docker", *arguments], timeout=45)

    def own(name: str) -> dict[str, Any]:
        row: dict[str, Any] = json.loads(docker("inspect", name))[0]
        assert (
            row["Id"] == made[name]
            and row["Config"]["Labels"]["io.aelev.events-test"] == token
        )
        return row

    def wait(predicate: Any, seconds: float = 45) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.2)
        raise RuntimeError("EVENT_SANDBOX_DEADLINE")

    try:
        for name in names:
            body = "import time; from pathlib import Path;\nwhile True:\n if Path('/tmp/crash').exists():\n  Path('/tmp/crash').unlink(); raise SystemExit(7)\n time.sleep(.05)"
            identity = (
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
                    "0.25",
                    "--pids-limit",
                    "32",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--restart",
                    "no",
                    "--label",
                    "io.aelev.events-test=" + token,
                    "--label",
                    "com.docker.compose.project=aelev-events-test-" + token,
                    "--label",
                    "com.docker.compose.service=" + name,
                    "--health-cmd",
                    "python -S -c 'pass'",
                    "--health-interval",
                    "1s",
                    "--health-timeout",
                    "5s",
                    "--health-retries",
                    "3",
                    "--entrypoint",
                    "python",
                    args.image,
                    "-S",
                    "-c",
                    body,
                )
                .decode()
                .strip()
            )
            made[name] = identity
        wait(
            lambda: all(
                own(name)["State"]["Health"]["Status"] == "healthy" for name in names
            )
        )
        recovery.enroll(root, names[0], None)
        recovery.automatic_enable(root, names[0])
        recovery.atomic(root / "event-observation.json", b"{}")
        observer = threading.Thread(
            target=recovery.collect_events, args=(root, stopped, processes), daemon=True
        )
        observer.start()
        wait(lambda: (root / "docker-events.json").exists())
        # Docker includes argv (including newlines) in exec_create's Action.
        # The format must discard it before readline, not after persistence.
        docker(
            "exec", made[names[1]], "python", "-S", "-c", "# " + "x" * 2000 + "\npass\n"
        )
        time.sleep(2)
        assert (
            len(json.loads((root / "docker-events.json").read_bytes())["epochs"]) == 1
        )
        result["checks"]["multiline_exec_does_not_interrupt_reader"] = True
        recovery.once(root)
        own(names[0])
        docker(
            "exec",
            made[names[0]],
            "python",
            "-S",
            "-c",
            "from pathlib import Path; Path('/tmp/crash').touch()",
        )
        wait(lambda: own(names[0])["State"]["Status"] == "exited")
        finished = recovery.automatic_stamp(own(names[0])["State"]["FinishedAt"])
        wait(
            lambda: any(
                e["id"] == made[names[0]] and e["action"] == "die"
                for e in json.loads((root / "docker-events.json").read_bytes())[
                    "events"
                ]
            )
        )
        # Every exec creates several Docker events. No working container is used.
        for _ in range(90):
            docker("exec", made[names[1]], "python", "-S", "-c", "pass")
        stamps = [
            int(v) / 1e9
            for v in docker(
                "events",
                "--since",
                "0",
                "--until",
                f"{time.time():.9f}",
                "--format",
                "{{.TimeNano}}",
            ).splitlines()
        ]
        assert min(stamps) > finished
        result["checks"]["die_evicted_from_docker"] = True
        recovery.DELAYS = (
            1,
            1,
            1,
        )  # Only this isolated process/journal, not the host timer.

        def recovered() -> bool:
            target = recovery.once(root)["targets"][names[0]]
            assert not target["blocked"], target["code"]
            return own(names[0])["State"]["Running"] is True

        wait(recovered)
        wait(lambda: own(names[0])["State"]["Health"]["Status"] == "healthy")
        state = recovery.once(root)
        assert state["targets"][names[0]]["attempts"] == 1
        result["checks"]["crash_started_exactly_once"] = True
        # Docker stop leads to nonzero 143 in this fixture; it is still manual.
        docker("stop", "--time", "1", made[names[0]])
        wait(
            lambda: any(
                e["id"] == made[names[0]] and e["action"] == "stop"
                for e in json.loads((root / "docker-events.json").read_bytes())[
                    "events"
                ]
            )
        )
        target = recovery.once(root)["targets"][names[0]]
        assert target["blocked"] and target["code"] == "AUTOMATIC_EXIT_UNCONFIRMED"
        assert target["attempts"] == 1 and not own(names[0])["State"]["Running"]
        result["checks"]["manual_stop_not_started"] = True
        evidence = json.loads((root / "docker-events.json").read_bytes())
        assert all(e["id"] == made[names[0]] for e in evidence["events"])
        result["checks"]["foreign_identities_not_saved"] = True
        result["code"] = "PASS"
    finally:
        stopped.set()
        for process in list(processes):
            process.terminate()
        if observer is not None:
            observer.join(timeout=5)
        for name in reversed(names):
            if name in made:
                own(name)
                docker("rm", "-f", made[name])
        recovery.atomic(root / "result.json", json.dumps(result, indent=2).encode())
        print(json.dumps(result))


if __name__ == "__main__":
    main()
