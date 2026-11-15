"""A retained network or failed inventory query cannot prove clean teardown."""

import pytest

from ha_cleanup import verify_cleanup


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
