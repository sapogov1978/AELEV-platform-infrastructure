"""Recovery replaces only the exhausted owned sender and preserves its budget."""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from aelev_spire.files import atomic, read, reserve
from aelev_spire.installation import Installer
from aelev_spire.sender_recovery import (
    SERVICE,
    apply_override,
    main,
    recover,
    require_running,
)

IMAGE = "aelev/spire-sender@sha256:" + "b" * 64
OLD = "a" * 64


def fixture(
    tmp_path: Path, role: str = "sender"
) -> tuple[Installer, dict[str, Any], list[tuple[str, ...]]]:
    service_name = SERVICE if role == "sender" else "spire-observer"
    other = "spire-observer" if role == "sender" else SERVICE
    recovery_label = "io.aelev." + role + "-recovery"
    installer = object.__new__(Installer)
    installer.project, installer.root = "fixture-spire", tmp_path
    atomic(
        tmp_path / "owner.json",
        {
            "owner": "infrastructure-spire",
            "role": "installer",
            "project": installer.project,
            "daemon_id": "fixture",
        },
    )
    labels = {
        "io.aelev.owner": "infrastructure-spire",
        "io.aelev.installation": installer.project,
    }
    model = {
        "name": installer.project,
        "services": {
            service_name: {
                "image": "old",
                "restart": "on-failure:3",
                "labels": labels,
                "volumes": [{"source": "data", "target": "/var/lib/delivery"}],
            },
            other: {"image": "unchanged"},
        },
        "volumes": {"data": {"name": "own-data"}},
    }
    atomic(tmp_path / "observation-compose.json", model)
    record: dict[str, Any] = {
        "Id": OLD,
        "Image": "sha256:old",
        "RestartCount": 3,
        "Config": {
            "Labels": {
                **labels,
                "com.docker.compose.service": service_name,
                "com.docker.compose.project": installer.project,
            }
        },
        "Mounts": [
            {
                "Type": "volume",
                "Name": "own-data",
                "Destination": "/var/lib/delivery",
                "RW": True,
            }
        ],
        "HostConfig": {"RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": 3}},
        "State": {"Status": "exited"},
    }
    calls: list[tuple[str, ...]] = []

    def docker(*args: str, payload: bytes | None = None) -> bytes:
        assert payload is None
        calls.append(args)
        if args[0] == "inspect":
            return json.dumps([record]).encode()
        if args[:2] == ("image", "inspect"):
            return b"sha256:new"
        assert args[0] == "compose"
        assert args[-1] == service_name and "--no-deps" in args
        assert (
            read(tmp_path / (role + "-recovery.json"))["exhausted_restart_count"] == 3
        )
        assert (
            read(tmp_path / "observation-compose.json")["services"][service_name][
                "restart"
            ]
            == "no"
        )
        replacement = "c" if sum(c[0] == "compose" for c in calls) == 1 else "e"
        record.update(
            {"Id": replacement * 64, "Image": "sha256:new", "RestartCount": 0}
        )
        selected = read(tmp_path / "observation-compose.json")["services"][service_name]
        record["Config"]["Labels"][recovery_label] = selected["labels"][recovery_label]
        record["HostConfig"]["RestartPolicy"] = {"Name": "no", "MaximumRetryCount": 0}
        record["State"] = {"Status": "running", "Health": {"Status": "healthy"}}
        return b""

    installer.docker = docker
    return installer, record, calls


def test_recovery_and_lost_ack_do_not_repeat_start(tmp_path: Path) -> None:
    installer, _, calls = fixture(tmp_path)
    recover(installer, OLD, IMAGE, "d" * 64)
    recover(installer, OLD, IMAGE, "d" * 64)
    assert len([c for c in calls if c[0] == "compose"]) == 1
    state = read(tmp_path / "sender-recovery.json")
    assert state["state"] == "COMPLETE" and state["old_restart_count"] == 3
    assert read(tmp_path / "owner.json")["sender_recovery_schema"] == 1
    model = read(tmp_path / "observation-compose.json")
    assert model["services"]["spire-observer"] == {"image": "unchanged"}
    service = {"image": "old", "restart": "on-failure:3"}
    apply_override(tmp_path, service)
    assert service == {
        "image": IMAGE,
        "restart": "no",
        "labels": {"io.aelev.sender-recovery": OLD},
        "healthcheck": {
            "test": [
                "CMD",
                "python",
                "/opt/aelev/delivery.py",
                "alive",
                "--config",
                "/etc/aelev/sender.json",
            ]
        },
    }
    require_running(installer)
    with pytest.raises(ValueError, match="INTENT_MISMATCH"):
        recover(installer, OLD, IMAGE, "e" * 64)


@pytest.mark.parametrize("role", ["sender", "observer"])
def test_recovery_entrypoint_preserves_native_no_marker_and_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    installer, _record, calls = fixture(tmp_path, role)
    selected = ["spire-server"]
    atomic(
        tmp_path / "owner.json",
        {**read(tmp_path / "owner.json"), "native_restart_no_roles": selected},
    )

    def transport(self: Installer, *args: str, payload: bytes | None = None) -> bytes:
        if args == ("info", "--format", "{{.ID}}"):
            return b"fixture"
        return installer.docker(*args, payload=payload)

    def bind(self: Installer) -> None:
        assert self.project == installer.project

    monkeypatch.setattr(Installer, "docker", transport)
    monkeypatch.setattr(Installer, "bind_compose_project", bind)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sender-recovery",
            "--project",
            installer.project,
            "--daemon-id",
            "fixture",
            "--root",
            str(tmp_path),
            "--expected-container",
            OLD,
            "--image",
            IMAGE,
            "--backup-sha256",
            "d" * 64,
            "--role",
            role,
        ],
    )
    main()
    main()
    assert read(tmp_path / "owner.json")["native_restart_no_roles"] == selected
    assert read(tmp_path / "owner.json")[role + "_recovery_schema"] == 1
    state = read(tmp_path / (role + "-recovery.json"))
    assert state["state"] == "COMPLETE" and state["old_restart_count"] == 3
    assert len([call for call in calls if call[0] == "compose"]) == 1


def test_observer_reuses_exact_recovery_without_changing_sender_or_health(
    tmp_path: Path,
) -> None:
    installer, record, calls = fixture(tmp_path, "observer")
    recover(installer, OLD, IMAGE, "d" * 64, role="observer")
    recover(installer, OLD, IMAGE, "d" * 64, role="observer")
    assert len([c for c in calls if c[0] == "compose"]) == 1
    state = read(tmp_path / "observer-recovery.json")
    assert state["state"] == "COMPLETE" and state["old_restart_count"] == 3
    assert read(tmp_path / "owner.json")["observer_recovery_schema"] == 1
    model = read(tmp_path / "observation-compose.json")
    assert model["services"][SERVICE] == {"image": "unchanged"}
    check = ["CMD", "python", "-S", "observer-health.py"]
    service: dict[str, Any] = {
        "image": "old",
        "restart": "on-failure:3",
        "healthcheck": {"test": check},
    }
    apply_override(tmp_path, service, role="observer")
    assert service["restart"] == "no" and service["healthcheck"]["test"] == check
    require_running(installer, role="observer")
    record["State"]["Status"] = "exited"
    with pytest.raises(ValueError, match="OPERATOR_REVIEW_REQUIRED"):
        require_running(installer, role="observer")
    with pytest.raises(ValueError, match="ROLE_INVALID"):
        recover(installer, OLD, IMAGE, "d" * 64, role="server")


@pytest.mark.parametrize("role", ["sender", "observer"])
@pytest.mark.parametrize("defect", ["owner", "mount", "running", "budget", "container"])
def test_unsafe_targets_never_dispatch(tmp_path: Path, defect: str, role: str) -> None:
    installer, record, calls = fixture(tmp_path, role)
    if defect == "owner":
        record["Config"]["Labels"]["io.aelev.owner"] = "foreign"
    elif defect == "mount":
        record["Mounts"][0]["Name"] = "foreign"
    elif defect == "running":
        record["State"]["Status"] = "running"
    elif defect == "budget":
        record["RestartCount"] = 2
    else:
        record["Id"] = "f" * 64
    with pytest.raises(ValueError):
        recover(installer, OLD, IMAGE, "d" * 64, role=role)
    assert not any(c[0] == "compose" for c in calls)
    assert not (tmp_path / (role + "-recovery.json")).exists()


def test_explicit_sender_update_preserves_exhaustion_chain(tmp_path: Path) -> None:
    installer, record, calls = fixture(tmp_path)
    recover(installer, OLD, IMAGE, "d" * 64)
    with pytest.raises(ValueError, match="STOPPED_UPDATE_REQUIRED"):
        recover(installer, "c" * 64, IMAGE, "f" * 64, update=True, local_liveness=True)
    record["State"]["Status"] = "exited"
    recover(installer, "c" * 64, IMAGE, "f" * 64, update=True, local_liveness=True)
    recover(installer, "c" * 64, IMAGE, "f" * 64, update=True, local_liveness=True)
    state = read(tmp_path / "sender-recovery.json")
    assert state["exhausted_restart_count"] == 3
    assert state["predecessor"] == OLD
    assert state["old_restart_policy"]["Name"] == "no"
    assert (
        read(tmp_path / ("sender-recovery-" + OLD + ".json"))["old_restart_count"] == 3
    )
    service = read(tmp_path / "observation-compose.json")["services"][SERVICE]
    assert service["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-S",
        "/opt/aelev/delivery_health.py",
    ]
    assert len([c for c in calls if c[0] == "compose"]) == 2


def test_failed_replacement_requires_operator_not_a_second_start(
    tmp_path: Path,
) -> None:
    installer, record, calls = fixture(tmp_path)
    recover(installer, OLD, IMAGE, "d" * 64)
    record["State"]["Status"] = "exited"
    calls.clear()
    with pytest.raises(ValueError, match="OPERATOR_REVIEW_REQUIRED"):
        recover(installer, OLD, IMAGE, "d" * 64)
    with pytest.raises(ValueError, match="OPERATOR_REVIEW_REQUIRED"):
        require_running(installer)
    assert not any(c[0] == "compose" for c in calls)


def test_old_installer_cannot_regenerate_a_fresh_restart_budget(tmp_path: Path) -> None:
    installer, _, _ = fixture(tmp_path)
    old_marker = read(tmp_path / "owner.json")
    recover(installer, OLD, IMAGE, "d" * 64)
    with pytest.raises(ValueError, match="STATE_OWNER_MISMATCH"):
        reserve(tmp_path, old_marker)
    reserve(tmp_path, {**old_marker, "sender_recovery_schema": 1})
