"""Logical ownership survives an explicitly selected physical coordinator."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_composition
from test_observation import plan
from test_sender_recovery import IMAGE, OLD
from test_sender_recovery import fixture as sender_fixture

from aelev_spire import composition, observation_setup
from aelev_spire.files import atomic, read
from aelev_spire.installation import Installer
from aelev_spire.sender_recovery import recover, require_running

LOGICAL = "fixture-spire"
PHYSICAL = "fixture-infrastructure"
installed_fixture = test_composition.installed


def operator(rows: list[dict[str, Any]]) -> tuple[Installer, list[tuple[str, ...]]]:
    value = object.__new__(Installer)
    value.project = LOGICAL
    calls: list[tuple[str, ...]] = []

    def docker(*args: str, payload: bytes | None = None) -> bytes:
        assert payload is None
        calls.append(args)
        if args[0] == "ps":
            assert "label=io.aelev.installation=" + LOGICAL in args
            return b"\n".join(str(r["Id"]).encode() for r in rows)
        assert args[0] == "inspect"
        return json.dumps(rows).encode()

    value.docker = docker
    return value, calls


def runtime(role: str, number: int) -> dict[str, Any]:
    return {
        "Id": str(number) * 64,
        "Config": {
            "Labels": {
                "io.aelev.owner": "infrastructure-spire",
                "io.aelev.installation": LOGICAL,
                "com.docker.compose.service": role,
                "com.docker.compose.project": PHYSICAL,
            }
        },
    }


def test_coordinator_is_derived_only_from_owned_runtime() -> None:
    rows = [runtime("spire-server", 1), runtime("spire-observer", 2)]
    rows.append(runtime("spire-status-provision", 3))
    rows[-1]["Config"]["Labels"]["com.docker.compose.project"] = LOGICAL
    value, calls = operator(rows)
    value.bind_compose_project()
    assert value.compose_project == PHYSICAL and value.project == LOGICAL
    assert {c[0] for c in calls} == {"ps", "inspect"}


def test_initial_install_keeps_its_logical_project() -> None:
    value, calls = operator([])
    value.bind_compose_project()
    assert value.compose_project == LOGICAL and len(calls) == 1


def test_initial_install_uses_explicit_canonical_physical_project() -> None:
    value, calls = operator([])
    value.bind_compose_project("aelev_infrastructure_spire_fixture")
    assert value.compose_project == "aelev_infrastructure_spire_fixture"
    assert value.project == LOGICAL and len(calls) == 1


def test_existing_runtime_remains_the_only_coordinator() -> None:
    value, _calls = operator([runtime("spire-agent", 1)])
    value.bind_compose_project("aelev_infrastructure_spire_fixture")
    assert value.compose_project == PHYSICAL


@pytest.mark.parametrize(
    "damage", ["owner", "installation", "project", "mixed", "duplicate"]
)
def test_ambiguous_or_foreign_coordinator_is_rejected(damage: str) -> None:
    rows = [runtime("spire-server", 1), runtime("spire-observer", 2)]
    labels = rows[0]["Config"]["Labels"]
    if damage in {"owner", "installation"}:
        labels["io.aelev." + damage] = "foreign"
    elif damage == "project":
        labels["com.docker.compose.project"] = "../foreign"
    elif damage == "mixed":
        labels["com.docker.compose.project"] = LOGICAL
    else:
        rows.append(runtime("spire-server", 3))
    value, _calls = operator(rows)
    with pytest.raises(ValueError, match="COORDINATOR_"):
        value.bind_compose_project()


@pytest.mark.parametrize("role", ["sender", "observer"])
def test_recovery_uses_physical_name_and_preserves_logical_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    value, record, calls = sender_fixture(tmp_path, role)
    monkeypatch.setattr(value, "_compose_project", PHYSICAL, raising=False)
    record["Config"]["Labels"]["com.docker.compose.project"] = PHYSICAL
    recover(value, OLD, IMAGE, "d" * 64, role=role)
    recover(value, OLD, IMAGE, "d" * 64, role=role)
    require_running(value, role=role)
    commands = [c for c in calls if c[0] == "compose"]
    assert len(commands) == 1 and commands[0][commands[0].index("-p") + 1] == PHYSICAL
    assert all(c[1].startswith(PHYSICAL + "-") for c in calls if c[0] == "inspect")
    assert read(tmp_path / "owner.json")["project"] == LOGICAL
    model = read(tmp_path / "observation-compose.json")
    assert model["name"] == LOGICAL and model["volumes"]["data"]["name"] == "own-data"
    assert read(tmp_path / (role + "-recovery.json"))["old_restart_count"] == 3
    assert record["HostConfig"]["RestartPolicy"] == {
        "Name": "no",
        "MaximumRetryCount": 0,
    }


def test_server_apply_scopes_foreign_shared_project_and_keeps_volume_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = object.__new__(Installer)
    value.project, value.root, value.image, value.image_id = (
        LOGICAL,
        tmp_path,
        "fixed",
        "fixed-id",
    )
    value.update = False
    monkeypatch.setattr(value, "_compose_project", PHYSICAL, raising=False)
    values_seen: list[dict[str, str]] = []
    volume_names: list[str] = []

    def volume(name: str, *, ephemeral: bool = False) -> str:
        volume_names.append(name)
        return LOGICAL + "_" + name

    def resolve(file: str, values: dict[str, str]) -> dict[str, Any]:
        assert file == "compose.yml"
        values_seen.append(values)
        return {
            "name": LOGICAL,
            "services": {"spire-server": {}},
            "volumes": {"state": {"name": LOGICAL + "_server"}},
            "networks": {"access": {"name": LOGICAL + "-access"}},
        }

    calls: list[tuple[str, ...]] = []

    def docker(*args: str, payload: bytes | None = None) -> bytes:
        calls.append(args)
        if args[0] == "ps":
            assert "label=io.aelev.installation=" + LOGICAL in args
            assert "label=com.docker.compose.project=" + PHYSICAL in args
            return b""
        if args[0] == "compose":
            assert args[args.index("-p") + 1] == PHYSICAL
            return b"a" * 64 if "ps" in args else b""
        if args[0] == "inspect":
            return b"healthy" if "Health.Status" in args[2] else b"running"
        assert args[0] == "exec"
        return json.dumps(
            {
                "owner": "infrastructure-spire",
                "ready": True,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ).encode()

    value.volume, value.resolve, value.docker = volume, resolve, docker
    value.apply(
        "server",
        {
            "owner": "infrastructure-spire",
            "schema_version": 1,
            "environment": "test",
            "trust_domain": "test.example",
        },
        {},
        None,
    )
    assert (
        volume_names
        and values_seen[0]["AELEV_INFRASTRUCTURE_SPIRE_COMPOSE_PROJECT"] == LOGICAL
    )
    model = read(tmp_path / "compose.json")
    assert model["name"] == LOGICAL
    assert (
        model["services"]["spire-server"]["labels"]["io.aelev.installation"] == LOGICAL
    )
    assert model["volumes"]["state"] == {"name": LOGICAL + "_server", "external": True}


def test_observation_commands_use_physical_coordinator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = object.__new__(Installer)
    value.project, value.root, value.image, value.image_id = (
        LOGICAL,
        tmp_path,
        "fixed",
        "fixed-id",
    )
    value.host_os, value.update = "Linux", False
    monkeypatch.setattr(value, "_compose_project", PHYSICAL, raising=False)
    atomic(
        tmp_path / "deployment.json",
        {"role": "server", "plan": {"environment": "test"}, "image_id": "fixed-id"},
    )

    def documents(_plan: dict[str, Any]) -> dict[str, bytes]:
        return {}

    def volume(name: str, *, ephemeral: bool = False) -> str:
        return LOGICAL + "_" + name

    monkeypatch.setattr(observation_setup, "documents", documents)
    monkeypatch.setattr(observation_setup, "SECRET_FILES", {})
    value.volume = volume
    model: dict[str, Any] = {
        "name": LOGICAL,
        "services": {
            role: {}
            for role in (
                "spire-observer",
                "spire-status-publisher",
                "spire-telemetry-sender",
            )
        },
        "volumes": {},
    }

    def resolve(file: str, values: dict[str, str]) -> dict[str, Any]:
        return copy.deepcopy(model)

    value.resolve = resolve
    commands: list[tuple[str, ...]] = []

    def docker(*args: str, payload: bytes | None = None) -> bytes:
        if args[0] == "compose":
            commands.append(args)
            assert args[args.index("-p") + 1] == PHYSICAL
        return b"[]"

    value.docker = docker
    observation_setup.install(value, plan(), tmp_path)
    assert len(commands) == 4
    current = read(tmp_path / "observation-compose.json")
    assert current["name"] == LOGICAL
    assert all(
        s["labels"]["io.aelev.installation"] == LOGICAL
        for s in current["services"].values()
    )


def test_export_accepts_shared_coordinator_but_refuses_partial_transfer(
    installed_fixture: dict[str, Any],
) -> None:
    data = installed_fixture
    for row in data["rows"].values():
        row["Config"]["Labels"]["com.docker.compose.project"] = PHYSICAL
    result = composition.export(data["root"], LOGICAL, "fixture-daemon")
    assert all(v["compose_project"] == PHYSICAL for v in result["roles"].values())
    next(iter(data["rows"].values()))["Config"]["Labels"][
        "com.docker.compose.project"
    ] = LOGICAL
    with pytest.raises(ValueError, match="COORDINATOR_MIXED"):
        composition.export(data["root"], LOGICAL, "fixture-daemon")
