"""Own native process supervision; probes read only its local heartbeat."""

from __future__ import annotations

import http.client
import json
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from .exporter import Exporter
from .files import atomic, read, writer
from .public_state import OWNER, PeerTLS, PublicServer, heartbeat, source_renewal
from .settings import AGENT, SERVER, agent, environment, server


@contextmanager
def public_listener(role: str) -> Generator[threading.Thread | None]:
    prefix = "AELEV_INFRASTRUCTURE_SPIRE_PUBLIC_SOURCE_"
    mode = os.environ.get(prefix + "MODE", "disabled")
    if mode == "disabled":
        if any(
            name.startswith(prefix) and name != prefix + "MODE" for name in os.environ
        ):
            raise ValueError("SPIRE_PUBLIC_SOURCE_CONFIGURATION_INVALID")
        yield None
        return
    if mode != "https" or role != "server":
        raise ValueError("SPIRE_PUBLIC_SOURCE_CONFIGURATION_INVALID")
    identity = PeerTLS(
        Path(os.environ[prefix + "TLS_ROOT"]),
        "source.crt",
        "source.key",
        "source-client-ca.crt",
        os.environ[prefix + "IDENTITY"],
        tuple(os.environ[prefix + "CLIENT_IDENTITIES"].split(",")),
        server=True,
    )
    port = int(os.environ.get(prefix + "PORT", "8084"))
    if not 1 <= port <= 65535:
        raise ValueError("SPIRE_PUBLIC_SOURCE_CONFIGURATION_INVALID")

    def capture() -> dict[str, Any]:
        value = read(Path("/run/spire/heartbeat.json"))
        if value.get("pid") != os.getpid():
            raise ValueError("SPIRE_PUBLIC_SOURCE_UNAVAILABLE")
        projected = heartbeat(value)
        try:
            receipt = source_renewal(
                Path(
                    os.environ.get(
                        prefix + "RENEWAL_PATH",
                        "/run/certificate-renewal/public-source.json",
                    )
                ),
                identity,
            )
        except (OSError, ValueError, KeyError, TypeError):
            receipt = None
        return {
            "schema_version": 1,
            "owner": OWNER,
            "heartbeat": projected,
            "renewal": receipt,
        }

    with PublicServer(
        (os.environ.get(prefix + "BIND_ADDRESS", "0.0.0.0"), port), identity, capture
    ) as listener:
        thread = threading.Thread(
            target=listener.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True
        )
        thread.start()
        try:
            yield thread
        finally:
            listener.shutdown()
            thread.join(timeout=3)


def native_ready() -> bool:
    connection = http.client.HTTPConnection("127.0.0.1", 8082, timeout=1)
    try:
        connection.request("GET", "/ready")
        response = connection.getresponse()
        response.read(8192)
        return response.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def run(
    role: str, plan: dict[str, Any], root: Path, bootstrap: Path, output: Path
) -> None:
    environment(plan)
    config = (
        server(plan, root, bootstrap)
        if role == "server"
        else agent(plan, root, bootstrap)
    )
    exporter = Exporter(output, plan) if role == "agent" else None
    executable = SERVER if role == "server" else AGENT
    stopping = threading.Event()

    def stop(_number: int, _frame: FrameType | None) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log_path = root / "native.jsonl"
    if log_path.is_symlink():
        raise ValueError("SPIRE_LOG_SYMLINK_FORBIDDEN")
    with (
        writer(root),
        log_path.open("ab") as log,
        public_listener(role) as public_thread,
    ):
        os.chmod(log_path, 0o600)
        child = subprocess.Popen(
            [executable, "run", "-config", str(config)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
        next_fetch, reason = 0.0, "SPIRE_STARTING"
        try:
            while not stopping.wait(1):
                if child.poll() is not None:
                    raise ValueError("SPIRE_NATIVE_PROCESS_EXITED")
                ready = native_ready()
                public_ready = public_thread is None or public_thread.is_alive()
                if exporter is not None and time.monotonic() >= next_fetch:
                    next_fetch = time.monotonic() + 5
                    try:
                        exporter.once()
                        reason = "SPIRE_SVID_READY"
                    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                        code = str(error)
                        reason = (
                            code
                            if re.fullmatch(r"SPIRE_[A-Z0-9_]{1,120}", code)
                            else "SPIRE_SVID_DELIVERY_UNAVAILABLE"
                        )
                        exporter.unavailable(reason)
                status_reason = reason
                if not exporter:
                    status_reason = "SPIRE_READY" if ready else "SPIRE_NOT_READY"
                    if not public_ready:
                        status_reason = "SPIRE_PUBLIC_SOURCE_STOPPED"
                status = {
                    "owner": "infrastructure-spire",
                    "role": role,
                    "pid": os.getpid(),
                    "ready": ready
                    and public_ready
                    and (exporter is None or reason == "SPIRE_SVID_READY"),
                    "reason": status_reason,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
                atomic(Path("/run/spire/heartbeat.json"), status)
                projection = os.environ.get(
                    "AELEV_INFRASTRUCTURE_SPIRE_OBSERVATION_ROOT"
                )
                if projection:
                    # A distinct volume contains only this allowlisted public
                    # projection, never the native admin socket or CA files.
                    atomic(Path(projection) / "heartbeat.json", status)
        finally:
            if exporter is not None:
                exporter.unavailable("SPIRE_AGENT_STOPPED")
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            log.flush()
            os.fsync(log.fileno())


def log_event(code: str) -> None:
    # The wrapper exposes safe codes, not native stderr or registration data.
    import logging

    logging.getLogger("spire").info(json.dumps({"code": code}))
