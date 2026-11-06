"""Bounded container adapter for the existing operator, not another controller."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import recovery

LIMIT = 512 * 1024
FINGERPRINT = {"id", "image", "project", "service", "config_hash"}
ROOT = Path("/var/lib/aelev-recovery")


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def document(raw: bytes) -> dict[str, Any]:
    require(len(raw) <= LIMIT, "RECOVERY_REQUEST_TOO_LARGE")
    value: Any = json.loads(raw)
    require(isinstance(value, dict), "RECOVERY_REQUEST_INVALID")
    return cast(dict[str, Any], value)


def request(raw: bytes) -> dict[str, Any]:
    """Accept exact predecessor proofs, never names alone or a shell command."""
    value = document(raw)
    require(
        set(value) == {"schema_version", "daemon_id", "action", "targets"}
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["action"]
        in {
            "status",
            "suspend",
            "replace",
            "relocate",
            "release-relocation",
            "register",
        }
        and isinstance(value["daemon_id"], str)
        and re.fullmatch(r"[A-Za-z0-9:_.-]{1,128}", value["daemon_id"]) is not None
        and isinstance(value["targets"], list)
        and 1 <= len(value["targets"]) <= 64,
        "RECOVERY_REQUEST_INVALID",
    )
    names: set[str] = set()
    registrations: set[tuple[str, str]] = set()
    identifiers: set[str] = set()
    for target in value["targets"]:
        fields = {"name", "expected"}
        if value["action"] == "replace":
            fields |= {"previous_native_count", "replacement_image"}
        elif value["action"] == "relocate":
            fields |= {"previous_native_count", "replacement_name", "replacement"}
        elif value["action"] == "release-relocation":
            fields |= {"relocation_proof"}
        require(
            isinstance(target, dict) and set(target) == fields, "RECOVERY_SCOPE_INVALID"
        )
        name, expected = target["name"], target["expected"]
        require(
            isinstance(name, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,253}", name) is not None
            and name not in names
            and isinstance(expected, dict)
            and set(expected) == FINGERPRINT
            and all(isinstance(v, str) for v in expected.values()),
            "RECOVERY_SCOPE_INVALID",
        )
        require(
            re.fullmatch(r"[a-f0-9]{64}", expected["id"]) is not None
            and re.fullmatch(r"sha256:[a-f0-9]{64}", expected["image"]) is not None
            and re.fullmatch(r"[a-f0-9]{64}", expected["config_hash"]) is not None
            and all(
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", expected[k])
                is not None
                for k in ("project", "service")
            ),
            "RECOVERY_SCOPE_INVALID",
        )
        if value["action"] == "replace":
            require(
                type(target["previous_native_count"]) is int
                and target["previous_native_count"] >= 0
                and isinstance(target["replacement_image"], str)
                and re.fullmatch(r"sha256:[a-f0-9]{64}", target["replacement_image"])
                is not None,
                "RECOVERY_REPLACEMENT_INVALID",
            )
        elif value["action"] == "relocate":
            replacement_name = target["replacement_name"]
            replacement = target["replacement"]
            require(
                type(target["previous_native_count"]) is int
                and target["previous_native_count"] >= 0
                and isinstance(replacement_name, str)
                and re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,253}", replacement_name
                )
                is not None
                and replacement_name not in names
                and isinstance(replacement, dict)
                and set(replacement) == FINGERPRINT
                and all(isinstance(v, str) for v in replacement.values())
                and re.fullmatch(r"[a-f0-9]{64}", replacement["id"]) is not None
                and re.fullmatch(
                    r"sha256:[a-f0-9]{64}", replacement["image"]
                )
                is not None
                and re.fullmatch(r"[a-f0-9]{64}", replacement["config_hash"])
                is not None
                and all(
                    re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", replacement[k])
                    is not None
                    for k in ("project", "service")
                ),
                "RECOVERY_RELOCATION_INVALID",
            )
            names.add(replacement_name)
        elif value["action"] == "release-relocation":
            require(
                isinstance(target["relocation_proof"], str)
                and re.fullmatch(r"[a-f0-9]{64}", target["relocation_proof"])
                is not None,
                "RECOVERY_RELOCATION_RELEASE_INVALID",
            )
        if value["action"] == "register":
            key = (expected["project"], expected["service"])
            require(
                key not in registrations and expected["id"] not in identifiers,
                "RECOVERY_REGISTRATION_DUPLICATE_TARGET",
            )
            registrations.add(key)
            identifiers.add(expected["id"])
        names.add(name)
    return value


def transport(state: dict[str, Any], *arguments: str) -> bytes:
    """Remap only Docker transport; leave the journal's host paths untouched."""
    del state
    return recovery.execute(
        ["/usr/local/bin/docker", "--host", "unix:///var/run/docker.sock", *arguments]
    )


def state(root: Path) -> dict[str, Any]:
    require(
        not any(p.is_symlink() for p in (root, *root.parents))
        and root.is_dir()
        and root.is_dir(),
        "RECOVERY_PRIVATE_JOURNAL_REQUIRED",
    )
    recovery.private(root)
    descriptor = os.open(
        root / "state.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and info.st_size <= 16 * 1024 * 1024,
            "RECOVERY_JOURNAL_INVALID",
        )
        value: Any = json.load(stream)
    require(
        isinstance(value, dict)
        and value.get("schema_version") in {1, 2}
        and isinstance(value.get("targets"), dict),
        "RECOVERY_JOURNAL_INVALID",
    )
    return cast(dict[str, Any], value)


def preflight(
    current: dict[str, Any], value: dict[str, Any], *, native: bool = False
) -> None:
    require(current["daemon"] == value["daemon_id"], "RECOVERY_DAEMON_MISMATCH")
    require(
        (recovery.docker if native else transport)(
            current, "info", "--format", "{{.ID}}"
        )
        .decode()
        .strip()
        == current["daemon"],
        "RECOVERY_DAEMON_MISMATCH",
    )
    for row in value["targets"]:
        target = current["targets"].get(row["name"])
        completed_relocation = False
        if value["action"] == "relocate" and target is None:
            target = current["targets"].get(row["replacement_name"])
            relocation = target.get("relocation", {}) if isinstance(target, dict) else {}
            completed_relocation = bool(
                relocation.get("phase") == "COMPLETE"
                and relocation.get("previous_name") == row["name"]
                and relocation.get("previous") == row["expected"]
                and relocation.get("replacement_name") == row["replacement_name"]
                and relocation.get("replacement") == row["replacement"]
                and relocation.get("previous_native_count")
                == row["previous_native_count"]
                and target.get("expected") == row["replacement"]
            )
        if value["action"] == "register":
            read = recovery.docker if native else transport
            observed = json.loads(read(current, "inspect", row["expected"]["id"]))
            require(
                isinstance(observed, list) and len(observed) == 1,
                "RECOVERY_REGISTRATION_TARGET_INVALID",
            )
            recovery.registration_check(
                current, row["name"], row["expected"], observed[0]
            )
            continue
        require(isinstance(target, dict), "RECOVERY_TARGET_NOT_ENROLLED")
        same = completed_relocation or target["expected"] == row["expected"]
        if value["action"] == "replace" and not same:
            previous = target.get("replacement_from", {})
            same = (
                previous
                == {
                    "id": row["expected"]["id"],
                    "native_count": row["previous_native_count"],
                }
                and target["expected"]["image"] == row["replacement_image"]
                and all(
                    target["expected"][k] == row["expected"][k]
                    for k in ("project", "service")
                )
            )
        if value["action"] == "relocate" and not same:
            pending = target.get("relocation", {})
            same = (
                pending.get("phase") == "POLICY_PENDING"
                and pending.get("previous_name") == row["name"]
                and pending.get("previous") == row["expected"]
                and pending.get("replacement_name") == row["replacement_name"]
                and pending.get("replacement") == row["replacement"]
                and pending.get("previous_native_count")
                == row["previous_native_count"]
                and target.get("expected") == row["replacement"]
            )
        require(same, "RECOVERY_TARGET_CHANGED")
        if value["action"] == "relocate":
            read = recovery.docker if native else transport
            observed_raw = json.loads(
                read(current, "inspect", row["replacement_name"])
            )
            require(
                isinstance(observed_raw, list) and len(observed_raw) == 1,
                "RECOVERY_RELOCATION_TARGET_CHANGED",
            )
            observed = observed_raw[0]
            require(
                recovery.fingerprint(observed) == row["replacement"]
                and observed.get("Name") == "/" + row["replacement_name"],
                "RECOVERY_RELOCATION_TARGET_CHANGED",
            )


def result(current: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    def result_name(row: dict[str, Any]) -> str:
        return row["replacement_name"] if value["action"] == "relocate" else row["name"]

    return {
        "schema_version": 1,
        "daemon_id": current["daemon"],
        "targets": {
            result_name(row): {
                **{
                    key: current["targets"][result_name(row)][key]
                    for key in (
                        "expected",
                        "attempts",
                        "native_count",
                        "policy_limit",
                        "blocked",
                        "code",
                    )
                },
                "target_digest": hashlib.sha256(
                    json.dumps(
                        current["targets"][result_name(row)], sort_keys=True
                    ).encode()
                ).hexdigest(),
                "automatic_recovery": current["targets"][result_name(row)]
                .get("automatic", {})
                .get("phase")
                == "ACTIVE",
                **(
                    {
                        "registration_complete": current["targets"][row["name"]]
                        .get("registration", {})
                        .get("phase")
                        == "COMPLETE",
                    }
                    if value["action"] == "register"
                    else {}
                ),
            }
            for row in value["targets"]
        },
    }


def run(
    root: Path, value: dict[str, Any], *, host_timer_quiesced: bool = False
) -> dict[str, Any]:
    """Delegate writes only within a host-held, quiesced maintenance window."""
    if value["action"] == "status":
        current = state(root)
        preflight(current, value)
        return result(current, value)
    # Desktop flock is not shared with macOS. A container lock alone is unsafe.
    require(host_timer_quiesced, "RECOVERY_HOST_MAINTENANCE_REQUIRED")
    descriptor = os.open(root / ".lock", os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r+") as lock:
        require(stat.S_ISREG(os.fstat(lock.fileno()).st_mode), "RECOVERY_LOCK_INVALID")
        recovery.lock(lock)
        current = state(root)
        preflight(current, value)
        recovery.docker = transport
        apply(root, value)
        return result(state(root), value)


def apply(root: Path, value: dict[str, Any]) -> None:
    """The caller holds the provider's barrier and has validated all targets."""
    for row in value["targets"]:
        if value["action"] == "register":
            recovery.register_exact(root, row["name"], row["expected"])
        elif value["action"] == "suspend":
            recovery.suspend(root, row["name"])
        elif value["action"] == "replace":
            recovery.replace(
                root,
                row["name"],
                row["expected"]["id"],
                row["previous_native_count"],
                row["replacement_image"],
            )
        elif value["action"] == "relocate":
            recovery.relocate(
                root,
                row["name"],
                row["expected"]["id"],
                row["previous_native_count"],
                row["replacement_name"],
                row["replacement"],
            )
        elif value["action"] == "release-relocation":
            recovery.release_relocation(
                root,
                row["name"],
                row["expected"]["id"],
                row["relocation_proof"],
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-timer-quiesced", action="store_true")
    args = parser.parse_args()
    try:
        # Docker create/start may keep an interactive stdin open after a
        # client has sent its input. One bounded JSON line must not wait for EOF.
        value = request(sys.stdin.buffer.readline(LIMIT + 1))
        answer = run(ROOT, value, host_timer_quiesced=args.host_timer_quiesced)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ) as error:
        code = str(error)
        if re.fullmatch(r"(?:RECOVERY_|[A-Z])[A-Z0-9_]{1,96}", code) is None:
            code = "RECOVERY_OPERATOR_UNCONFIRMED"
        sys.stderr.write(json.dumps({"code": code}) + "\n")
        raise SystemExit(1) from None
    sys.stdout.write(json.dumps(answer, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
