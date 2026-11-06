"""Real public registration on one disposable target; no host timer is installed."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery
import recovery_host


def prove(image: str, endpoint: str) -> dict[str, Any]:
    if re.fullmatch(r"sha256:[a-f0-9]{64}", image) is None or not endpoint.startswith(
        "unix:///"
    ):
        raise ValueError("PINNED_LOCAL_TEST_SCOPE_REQUIRED")
    run = uuid.uuid4().hex
    name = "aelev-registration-test-" + run[:12]
    current: dict[str, Any] = {
        "schema_version": 1,
        "docker": shutil.which("docker"),
        "endpoint": endpoint,
        "targets": {},
    }
    current["daemon"] = (
        recovery.docker(current, "info", "--format", "{{.ID}}").decode().strip()
    )
    created: str | None = None
    helper_names: list[str] = []
    try:
        created = (
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
                "32",
                "--tmpfs",
                "/tmp:size=1m,mode=1777",
                "--restart",
                "on-failure:3",
                "--label",
                "io.aelev.test-run=" + run,
                "--label",
                "io.aelev.recovery.registration=unguarded-automatic-v1",
                "--label",
                "com.docker.compose.project=" + name,
                "--label",
                "com.docker.compose.service=probe",
                "--label",
                "com.docker.compose.config-hash=" + "a" * 64,
                "--health-cmd",
                "python -S -c 'raise SystemExit(0)'",
                "--health-interval",
                "1s",
                "--health-timeout",
                "2s",
                "--entrypoint",
                "python",
                image,
                "-S",
                "-c",
                "import signal,sys,time; "
                "signal.signal(signal.SIGUSR1,lambda *_:sys.exit(1)); time.sleep(900)",
            )
            .decode()
            .strip()
        )
        if re.fullmatch(r"[a-f0-9]{64}", created) is None:
            raise ValueError("TEST_CONTAINER_ID_INVALID")

        def healthy() -> dict[str, Any]:
            deadline = time.monotonic() + 90
            while True:
                observed = recovery.inspect(current, created)
                if observed["State"].get("Health", {}).get("Status") == "healthy":
                    return observed
                if time.monotonic() >= deadline:
                    sys.stderr.write(
                        json.dumps(
                            {
                                "code": "TEST_HEALTH_DEADLINE",
                                "running": observed["State"].get("Running"),
                                "oom": observed["State"].get("OOMKilled"),
                                "exit_code": observed["State"].get("ExitCode"),
                                "native_count": observed.get("RestartCount"),
                                "probes": [
                                    p.get("ExitCode")
                                    for p in observed["State"]
                                    .get("Health", {})
                                    .get("Log", [])[-3:]
                                ],
                            }
                        )
                        + "\n"
                    )
                    raise ValueError("TEST_HEALTH_DEADLINE")
                time.sleep(0.5)

        healthy()
        # Crash the test process once from inside its own namespace. A tmpfs
        # flag cannot remember this across a Docker restart, so never use one.
        subprocess.run(
            [
                current["docker"],
                "--host",
                endpoint,
                "exec",
                created,
                "python",
                "-S",
                "-c",
                "import os,signal;os.kill(1,signal.SIGUSR1)",
            ],
            capture_output=True,
            timeout=20,
            check=False,
        )
        deadline = time.monotonic() + 30
        while recovery.inspect(current, created)["RestartCount"] != 1:
            if time.monotonic() >= deadline:
                raise ValueError("TEST_NATIVE_EXIT_UNCONFIRMED")
            time.sleep(0.5)
        row = healthy()
        if (
            row["RestartCount"] != 1
            or row.get("Mounts")
            and any(m.get("Type") in {"bind", "volume"} for m in row["Mounts"])
        ):
            raise ValueError("TEST_NATIVE_HISTORY_OR_MOUNTS_INVALID")
        with tempfile.TemporaryDirectory(
            prefix="aelev-registration-state-"
        ) as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            recovery.commit(root, current)
            (root / ".lock").touch(mode=0o600)
            group = recovery_host.socket_group(current, image)
            request = {
                "schema_version": 1,
                "daemon_id": current["daemon"],
                "action": "register",
                "targets": [{"name": name, "expected": recovery.fingerprint(row)}],
            }

            def invoke() -> dict[str, Any]:
                helper_name = name + "-operator-" + str(len(helper_names))
                helper_names.append(helper_name)
                command = [
                    current["docker"],
                    "--host",
                    endpoint,
                    "run",
                    "--rm",
                    "--interactive",
                    "--name",
                    helper_name,
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
                    group,
                    "--label",
                    "io.aelev.test-run=" + run,
                    "--mount",
                    f"type=bind,source={root},target=/var/lib/aelev-recovery",
                    "--mount",
                    "type=bind,source="
                    + endpoint.removeprefix("unix://")
                    + ",target=/var/run/docker.sock",
                    image,
                    "--host-timer-quiesced",
                ]
                completed = subprocess.run(
                    command,
                    input=json.dumps(request, sort_keys=True).encode() + b"\n",
                    capture_output=True,
                    timeout=120,
                    check=False,
                )
                if completed.returncode or len(completed.stdout) > 65536:
                    raise ValueError("TEST_PUBLIC_REGISTRATION_FAILED")
                return json.loads(completed.stdout)

            answer = invoke()
            first = json.loads((root / "state.json").read_bytes())
            target = answer["targets"][name]
            assert target["attempts"] == target["native_count"] == 1
            assert target["registration_complete"] and target["automatic_recovery"]
            assert target["policy_limit"] == 0
            before = (root / "state.json").read_bytes()
            assert invoke() == answer
            assert (root / "state.json").read_bytes() == before
            checked = recovery.inspect(current, created)
            assert checked["State"]["StartedAt"] == row["State"]["StartedAt"]
            assert checked["HostConfig"]["RestartPolicy"] == recovery.NO_RESTART
            recovery.docker(current, "stop", "--timeout", "2", created)
            # Same core, one explicit pass on this test journal only; no timer.
            recovery.once(root)
            stopped = recovery.inspect(current, created)
            assert not stopped["State"]["Running"]
            saved = (root / "state.json").read_bytes()
            repeated = invoke()
            assert (root / "state.json").read_bytes() == saved
            assert not recovery.inspect(current, created)["State"]["Running"]
            assert (
                repeated["targets"][name]["attempts"]
                >= first["targets"][name]["attempts"]
            )
            return {
                "code": "REGISTRATION_SANDBOX_PASSED",
                "image": image,
                "daemon_id": current["daemon"],
                "test_scope": name,
                "initial_native_history": 1,
                "manual_stop_preserved": True,
                "host_timer_installed": False,
                "working_state_accessed": False,
            }
    finally:
        # Never enumerate/delete unrelated names or any volume/network.
        for owned_name in [*helper_names, name]:
            ids = (
                recovery.docker(
                    current,
                    "ps",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    "name=^/" + owned_name + "$",
                    "--format",
                    "{{.ID}}",
                )
                .decode()
                .splitlines()
            )
            if len(ids) > 1:
                raise ValueError("TEST_CLEANUP_SCOPE_CHANGED")
            if ids:
                found = recovery.inspect(current, ids[0])
                if (
                    found.get("Image") != image
                    or found.get("Config", {})
                    .get("Labels", {})
                    .get("io.aelev.test-run")
                    != run
                ):
                    raise ValueError("TEST_CLEANUP_SCOPE_CHANGED")
                recovery.docker(current, "rm", "--force", found["Id"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--endpoint", required=True)
    args = parser.parse_args()
    sys.stdout.write(
        json.dumps(prove(args.image, args.endpoint), sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    main()
