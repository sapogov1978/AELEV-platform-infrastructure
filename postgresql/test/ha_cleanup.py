"""Read the complete remaining resource inventory for one HA test scope."""

from collections.abc import Callable
import json
import re


def remove_never_started_helpers(
    docker: Callable[..., str], scope: str, image: str, preexisting: set[str]
) -> list[str]:
    """After all test roles stop, remove only their never-started auto-rm helpers."""
    if re.fullmatch(r"ssh-ha-[a-f0-9]{12}", scope) is None:
        raise RuntimeError("TEST_SCOPE_INVALID")
    if docker("ps", "-aq", "--filter", "label=io.aelev.evidence.scope=" + scope):
        raise RuntimeError("OWNED_ROLE_TEARDOWN_UNCONFIRMED")
    identities = docker(
        "ps",
        "-aq",
        "--no-trunc",
        "--filter",
        "status=created",
        "--filter",
        "label=io.aelev.postgresql.fencing-cluster=" + scope,
    ).split()
    removed = []
    for identity in identities:
        if re.fullmatch(r"[a-f0-9]{64}", identity) is None or identity in preexisting:
            raise RuntimeError("CREATED_HELPER_NOT_OWNED")
        row = json.loads(docker("inspect", identity))[0]
        labels = row["Config"]["Labels"]
        if (
            row["Id"] != identity
            or row["Image"] != image
            or labels.get("io.aelev.postgresql.fencing-cluster") != scope
            or labels.get("io.aelev.postgresql.operation") != "node-fencing"
            or labels.get("io.aelev.postgresql.fencing-target")
            not in {"postgresql-1", "postgresql-2", "postgresql-3"}
            or row["HostConfig"].get("AutoRemove") is not True
        ):
            raise RuntimeError("CREATED_HELPER_NOT_OWNED")
        state = row["State"]
        if (
            state.get("Status") == "created"
            and state.get("Running") is False
            and state.get("Pid") == 0
            and state.get("StartedAt") == "0001-01-01T00:00:00Z"
        ):
            # Normal rm rejects a concurrent start; never force or start it.
            docker("rm", identity)
            removed.append(identity)
    return removed


def owned_resources(docker: Callable[..., str], scope: str) -> dict[str, list[str]]:
    queries = {
        "containers": ("ps", "-aq", "--no-trunc"),
        "node_operations": ("ps", "-aq", "--no-trunc"),
        "networks": ("network", "ls", "-q", "--no-trunc"),
        "volumes": ("volume", "ls", "-q"),
    }
    result = {}
    for kind, arguments in queries.items():
        label = (
            "io.aelev.postgresql.fencing-cluster"
            if kind == "node_operations"
            else "io.aelev.evidence.scope"
        )
        result[kind] = sorted(
            set(docker(*arguments, "--filter", f"label={label}={scope}").split())
        )
    return result


def verify_cleanup(
    docker: Callable[..., str], scope: str, proof: dict[str, object]
) -> None:
    proof["owned_resources_removed"] = False
    remaining = owned_resources(docker, scope)
    proof["cleanup_inventory"] = {"scope": scope, "remaining": remaining}
    if any(remaining.values()):
        raise RuntimeError("OWNED_TEST_RESOURCES_REMAIN")
    proof["owned_resources_removed"] = True
