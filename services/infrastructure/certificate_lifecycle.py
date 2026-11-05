"""Fail-closed audit of owner-declared local X.509 certificate lifecycles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GENERIC_OWNER = "telemetry-certificate-renewal"
POSTGRESQL_OWNER = "postgresql-client-renewal"
GENERIC_WINDOW_SECONDS = 30 * 24 * 60 * 60
EGRESS_WINDOW_SECONDS = 3 * 24 * 60 * 60
REPORT_MAX_AGE_SECONDS = 180


class LifecycleError(Exception):
    """A bounded lifecycle invariant failed without exposing private material."""

    def __init__(self, code: str, subject: str | None = None) -> None:
        super().__init__(code)
        self.subject = subject


@dataclass(frozen=True)
class Lifecycle:
    owner: str
    state: Path
    certificate: Path
    certificate_sha256: str
    certificate_expires_at: float
    ca_expires_at: float
    checked_at: float


def require(condition: bool, code: str) -> None:
    """Raise one stable diagnostic code for a failed invariant."""
    if not condition:
        raise LifecycleError(code)


def read_json(path: Path) -> dict[str, Any]:
    """Read a bounded object without duplicate or non-finite fields."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            require(key not in result, "CERTIFICATE_LIFECYCLE_DUPLICATE_FIELD")
            result[key] = value
        return result

    def invalid(_: str) -> None:
        raise LifecycleError("CERTIFICATE_LIFECYCLE_INVALID_JSON")

    require(
        path.is_file() and not path.is_symlink() and path.stat().st_size <= 256 * 1024,
        "CERTIFICATE_LIFECYCLE_INVALID_JSON",
    )
    value = json.loads(
        path.read_bytes(), object_pairs_hook=pairs, parse_constant=invalid
    )
    require(isinstance(value, dict), "CERTIFICATE_LIFECYCLE_INVALID_JSON")
    return dict(value)


def digest(path: Path) -> str:
    """Hash a public certificate without returning its contents."""
    require(path.is_file() and not path.is_symlink(), "CERTIFICATE_LIFECYCLE_LEAF_MISSING")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def task(
    launch_agents: Path,
    label: str,
    state: Path,
    *,
    state_option: str,
    require_loaded: bool,
) -> None:
    """Verify the exact recurring task and optionally its loaded launchd job."""
    path = launch_agents / f"{label}.plist"
    require(path.is_file() and not path.is_symlink(), "CERTIFICATE_LIFECYCLE_TIMER_MISSING")
    value = plistlib.loads(path.read_bytes())
    arguments = value.get("ProgramArguments")
    require(
        value.get("Label") == label
        and value.get("RunAtLoad") is True
        and value.get("StartInterval") == 60
        and isinstance(arguments, list)
        and state_option in arguments
        and str(state.resolve()) in arguments,
        "CERTIFICATE_LIFECYCLE_TIMER_INVALID",
    )
    if require_loaded:
        result = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        require(
            result.returncode == 0 and str(state.resolve()).encode() in result.stdout,
            "CERTIFICATE_LIFECYCLE_TIMER_NOT_LOADED",
        )


def fresh(report: dict[str, Any], now: float, max_age: int) -> float:
    """Return the exact check time only for a fresh positive report."""
    checked_at = report.get("checked_at")
    require(
        report.get("healthy") is True
        and type(checked_at) in (int, float)
        and 0 <= now - float(checked_at) <= max_age,
        "CERTIFICATE_LIFECYCLE_RECEIPT_NOT_READY",
    )
    return float(checked_at)


def generic_lifecycles(
    state: Path,
    launch_agents: Path,
    now: float,
    max_age: int,
    *,
    require_loaded: bool,
) -> list[Lifecycle]:
    """Validate one consumer-agnostic owner approval and its exact receipt."""
    current, approval = read_json(state / "state.json"), read_json(
        state / "approval.json"
    )
    require(
        current.get("owner") == GENERIC_OWNER and current.get("revoked") is False,
        "CERTIFICATE_LIFECYCLE_STATE_INVALID",
    )
    timer = current.get("timer")
    require(isinstance(timer, dict), "CERTIFICATE_LIFECYCLE_TIMER_MISSING")
    task(
        launch_agents,
        str(timer.get("label")),
        state,
        state_option="--state",
        require_loaded=require_loaded,
    )
    report = read_json(Path(approval["report"]))
    checked_at = fresh(report, now, max_age)
    require(
        report.get("renewal_healthy") is True
        and report.get("ready") is True
        and report.get("consumer_owner") == approval.get("consumer_owner"),
        "CERTIFICATE_LIFECYCLE_RECEIPT_NOT_READY",
    )
    received = {
        item.get("id"): item
        for item in report.get("certificates", [])
        if isinstance(item, dict)
    }
    entries = approval.get("certificates")
    require(
        isinstance(entries, list) and len(received) == len(entries),
        "CERTIFICATE_LIFECYCLE_INVENTORY_MISMATCH",
    )
    result = []
    for entry in entries:
        item = received.get(entry.get("id"))
        certificate = Path(entry["certificate"]).resolve()
        certificate_hash = digest(certificate)
        require(
            isinstance(item, dict)
            and item.get("healthy") is True
            and item.get("certificate_sha256") == certificate_hash
            and type(item.get("certificate_expires_at")) in (int, float)
            and type(item.get("ca_expires_at")) in (int, float)
            and float(item["certificate_expires_at"]) - now
            > GENERIC_WINDOW_SECONDS
            and float(item["ca_expires_at"]) - now > GENERIC_WINDOW_SECONDS,
            "CERTIFICATE_LIFECYCLE_RECEIPT_NOT_READY",
        )
        result.append(
            Lifecycle(
                owner=str(approval["consumer_owner"]),
                state=state,
                certificate=certificate,
                certificate_sha256=certificate_hash,
                certificate_expires_at=float(item["certificate_expires_at"]),
                ca_expires_at=float(item["ca_expires_at"]),
                checked_at=checked_at,
            )
        )
    return result


def postgresql_lifecycle(
    state: Path,
    launch_agents: Path,
    now: float,
    max_age: int,
    *,
    require_loaded: bool,
) -> Lifecycle:
    """Validate the pre-existing PostgreSQL client renewal contract."""
    current, approval = read_json(state / "state.json"), read_json(
        state / "approval.json"
    )
    require(
        current.get("owner") == POSTGRESQL_OWNER and current.get("revoked") is False,
        "CERTIFICATE_LIFECYCLE_STATE_INVALID",
    )
    timer = current.get("timer")
    require(isinstance(timer, dict), "CERTIFICATE_LIFECYCLE_TIMER_MISSING")
    task(
        launch_agents,
        str(timer.get("label")),
        state,
        state_option="--state",
        require_loaded=require_loaded,
    )
    report = read_json(Path(approval["report"]))
    checked_at = fresh(report, now, max_age)
    certificate = (Path(approval["client_root"]) / approval["certificate"]).resolve()
    certificate_hash = digest(certificate)
    require(
        report.get("owner") == POSTGRESQL_OWNER
        and report.get("code") == "PG_RENEWAL_READY"
        and report.get("certificate_sha256") == certificate_hash
        and type(report.get("certificate_expires_at")) in (int, float)
        and type(report.get("ca_expires_at")) in (int, float)
        and float(report["certificate_expires_at"]) - now
        > GENERIC_WINDOW_SECONDS
        and float(report["ca_expires_at"]) - now > GENERIC_WINDOW_SECONDS,
        "CERTIFICATE_LIFECYCLE_RECEIPT_NOT_READY",
    )
    return Lifecycle(
        owner=str(approval["role"]),
        state=state,
        certificate=certificate,
        certificate_sha256=certificate_hash,
        certificate_expires_at=float(report["certificate_expires_at"]),
        ca_expires_at=float(report["ca_expires_at"]),
        checked_at=checked_at,
    )


def egress_lifecycle(
    state: Path,
    launch_agents: Path,
    now: float,
    max_age: int,
    *,
    require_loaded: bool,
) -> Lifecycle:
    """Validate an installed short-lived Egress leaf without changing its grant."""
    approval = read_json(state / "approval.json")
    label = "com.aelev.egress-renewal." + hashlib.sha256(
        str(state.resolve()).encode()
    ).hexdigest()[:16]
    task(
        launch_agents,
        label,
        state,
        state_option="--state-directory",
        require_loaded=require_loaded,
    )
    server = approval.get("kind") == "SERVER"
    status = Path(approval["status_directory"]) / (
        "server-status.json" if server else "status.json"
    )
    report = read_json(status)
    checked_at = fresh(report, now, max_age)
    certificate = (
        Path(approval["server_directory"]) / "server.pem"
        if server
        else Path(approval["client_directory"]) / "client.crt"
    ).resolve()
    certificate_hash = digest(certificate)
    require(
        report.get("code") == "READY"
        and type(report.get("certificate_expires_at")) in (int, float)
        and type(report.get("ca_expires_at")) in (int, float)
        and float(report["certificate_expires_at"]) - now > EGRESS_WINDOW_SECONDS
        and float(report["ca_expires_at"]) - now > GENERIC_WINDOW_SECONDS,
        "CERTIFICATE_LIFECYCLE_RECEIPT_NOT_READY",
    )
    return Lifecycle(
        owner=("public-egress-gateway" if server else str(approval["identity"])),
        state=state,
        certificate=certificate,
        certificate_sha256=certificate_hash,
        certificate_expires_at=float(report["certificate_expires_at"]),
        ca_expires_at=float(report["ca_expires_at"]),
        checked_at=checked_at,
    )


def discover_states(repo: Path) -> tuple[list[Path], list[Path]]:
    """Discover distributed approvals; historical and revoked state stays evidence."""
    generic = []
    postgresql = []
    for path in (repo / "services").rglob("state.json"):
        if "obsolete" in path.parts or not (path.parent / "approval.json").is_file():
            continue
        try:
            value = read_json(path)
        except (LifecycleError, OSError, ValueError, TypeError):
            continue
        if value.get("revoked") is True:
            continue
        if value.get("owner") == GENERIC_OWNER:
            generic.append(path.parent.resolve())
        elif value.get("owner") == POSTGRESQL_OWNER:
            postgresql.append(path.parent.resolve())
    return sorted(generic), sorted(postgresql)


def discover_egress_states(launch_agents: Path, repo: Path) -> list[Path]:
    """Use installed exact tasks, not retained old approvals, as active inventory."""
    result = []
    for path in launch_agents.glob("com.aelev.egress-renewal.*.plist"):
        if path.is_symlink() or not path.is_file():
            continue
        value = plistlib.loads(path.read_bytes())
        arguments = value.get("ProgramArguments", [])
        if "--state-directory" not in arguments:
            continue
        index = arguments.index("--state-directory")
        require(index + 1 < len(arguments), "CERTIFICATE_LIFECYCLE_TIMER_INVALID")
        state = Path(arguments[index + 1]).resolve()
        require(
            state.is_relative_to(repo.resolve()),
            "CERTIFICATE_LIFECYCLE_STATE_OUTSIDE_REPOSITORY",
        )
        result.append(state)
    return sorted(set(result))


def audit(
    repo: Path,
    launch_agents: Path,
    *,
    now: float | None = None,
    max_age: int = REPORT_MAX_AGE_SECONDS,
    require_loaded: bool = True,
) -> dict[str, Any]:
    """Audit every installed owner declaration and reject missing or duplicate cover."""
    require(1 <= max_age <= 3600, "CERTIFICATE_LIFECYCLE_MAX_AGE_INVALID")
    current_time = time.time() if now is None else now
    generic, postgresql = discover_states(repo)
    egress = discover_egress_states(launch_agents, repo)
    lifecycles: list[Lifecycle] = []
    for state in generic:
        try:
            lifecycles.extend(
                generic_lifecycles(
                    state,
                    launch_agents,
                    current_time,
                    max_age,
                    require_loaded=require_loaded,
                )
            )
        except LifecycleError as error:
            raise LifecycleError(
                str(error), str(state.relative_to(repo.resolve()))
            ) from None
    for state in postgresql:
        try:
            lifecycles.append(
                postgresql_lifecycle(
                    state,
                    launch_agents,
                    current_time,
                    max_age,
                    require_loaded=require_loaded,
                )
            )
        except LifecycleError as error:
            raise LifecycleError(
                str(error), str(state.relative_to(repo.resolve()))
            ) from None
    for state in egress:
        try:
            lifecycles.append(
                egress_lifecycle(
                    state,
                    launch_agents,
                    current_time,
                    max_age,
                    require_loaded=require_loaded,
                )
            )
        except LifecycleError as error:
            raise LifecycleError(
                str(error), str(state.relative_to(repo.resolve()))
            ) from None
    require(lifecycles, "CERTIFICATE_LIFECYCLE_INVENTORY_EMPTY")
    covered: dict[Path, Lifecycle] = {}
    for lifecycle in lifecycles:
        require(
            lifecycle.certificate not in covered,
            "CERTIFICATE_LIFECYCLE_DUPLICATE_OWNER",
        )
        covered[lifecycle.certificate] = lifecycle
    earliest_leaf = min(item.certificate_expires_at for item in lifecycles)
    earliest_ca = min(item.ca_expires_at for item in lifecycles)
    return {
        "code": "CERTIFICATE_LIFECYCLE_READY",
        "ready": True,
        "checked_at": current_time,
        "owners": len({item.owner for item in lifecycles}),
        "mechanisms": {
            "generic": len(generic),
            "postgresql": len(postgresql),
            "egress": len(egress),
        },
        "certificates": len(lifecycles),
        "earliest_leaf_expires_at": earliest_leaf,
        "earliest_ca_expires_at": earliest_ca,
    }


def main() -> None:
    """Expose one read-only deployment gate for the local installation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument(
        "--launch-agents", type=Path, default=Path.home() / "Library/LaunchAgents"
    )
    parser.add_argument("--max-age", type=int, default=REPORT_MAX_AGE_SECONDS)
    parser.add_argument("--without-loaded-check", action="store_true")
    args = parser.parse_args()
    try:
        result = audit(
            args.repo.resolve(),
            args.launch_agents.resolve(),
            max_age=args.max_age,
            require_loaded=not args.without_loaded_check,
        )
    except (LifecycleError, OSError, ValueError, KeyError, TypeError) as error:
        code = (
            str(error)
            if isinstance(error, LifecycleError)
            else "CERTIFICATE_LIFECYCLE_IO_OR_FORMAT_FAILED"
        )
        result: dict[str, Any] = {"code": code, "ready": False}
        if isinstance(error, LifecycleError) and error.subject is not None:
            result["subject"] = error.subject
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
