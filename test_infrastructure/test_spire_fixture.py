"""Failure-path checks for fresh owner scope and public runtime acceptance."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import lifecycle
import root_routes
from resources import Inventory, cleanup_plan, inventory, verify_preserved
from spire_fixture import Fixture


def fixture(tmp_path: Path) -> Fixture:
    (tmp_path / "spire").mkdir()
    (tmp_path / "spire/.env").write_text(
        "AELEV_INFRASTRUCTURE_SPIRE_COMPOSE_PROJECT=owned-spire\n"
    )
    (tmp_path / ".env").write_text("EXISTING=kept\n")
    return Fixture(tmp_path)


def empty() -> Inventory:
    return {kind: {} for kind in ("containers", "networks", "volumes", "images")}


def test_owner_scope_covers_partial_native_resources_without_adopting_peers() -> None:
    before = empty()
    before["volumes"]["original"] = {"project": "core"}
    after = copy.deepcopy(before)
    after["volumes"].update(
        {
            "native": {
                "project": "",
                "owner": "infrastructure-spire",
                "installation": "new-spire",
            },
            "peer": {
                "project": "",
                "owner": "infrastructure-spire",
                "installation": "other-spire",
            },
            "foreign": {
                "project": "",
                "owner": "another-owner",
                "installation": "new-spire",
            },
        }
    )
    owners = frozenset({("infrastructure-spire", "new-spire")})
    assert cleanup_plan(before, after, "core", set(), owners)["volumes"] == ["native"]
    with pytest.raises(RuntimeError, match="created project resources remain"):
        verify_preserved(before, after, "core", owners)
    del after["volumes"]["native"]
    verify_preserved(before, after, "core", owners)
    del after["volumes"]["original"]
    with pytest.raises(RuntimeError, match="pre-existing volumes changed"):
        verify_preserved(before, after, "core", owners)


def test_inventory_keeps_native_owner_labels() -> None:
    def execute(args: list[str]) -> str:
        if args[1:3] == ["volume", "ls"]:
            return json.dumps(
                {
                    "Name": "native",
                    "Labels": "io.aelev.owner=infrastructure-spire,io.aelev.installation=isolated",
                }
            )
        return ""

    assert inventory(execute)["volumes"]["native"]["installation"] == "isolated"


@pytest.mark.parametrize("kind", ["containers", "networks", "volumes"])
def test_fresh_preflight_rejects_prior_native_owner_state(
    tmp_path: Path, kind: str
) -> None:
    owner = fixture(tmp_path)
    before = empty()
    before[kind]["existing"] = {
        "owner": "infrastructure-spire",
        "installation": owner.project,
    }
    with pytest.raises(RuntimeError, match="EXISTING_RESOURCES_REFUSED"):
        owner.preflight(before)
    assert not owner.directory.exists()


def test_fresh_preflight_rejects_existing_public_binding(tmp_path: Path) -> None:
    owner = fixture(tmp_path)
    (tmp_path / ".env").write_text(
        "AELEV_INFRASTRUCTURE_SPIRE_INSTALLED_COMPOSE=/existing/compose.json\n"
    )
    with pytest.raises(RuntimeError, match="EXISTING_BINDING_REFUSED"):
        owner.preflight(empty())


def test_partial_bootstrap_cleanup_removes_only_new_exact_owner_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = empty()
    baseline["containers"]["protected"] = {"project": "other", "state": "running"}
    current = copy.deepcopy(baseline)
    labels = {
        "owner": "infrastructure-spire",
        "installation": "new",
        "project": "native",
    }
    current["containers"]["native-container"] = labels
    current["volumes"]["native-state"] = labels | {"project": ""}
    current["networks"]["native-network"] = labels
    commands: list[list[str]] = []

    def execute(args: list[str], **kwargs: Any) -> str:
        commands.append(args)
        return ""

    monkeypatch.setattr(lifecycle, "execute", execute)
    monkeypatch.setattr(lifecycle, "mounted_volumes", lambda *_: {"native-state"})
    monkeypatch.setattr(
        lifecycle, "inventory", Mock(side_effect=[current, current, baseline])
    )
    lifecycle.cleanup(
        baseline,
        "core",
        set(),
        ["docker", "compose", "-p", "core"],
        frozenset({("infrastructure-spire", "new")}),
    )
    assert ["docker", "container", "rm", "-f", "native-container"] in commands
    assert ["docker", "volume", "rm", "native-state"] in commands
    assert not any("protected" in command for command in commands)


def test_initial_and_repeat_use_public_cli_with_explicit_owner_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    execute = Mock(return_value=subprocess.CompletedProcess([], 0, "{}", ""))
    monkeypatch.setattr("lifecycle.subprocess.run", execute)
    for phase in ("initial", "repeat"):
        lifecycle.execute_group_start(
            ["unused"],
            "core",
            {},
            tmp_path / "diagnostic.json",
            phase,
            runtime_inputs=("--spire-local-input", "/explicit/private.json"),
        )
        args = execute.call_args.args[0]
        assert "bootstrap.py" in args[1] and "--full-runtime" in args
        assert args[-2:] == ["--spire-local-input", "/explicit/private.json"]


def test_unknown_operator_outcome_is_not_repeated_or_disclosed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = fixture(tmp_path)
    execute = Mock(
        side_effect=subprocess.TimeoutExpired(
            ["operator"], 1, stderr="sensitive-output"
        )
    )
    monkeypatch.setattr("spire_fixture.subprocess.run", execute)
    with pytest.raises(
        RuntimeError, match="ROOT_SPIRE_OPERATION_RESULT_UNKNOWN"
    ) as failure:
        owner.invoke(["operator"])
    assert "sensitive-output" not in str(failure.value)
    assert execute.call_count == 1


def test_binding_restore_preserves_original_bytes_and_refuses_unrelated_changes(
    tmp_path: Path,
) -> None:
    owner = fixture(tmp_path)
    path = tmp_path / ".env"
    binding = (
        "AELEV_INFRASTRUCTURE_SPIRE_INSTALLED_COMPOSE="
        + str(tmp_path / "runtime/spire-installed/runtime/installed-composition")
        + "/"
        + "a" * 64
        + "/compose.json\n"
    )
    inode = path.stat().st_ino
    path.write_bytes(owner.original_env + binding.encode())
    owner.restore_binding()
    assert path.read_bytes() == owner.original_env and path.stat().st_ino == inode
    changed = owner.original_env + binding.encode() + b"OTHER=operator-change\n"
    path.write_bytes(changed)
    with pytest.raises(RuntimeError, match="ENV_CHANGED"):
        owner.restore_binding()
    assert path.read_bytes() == changed


def test_route_preparation_requires_explicit_ephemeral_ci(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(RuntimeError, match="EPHEMERAL_CI_REQUIRED"):
        root_routes.prepare(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_route_restore_checks_every_file_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(root_routes, "ensure_stopped", lambda _: None)
    record: dict[str, Any] = {"schema_version": 1, "files": {}}
    for name in root_routes.OWNERS:
        directory = tmp_path / "observability" / name
        directory.mkdir(parents=True)
        (directory / ".env").write_text("changed\n")
        record["files"][name] = {"original": "original\n", "changed": "changed\n"}
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/root-spire-routes.json").write_text(json.dumps(record))
    last = tmp_path / "observability/grafana/.env"
    last.write_text("operator-change\n")
    with pytest.raises(RuntimeError, match="ENV_CHANGED"):
        root_routes.restore(tmp_path)
    assert (tmp_path / "observability/delivery/.env").read_text() == "changed\n"
    assert last.read_text() == "operator-change\n"


def test_core_restart_targets_exclude_delegated_spire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    execute = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr("lifecycle.subprocess.run", execute)
    lifecycle.execute_group_start(
        ["docker", "compose"],
        "core",
        {"kafka": {}, "keycloak": {}},
        tmp_path / "diagnostic.json",
        "stop-up",
    )
    assert execute.call_args.args[0][-2:] == ["kafka", "keycloak"]


@pytest.mark.parametrize("invalid", [False, True])
def test_complete_status_requires_every_role_and_both_coordinators(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, invalid: bool
) -> None:
    owner = fixture(tmp_path)
    expected = json.loads((lifecycle.ROOT / "full-local.json").read_bytes())[
        "root_compose"
    ]["required_services"]
    observed = {
        "status": "RUNTIME_HEALTHY",
        "services": expected[:-1] if invalid else expected,
        "owner_coordinators": {"core": "core", "spire": owner.project},
    }
    monkeypatch.setattr(lifecycle, "execute", lambda _: json.dumps(observed))
    if invalid:
        with pytest.raises(RuntimeError, match="ROOT_FULL_RUNTIME_NOT_VERIFIED"):
            lifecycle.verify_full_runtime(owner)
    else:
        lifecycle.verify_full_runtime(owner)
