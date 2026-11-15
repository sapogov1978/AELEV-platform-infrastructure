"""Read the complete remaining resource inventory for one HA test scope."""

from collections.abc import Callable


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
