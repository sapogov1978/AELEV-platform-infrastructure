"""Publish local health from the serving event loop, never from the probe."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from translator_service import local_health


def enabled(role: str) -> bool:
    return os.environ.get("AELEV_TRANSLATOR_HEALTH_ROLE") == role


def withdraw(role: str, *, root: str = local_health.ROOT) -> None:
    (Path(root) / f"{role}.signal").unlink(missing_ok=True)


def publish(role: str, *, root: str = local_health.ROOT) -> None:
    raw = local_health.frame(role)
    _write(role, raw, root=root)


def progress(role: str, phase: str = "loop", *, root: str = local_health.ROOT) -> None:
    """Only the main loop calls this; no independent thread certifies progress."""
    if enabled(role):
        _write(role, local_health.progress_frame(role, phase), root=root)


def _write(role: str, raw: bytes, *, root: str) -> None:
    path = Path(root)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{role}-", dir=path)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        os.replace(temporary, path / f"{role}.signal")
    finally:
        Path(temporary).unlink(missing_ok=True)


async def run(
    role: str,
    ready: Callable[[], bool],
    *,
    root: str = local_health.ROOT,
) -> None:
    """A stalled event loop cannot refresh its owner-local marker."""
    withdraw(role, root=root)
    try:
        while True:
            if ready():
                try:
                    publish(role, root=root)
                except (OSError, ValueError):
                    withdraw(role, root=root)
            else:
                withdraw(role, root=root)
            await asyncio.sleep(1)
    finally:
        withdraw(role, root=root)
