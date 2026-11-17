"""Reduce existing private command failures to public, finite diagnostic fields."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

LIMIT = 2 * 1024 * 1024
STEPS = {
    "starting Kafka": "START_KAFKA",
    "preparing owner configuration": "PREPARE_OWNER_CONFIGURATION",
    "starting operator identity": "START_OPERATOR_IDENTITY",
    "preparing object-store replication": "PREPARE_OBJECT_STORE_REPLICATION",
    "starting declared core roles": "START_CORE_ROLES",
    "preparing local SPIRE owner inputs": "PREPARE_LOCAL_SPIRE_INPUTS",
    "checking all owner coordinators": "CHECK_OWNER_COORDINATORS",
    "checking three SPIRE renewal deliveries": "CHECK_SPIRE_RENEWAL",
}
ERROR_TYPES = {
    "HTTPStatusError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "TimeoutError",
    "PermissionError",
    "FileNotFoundError",
    "ConnectionError",
    "RuntimeError",
    "ValueError",
    "JSONDecodeError",
    "SSLError",
    "SSLCertVerificationError",
}


def bounded_read(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > LIMIT:
        return ""
    with path.open("rb") as source:
        raw = source.read(LIMIT + 1)
    return raw.decode(errors="replace") if len(raw) <= LIMIT else ""


def runtime_progress(raw: str | bytes | None, root: Path) -> dict[str, str]:
    """A marker announces a step before execution; it never proves completion."""
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw or ""
    roles: set[str] = set()
    owners: set[str] = set()
    try:
        catalog = json.loads(bounded_read(root / "deployment-catalog.json"))
        roles = {
            row["id"]
            for row in catalog["roles"]
            if isinstance(row.get("id"), str)
            and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,95}", row["id"])
        }
        owners = {
            row["owner"]
            for row in catalog["roles"]
            if isinstance(row.get("owner"), str)
            and re.fullmatch(
                r"(?:[a-z0-9][a-z0-9-]*/)*[a-z0-9][a-z0-9-]*", row["owner"]
            )
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        roles = set()
        owners = set()
    last: dict[str, str] = {}
    for line in text[-LIMIT:].splitlines():
        if not line.startswith("[full-runtime] "):
            continue
        value = line.removeprefix("[full-runtime] ")
        if value in STEPS:
            last = {"step": STEPS[value]}
        elif value.startswith("starting declared owner "):
            owner = value.removeprefix("starting declared owner ")
            last = {"step": "START_OWNER_ROLES"}
            if owner in owners:
                last["owner"] = owner
        elif value.startswith("provisioning "):
            task = value.removeprefix("provisioning ")
            last = {"step": "PROVISION_OWNER_TASK"}
            if task in roles:
                last["task"] = task
    return last


def failure_summary(text: str) -> dict[str, Any]:
    types = (
        set(
            re.findall(
                r"^(?:Provisioning failed: |[a-z_][a-z0-9_.]*\.)?([A-Z][A-Za-z0-9]{0,40})(?=:|$)",
                text,
                re.MULTILINE,
            )
        )
        & ERROR_TYPES
    )
    statuses = re.findall(
        r"\bHTTP(?:/\d(?:\.\d)?)?\s+([45][0-9]{2})\b|(?:Client|Server) error ['\"]([45][0-9]{2})\b",
        text,
    )
    lower = text.lower()
    categories = {
        "TLS_FAILURE": ("x509:", "certificate verify failed", "tls handshake"),
        "AUTH_FAILURE": (
            "access denied",
            "accessdenied",
            "invalidaccesskeyid",
            "signaturedoesnotmatch",
        ),
        "TRANSPORT_FAILURE": (
            "connection refused",
            "no such host",
            "dial tcp",
            "i/o timeout",
        ),
        "QUORUM_UNAVAILABLE": ("quorum", "server not initialized"),
        "REPLICATION_STATE_UNRECOGNIZED": (
            "cannot identify the existing site-replication state",
        ),
        "REPLICATION_TOPOLOGY_REJECTED": (
            "existing site replication does not contain",
        ),
        "SECRET_INPUT_UNAVAILABLE": (
            "required minio secret file is missing, empty, or unreadable",
        ),
    }
    return {
        "error_types": sorted(types),
        "http_statuses": sorted(
            {int(value) for row in statuses for value in row if value}
        ),
        "categories": sorted(
            name
            for name, phrases in categories.items()
            if any(p in lower for p in phrases)
        ),
    }


def command_failure_summaries(root: Path) -> list[dict[str, Any]]:
    """Never export file names, raw output, arbitrary codes or message values."""
    runtime = root / "runtime"
    if runtime.is_symlink():
        return []
    result: list[dict[str, Any]] = []
    try:
        for directory in sorted(runtime.glob("full-runtime-failure-*"))[:16]:
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                record = json.loads(bounded_read(directory / "result.json"))
                status = record.get("status")
                if status not in {"FAILED", "UNKNOWN"}:
                    continue
                text = (
                    bounded_read(directory / "stdout")
                    + "\n"
                    + bounded_read(directory / "stderr")
                )
                result.append({"status": status, **failure_summary(text)})
            except (OSError, ValueError, TypeError, AttributeError):
                continue
    except OSError:
        return [{"status": "DIAGNOSTIC_UNAVAILABLE"}]
    return result
