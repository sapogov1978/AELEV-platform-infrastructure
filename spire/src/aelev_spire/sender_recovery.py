"""Explicit operator recovery of this installation's exhausted sender only."""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any

from .files import atomic, read, writer
from .installation import Installer

OWNER = "infrastructure-spire"
SERVICE = "spire-telemetry-sender"


def apply_override(
    root: Path, service: dict[str, Any], *, role: str = "sender"
) -> None:
    """A normal repeat must not replenish an exhausted native restart budget."""
    if role not in {"sender", "observer"}:
        raise ValueError("SPIRE_OBSERVATION_RECOVERY_ROLE_INVALID")
    path = root / (role + "-recovery.json")
    if not path.exists():
        return
    value = read(path)
    if (
        value.get("owner") != OWNER
        or value.get("restart") != "no"
        or not re.fullmatch(r".+@sha256:[0-9a-f]{64}", value.get("image", ""))
    ):
        raise ValueError("SPIRE_SENDER_RECOVERY_STATE_INVALID")
    service["image"] = value["image"]
    service["restart"] = "no"
    service.setdefault("labels", {})["io.aelev." + role + "-recovery"] = value[
        "old_container"
    ]
    if role == "observer":
        return  # Keep the observer's own standalone heartbeat check unchanged.
    service.setdefault("healthcheck", {})["test"] = (
        ["CMD", "python", "-S", "/opt/aelev/delivery_health.py"]
        if value.get("local_liveness", False)
        else [
            "CMD",
            "python",
            "/opt/aelev/delivery.py",
            "alive",
            "--config",
            "/etc/aelev/sender.json",
        ]
    )


def require_running(installer: Installer, *, role: str = "sender") -> None:
    """An ordinary install repeat is not a new exhausted-budget recovery."""
    if role not in {"sender", "observer"}:
        raise ValueError("SPIRE_OBSERVATION_RECOVERY_ROLE_INVALID")
    service = SERVICE if role == "sender" else "spire-observer"
    path = installer.root / (role + "-recovery.json")
    if not path.exists():
        return
    value = read(path)
    record = json.loads(
        installer.docker("inspect", installer.compose_project + "-" + service + "-1")
    )[0]
    labels = record["Config"].get("Labels", {})
    if (
        record["State"]["Status"] != "running"
        or record["HostConfig"]["RestartPolicy"]["Name"] != "no"
        or labels.get("io.aelev." + role + "-recovery") != value["old_container"]
        or labels.get("io.aelev.installation") != installer.project
        or labels.get("io.aelev.owner") != OWNER
        or labels.get("com.docker.compose.project") != installer.compose_project
    ):
        raise ValueError("SPIRE_SENDER_RECOVERY_OPERATOR_REVIEW_REQUIRED")


def recover(
    installer: Installer,
    expected: str,
    image: str,
    backup_digest: str,
    *,
    dispatched: bool = False,
    update: bool = False,
    local_liveness: bool = False,
    role: str = "sender",
) -> None:
    """Persist intent before replacement; reconcile an interrupted exact retry."""
    if role not in {"sender", "observer"}:
        raise ValueError("SPIRE_OBSERVATION_RECOVERY_ROLE_INVALID")
    service_name = SERVICE if role == "sender" else "spire-observer"
    recovery_label = "io.aelev." + role + "-recovery"
    if role == "observer" and local_liveness:
        raise ValueError("SPIRE_OBSERVATION_RECOVERY_ROLE_INVALID")
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or not re.fullmatch(
        r".+@sha256:[0-9a-f]{64}", image
    ):
        raise ValueError("SPIRE_SENDER_RECOVERY_EXACT_RELEASE_REQUIRED")
    if not re.fullmatch(r"[0-9a-f]{64}", backup_digest):
        raise ValueError("SPIRE_SENDER_RECOVERY_BACKUP_REQUIRED")
    path = installer.root / "observation-compose.json"
    model = read(path)
    service = model["services"][service_name]
    labels = service.get("labels", {})
    if (
        model.get("name") != installer.project
        or labels.get("io.aelev.owner") != OWNER
        or labels.get("io.aelev.installation") != installer.project
    ):
        raise ValueError("SPIRE_SENDER_RECOVERY_OWNER_MISMATCH")
    intent_path = installer.root / (role + "-recovery.json")
    intent = {
        "owner": OWNER,
        "old_container": expected,
        "image": image,
        "backup_sha256": backup_digest,
        "restart": "no",
        "kind": "UPDATE" if update else "RECOVERY",
        "local_liveness": local_liveness,
    }
    previous = read(intent_path) if intent_path.exists() else None
    if previous is not None:
        previous.setdefault("kind", "RECOVERY")
        previous.setdefault("local_liveness", False)
    superseding = bool(
        update
        and previous is not None
        and previous.get("state") == "COMPLETE"
        and previous["old_container"] != expected
    )
    if (
        previous is not None
        and not superseding
        and any(previous.get(k) != v for k, v in intent.items())
    ):
        raise ValueError("SPIRE_SENDER_RECOVERY_INTENT_MISMATCH")
    record = json.loads(
        installer.docker(
            "inspect", installer.compose_project + "-" + service_name + "-1"
        )
    )[0]
    actual_labels = record["Config"].get("Labels", {})
    if any(
        actual_labels.get(k) != v
        for k, v in {
            "io.aelev.owner": OWNER,
            "io.aelev.installation": installer.project,
            "com.docker.compose.service": service_name,
            "com.docker.compose.project": installer.compose_project,
        }.items()
    ):
        raise ValueError("SPIRE_SENDER_RECOVERY_OWNER_MISMATCH")
    expected_mounts = {
        mount["target"]: (
            model["volumes"][mount["source"]]["name"],
            not mount.get("read_only", False),
        )
        for mount in service["volumes"]
    }
    actual_mounts = {
        mount["Destination"]: (mount.get("Name"), mount["RW"])
        for mount in record["Mounts"]
        if mount["Type"] == "volume"
    }
    if actual_mounts != expected_mounts or any(
        mount["Type"] not in {"volume", "tmpfs"} for mount in record["Mounts"]
    ):
        raise ValueError("SPIRE_SENDER_RECOVERY_MOUNTS_CHANGED")
    image_id = (
        installer.docker("image", "inspect", image, "--format", "{{.Id}}")
        .decode()
        .strip()
    )
    if record["Id"] == expected:
        if dispatched:
            raise ValueError("SPIRE_SENDER_RECOVERY_REPLACEMENT_NOT_CONFIRMED")
        if update:
            if previous is None:
                raise ValueError("SPIRE_SENDER_RECOVERY_PREDECESSOR_REQUIRED")
            predecessor = (
                previous["old_container"] if superseding else previous["predecessor"]
            )
            if (
                record["State"]["Status"] != "exited"
                or record["HostConfig"]["RestartPolicy"]["Name"] != "no"
                or actual_labels.get(recovery_label) != predecessor
            ):
                raise ValueError("SPIRE_SENDER_RECOVERY_STOPPED_UPDATE_REQUIRED")
        elif (
            record["State"]["Status"] != "exited"
            or record["RestartCount"] != 3
            or record["HostConfig"]["RestartPolicy"]
            != {"Name": "on-failure", "MaximumRetryCount": 3}
        ):
            raise ValueError("SPIRE_SENDER_RECOVERY_EXHAUSTED_STOP_REQUIRED")
    elif previous is None or record["Image"] != image_id:
        raise ValueError("SPIRE_SENDER_RECOVERY_CONTAINER_CHANGED")
    else:
        if (
            record["HostConfig"]["RestartPolicy"]["Name"] != "no"
            or record["State"]["Status"] != "running"
            or actual_labels.get(recovery_label) != expected
        ):
            raise ValueError("SPIRE_SENDER_RECOVERY_OPERATOR_REVIEW_REQUIRED")
        # Lost acknowledgement: inspect the sole replacement, never start again.
        if record["State"].get("Health", {}).get("Status") != "healthy":
            raise ValueError("SPIRE_SENDER_RECOVERY_NOT_READY")
        atomic(intent_path, {**previous, "state": "COMPLETE"})
        return
    if previous is None or superseding:
        # Old installers compare this marker exactly. They must fail closed,
        # not silently regenerate on-failure:3 over an exhausted installation.
        owner_path = installer.root / "owner.json"
        atomic(owner_path, {**read(owner_path), role + "_recovery_schema": 1})
        if superseding:
            assert previous is not None
            atomic(
                installer.root
                / (role + "-recovery-" + previous["old_container"] + ".json"),
                previous,
            )
        atomic(installer.root / (role + "-before-" + expected + ".json"), model)
        atomic(
            intent_path,
            {
                **intent,
                "state": "PREPARED",
                "old_restart_count": record["RestartCount"],
                "old_restart_policy": record["HostConfig"]["RestartPolicy"],
                "exhausted_restart_count": 3,
                **(
                    {"predecessor": previous["old_container"]}
                    if superseding and previous is not None
                    else {}
                ),
            },
        )
    apply_override(installer.root, service, role=role)
    atomic(path, model)
    installer.docker(
        "compose",
        "--env-file",
        "/dev/null",
        "-p",
        installer.compose_project,
        "-f",
        str(path),
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--pull",
        "never",
        "--wait",
        "--wait-timeout",
        "90",
        service_name,
    )
    # Verify the replacement through the same reconciliation path.
    recover(
        installer,
        expected,
        image,
        backup_digest,
        dispatched=True,
        update=update,
        local_liveness=local_liveness,
        role=role,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--daemon-id", required=True)
    parser.add_argument("--expected-container", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--backup-sha256", required=True)
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--local-liveness", action="store_true")
    parser.add_argument("--role", choices=("sender", "observer"), default="sender")
    parser.add_argument("--root", type=Path, default=Path("/installation"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", args.project):
            raise ValueError("SPIRE_INSTALL_PROJECT_INVALID")
        # Reuse only the bounded Docker transport, not a server release update.
        installer = object.__new__(Installer)
        installer.project, installer.root = args.project, args.root
        if (
            installer.docker("info", "--format", "{{.ID}}").decode().strip()
            != args.daemon_id
        ):
            raise ValueError("SPIRE_INSTALL_DAEMON_MISMATCH")
        marker = {
            "owner": OWNER,
            "role": "installer",
            "project": args.project,
            "daemon_id": args.daemon_id,
        }
        actual_marker = read(args.root / "owner.json")
        for role in ("sender", "observer"):
            if (
                type(actual_marker.get(role + "_recovery_schema")) is int
                and actual_marker[role + "_recovery_schema"] == 1
            ):
                marker[role + "_recovery_schema"] = 1
        if actual_marker != marker:
            raise ValueError("SPIRE_SENDER_RECOVERY_OWNER_MISMATCH")
        with writer(args.root, "installation.lock"):
            installer.bind_compose_project()
            recover(
                installer,
                args.expected_container,
                args.image,
                args.backup_sha256,
                update=args.update,
                local_liveness=args.local_liveness,
                role=args.role,
            )
            atomic(
                args.root / "owner.json", {**marker, args.role + "_recovery_schema": 1}
            )
        logging.info(json.dumps({"code": "SPIRE_" + args.role.upper() + "_RECOVERED"}))
    except (ValueError, OSError, KeyError, TypeError) as error:
        code = str(error)
        if not re.fullmatch(r"SPIRE_[A-Z0-9_]+", code):
            code = "SPIRE_SENDER_RECOVERY_FAILED"
        if args.role == "observer":
            code = code.replace("SPIRE_SENDER_", "SPIRE_OBSERVER_", 1)
        logging.error(json.dumps({"code": code}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
