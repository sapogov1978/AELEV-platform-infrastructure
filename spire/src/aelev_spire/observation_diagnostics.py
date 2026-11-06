"""Bounded locations of this observer's two critical threads, never their data."""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .files import atomic

PHASES = frozenset(
    {
        "STARTING",
        "WAIT",
        "SNAPSHOT",
        "SCAN_PENDING",
        "READ_PACKET",
        "CAPTURE",
        "TLS_CONTEXT",
        "HTTP_CONNECT",
        "HTTP_REPLY",
        "HTTP_BODY",
        "ACK_VALIDATE",
        "JOURNAL_ACK",
        "JOURNAL_DELETE",
    }
)
CODES = frozenset(
    {
        "SPIRE_OBSERVATION_WORKER_STOPPED",
        "SPIRE_OBSERVATION_WORKER_STALLED",
    }
)


def locations(identifier: int | None) -> list[dict[str, str | int]]:
    """Exclude locals, source lines, full paths and every unrelated thread."""
    # CPython's diagnostic hook is intentional; frame locals are never read.
    frames = sys._current_frames()  # pyright: ignore[reportPrivateUsage]
    frame = frames.get(identifier) if identifier is not None else None
    del frames
    result: list[dict[str, str | int]] = []
    try:
        while frame is not None and len(result) < 16:
            name = Path(frame.f_code.co_filename).name
            function = frame.f_code.co_name
            result.append(
                {
                    "file": (
                        name
                        if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name)
                        else "unknown"
                    ),
                    "function": (
                        function
                        if re.fullmatch(r"[A-Za-z0-9_<>]{1,64}", function)
                        else "unknown"
                    ),
                    "line": min(max(0, frame.f_lineno), 9999999),
                }
            )
            frame = frame.f_back
    finally:
        del frame
    return result


class Diagnostics:
    """One durable terminal report per process; phase changes do not renew health."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.run_id = str(uuid.uuid4())
        self.supervisor = threading.get_ident()
        self.observation = ("STARTING", time.monotonic())

    def mark(self, phase: str) -> None:
        if phase not in PHASES:
            raise ValueError("SPIRE_OBSERVATION_DIAGNOSTIC_PHASE_INVALID")
        # One immutable observation; diagnostic capture must not wait on a lock
        # potentially held by the very worker being diagnosed.
        self.observation = (phase, time.monotonic())

    def record(
        self,
        code: str,
        worker: threading.Thread,
        *,
        progress_age: float,
        supervisor_cycle: float,
        error: BaseException | None = None,
    ) -> bool:
        """Diagnostic failure must never conceal the original terminal reason."""
        try:
            if code not in CODES:
                raise ValueError("SPIRE_OBSERVATION_DIAGNOSTIC_CODE_INVALID")
            phase, phase_at = self.observation
            report: dict[str, Any] = {
                "owner": "infrastructure-spire",
                "schema_version": 1,
                "run_id": self.run_id,
                "code": code,
                "observed_at": datetime.now(UTC).isoformat(),
                "phase": phase,
                "phase_age_seconds": max(0, time.monotonic() - phase_at),
                "progress_age_seconds": max(0, progress_age),
                "supervisor_cycle_seconds": max(0, supervisor_cycle),
                "exception_type": (
                    type(error).__name__
                    if error is not None
                    and re.fullmatch(r"[A-Za-z0-9_]{1,64}", type(error).__name__)
                    else None
                ),
                "threads": {
                    "supervisor": locations(self.supervisor),
                    "worker": locations(worker.ident),
                },
            }
            raw = json.dumps(report, sort_keys=True, allow_nan=False).encode()
            if len(raw) > 8192:
                raise ValueError("SPIRE_OBSERVATION_DIAGNOSTIC_TOO_LARGE")
            atomic(self.root / "diagnostics" / (self.run_id + ".json"), raw)
            return True
        except (OSError, ValueError, TypeError, RuntimeError):
            with suppress(OSError):
                sys.stderr.write(
                    '{"code":"SPIRE_OBSERVATION_DIAGNOSTIC_WRITE_FAILED"}\n'
                )
            return False
