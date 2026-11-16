"""A retained network or failed inventory query cannot prove clean teardown."""

import copy
import json
from typing import Any

import pytest

from ha_cleanup import remove_never_started_helpers, verify_cleanup

SCOPE = "ssh-ha-0123456789ab"
IDENTITY = "a" * 64
IMAGE = "sha256:" + "b" * 64


def helper() -> dict[str, Any]:
    return {
        "Id": IDENTITY,
        "Image": IMAGE,
        "Config": {
            "Labels": {
                "io.aelev.postgresql.fencing-cluster": SCOPE,
                "io.aelev.postgresql.operation": "node-fencing",
                "io.aelev.postgresql.fencing-target": "postgresql-3",
            }
        },
        "HostConfig": {"AutoRemove": True},
        "State": {
            "Status": "created",
            "Running": False,
            "Pid": 0,
            "StartedAt": "0001-01-01T00:00:00Z",
        },
    }


@pytest.mark.parametrize(
    "change",
    [None, "image", "scope", "operation", "target", "autoremove", "preexisting"],
)
def test_only_exact_new_never_started_helper_can_be_removed(change: str | None) -> None:
    row = helper()
    if change == "image":
        row["Image"] = "another-image"
    if change in ("scope", "operation", "target"):
        label = {
            "scope": "fencing-cluster",
            "operation": "operation",
            "target": "fencing-target",
        }[change]
        row["Config"]["Labels"]["io.aelev.postgresql." + label] = "another-owner"
    if change == "autoremove":
        row["HostConfig"]["AutoRemove"] = False
    calls: list[tuple[str, ...]] = []

    def docker(*arguments: str) -> str:
        calls.append(arguments)
        if arguments[0] == "ps":
            return IDENTITY if "status=created" in arguments else ""
        if arguments[0] == "inspect":
            return json.dumps([row])
        assert arguments == ("rm", IDENTITY)
        return IDENTITY

    if change is None:
        assert remove_never_started_helpers(docker, SCOPE, IMAGE, set()) == [IDENTITY]
        assert calls[-1] == ("rm", IDENTITY)
    else:
        with pytest.raises(RuntimeError, match="CREATED_HELPER_NOT_OWNED"):
            remove_never_started_helpers(
                docker, SCOPE, IMAGE, {IDENTITY} if change == "preexisting" else set()
            )
        assert not any(call[0] == "rm" for call in calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("Status", "running"),
        ("Running", True),
        ("Pid", 12),
        ("StartedAt", "2026-11-15T23:00:00Z"),
    ],
)
def test_started_or_running_helper_is_never_removed(field: str, value: object) -> None:
    row = copy.deepcopy(helper())
    row["State"][field] = value
    calls: list[tuple[str, ...]] = []

    def docker(*arguments: str) -> str:
        calls.append(arguments)
        if arguments[0] == "ps":
            return IDENTITY if "status=created" in arguments else ""
        assert arguments == ("inspect", IDENTITY)
        return json.dumps([row])

    assert remove_never_started_helpers(docker, SCOPE, IMAGE, set()) == []
    assert not any(call[0] == "rm" for call in calls)


def test_live_role_blocks_helper_removal_before_inspect() -> None:
    calls: list[tuple[str, ...]] = []

    def docker(*arguments: str) -> str:
        calls.append(arguments)
        return "owned-role"

    with pytest.raises(RuntimeError, match="OWNED_ROLE_TEARDOWN_UNCONFIRMED"):
        remove_never_started_helpers(docker, SCOPE, IMAGE, set())
    assert len(calls) == 1


def test_concurrent_start_rejection_never_uses_force_or_repeats() -> None:
    removals = []

    def docker(*arguments: str) -> str:
        if arguments[0] == "ps":
            return IDENTITY if "status=created" in arguments else ""
        if arguments[0] == "inspect":
            return json.dumps([helper()])
        removals.append(arguments)
        raise RuntimeError("CONTAINER_NOW_RUNNING")

    with pytest.raises(RuntimeError, match="CONTAINER_NOW_RUNNING"):
        remove_never_started_helpers(docker, SCOPE, IMAGE, set())
    assert removals == [("rm", IDENTITY)]


@pytest.mark.parametrize("scope", ["", "different", "ssh-ha-" + "0" * 13])
def test_invalid_scope_never_queries_docker(scope: str) -> None:
    def docker(*arguments: str) -> str:
        raise AssertionError("DOCKER_MUST_NOT_BE_CALLED")

    with pytest.raises(RuntimeError, match="TEST_SCOPE_INVALID"):
        remove_never_started_helpers(docker, scope, IMAGE, set())


@pytest.mark.parametrize(
    "remaining", [None, "containers", "node_operations", "networks", "volumes"]
)
def test_inventory_keeps_every_scoped_resource_class(remaining: str | None) -> None:
    observed: list[tuple[str, ...]] = []

    def docker(*arguments: str) -> str:
        observed.append(arguments)
        assert arguments[-2:] == (
            "--filter",
            "label="
            + (
                "io.aelev.postgresql.fencing-cluster"
                if len(observed) == 2
                else "io.aelev.evidence.scope"
            )
            + "=test-scope",
        )
        kind = ("containers", "node_operations", "networks", "volumes")[
            len(observed) - 1
        ]
        return "retained-resource\n" if kind == remaining else ""

    proof: dict[str, object] = {}
    if remaining is None:
        verify_cleanup(docker, "test-scope", proof)
    else:
        with pytest.raises(RuntimeError, match="OWNED_TEST_RESOURCES_REMAIN"):
            verify_cleanup(docker, "test-scope", proof)
    assert len(observed) == 4
    assert proof["cleanup_inventory"] == {
        "scope": "test-scope",
        "remaining": {
            kind: ["retained-resource"] if kind == remaining else []
            for kind in ("containers", "node_operations", "networks", "volumes")
        },
    }
    assert proof["owned_resources_removed"] is (remaining is None)


@pytest.mark.parametrize("failed_query", [1, 2, 3, 4])
def test_inventory_failure_is_never_an_empty_success(failed_query: int) -> None:
    calls = 0

    def docker(*arguments: str) -> str:
        nonlocal calls
        calls += 1
        if calls == failed_query:
            raise RuntimeError("DOCKER_QUERY_FAILED")
        return ""

    proof: dict[str, object] = {"owned_resources_removed": True}
    with pytest.raises(RuntimeError, match="DOCKER_QUERY_FAILED"):
        verify_cleanup(docker, "test-scope", proof)
    assert proof["owned_resources_removed"] is False
