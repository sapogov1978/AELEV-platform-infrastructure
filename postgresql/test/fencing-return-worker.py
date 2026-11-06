"""Opt-in real Docker return test; native DCS is deliberately simulated.

Run in the trusted checks image with /run/docker.sock and the owner's source.
No PostgreSQL, etcd, credentials, real network or existing volume is used.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

from aelev_postgresql import fencing, fencing_intent, fencing_return
from aelev_postgresql.configuration import PREFIX


def docker(*args: str) -> str:
    """Use the fixed local socket, not an inherited remote context."""
    result = subprocess.run(
        ["docker", "--host", "unix:///run/docker.sock", *args],
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    if result.returncode:
        raise ValueError("TEST_DOCKER_OPERATION_FAILED")
    return result.stdout.strip()


def record(*args: Any) -> None:
    """Keep the test audit inside its disposable owner directory."""
    with (fencing_intent.ROOT / "test-audit.jsonl").open("a") as stream:
        stream.write(json.dumps(args) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def child(root: Path, absence: bool) -> None:
    fencing_intent.ROOT = root
    with patch.object(
        fencing_return,
        "confirmed_claim",
        return_value=None if absence else fencing.Claim("postgresql-1", "7", "8"),
    ):
        worker = fencing_return.Returns(
            fencing.Docker(), {node: threading.Lock() for node in fencing.NODES}, record
        )
        worker.once()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image")
    parser.add_argument("--resume-root", type=Path)
    parser.add_argument("--absence", action="store_true")
    parser.add_argument("--successful-rpc", action="store_true")
    args = parser.parse_args()
    if args.resume_root is not None:
        child(args.resume_root, args.absence)
        return
    if not args.image:
        parser.error("--image requires an already inspected immutable local image ID")
    image = json.loads(docker("image", "inspect", args.image))[0]["Id"]
    cluster = "fencing-return-test-" + uuid.uuid4().hex
    name = cluster + "-target"
    identifier: str | None = None
    os.environ.update(
        {
            PREFIX + "CLUSTER": cluster,
            PREFIX + "FENCING_MODE": "docker",
            PREFIX + "FENCING_TIMEOUT_SECONDS": "20",
            PREFIX + "FENCING_DOCKER_LOCAL_NODES": "postgresql-2",
        }
    )
    try:
        identifier = docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--memory",
            "64m",
            "--cpus",
            "0.25",
            "--pids-limit",
            "16",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--label",
            fencing.CLUSTER_LABEL + "=" + cluster,
            "--label",
            fencing.NODE_LABEL + "=postgresql-2",
            "--entrypoint",
            "python",
            image,
            "-c",
            "import time; time.sleep(3600)",
        )
        with tempfile.TemporaryDirectory(prefix="fencing-return-test-") as directory:
            root = Path(directory)
            fencing_intent.ROOT = root
            backend = fencing.Docker()
            original = backend.call

            def lost_lease(method: str, path: str) -> Any:
                result = original(method, path)
                if "/stop?" in path:
                    raise fencing.FencingError(
                        "Injected expired lease", "LEASE_CHANGED"
                    )
                return result

            with patch.object(fencing.Claim, "verify"):
                if args.successful_rpc:
                    receipt = backend.fence(
                        "postgresql-2", fencing.Claim("postgresql-1", "5", "6"), record
                    )
                    assert receipt == [identifier]
                else:
                    with patch.object(backend, "call", side_effect=lost_lease):
                        try:
                            backend.fence(
                                "postgresql-2",
                                fencing.Claim("postgresql-1", "5", "6"),
                                record,
                            )
                        except fencing.FencingError as error:
                            assert error.code == "LEASE_CHANGED"
                        else:
                            raise AssertionError("Fault did not interrupt fencing")
            store = fencing_intent.IntentStore("postgresql-2")
            pending = store.load()
            assert pending is not None
            assert pending["restart_required"] is True
            assert pending["start_attempts"] == 0
            assert pending["phase"] == (
                "STOP_CONFIRMED" if args.successful_rpc else "STOP_REQUESTED"
            )
            assert not original("GET", f"/containers/{identifier}/json")["State"][
                "Running"
            ]
            # New OS process must read the durable intent, not Python memory.
            resume = [sys.executable, __file__, "--resume-root", str(root)]
            if args.absence:
                resume.append("--absence")
            for _ in range(2):
                subprocess.run(
                    resume,
                    check=True,
                    timeout=90,
                )
            intent = store.load()
            assert intent is not None
            assert intent["phase"] == "COMPLETED" and intent["start_attempts"] == 1
            resumed = original("GET", f"/containers/{identifier}/json")["State"]
            assert resumed["Running"] and resumed["StartedAt"] != intent["started_at"]
            # A later manual stop must remain stopped, even after another pass.
            original("POST", f"/containers/{identifier}/stop?t=1")
            subprocess.run(
                resume,
                check=True,
                timeout=90,
            )
            assert not original("GET", f"/containers/{identifier}/json")["State"][
                "Running"
            ]
            sys.stdout.write(
                json.dumps(
                    {
                        "test": "docker-return-worker",
                        "result": "PASS",
                        "real_stop_start": True,
                        "new_executor_process": True,
                        "start_attempts": 1,
                        "manual_stop_preserved": True,
                        "native_dcs": "SIMULATED",
                        "lease_absence": args.absence,
                        "successful_rpc_return_deferred": args.successful_rpc,
                        "working_cluster_used": False,
                    }
                )
                + "\n"
            )
    finally:
        if identifier is not None:
            data = json.loads(docker("inspect", identifier))[0]
            if (
                data["Id"] == identifier
                and data["Image"] == image
                and data["Config"]["Labels"].get(fencing.CLUSTER_LABEL) == cluster
                and data["Config"]["Labels"].get(fencing.NODE_LABEL) == "postgresql-2"
                and data["Name"] == "/" + name
            ):
                docker("rm", "-f", identifier)
            else:
                raise ValueError("TEST_CLEANUP_OWNERSHIP_UNCONFIRMED")


if __name__ == "__main__":
    main()
