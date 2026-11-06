"""Validate generated privileges without claiming a real kubelet exercise."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "render_fencing", ROOT / "kubernetes/render-fencing.py"
)
assert SPEC is not None and SPEC.loader is not None
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)


def values() -> dict[str, str]:
    result = {}
    for path in (ROOT / ".env.example", ROOT / "kubernetes/fencing.env.example"):
        for line in path.read_text().splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                result[key] = value
    return result


def test_exact_pod_permissions_without_node_or_secret_access() -> None:
    model = RENDERER.render(values())
    role = next(row for row in model["items"] if row["kind"] == "Role")
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["pods"],
            "resourceNames": ["postgresql-1-0", "postgresql-2-0", "postgresql-3-0"],
            "verbs": ["get", "list", "watch", "delete"],
        }
    ]
    assert not any(
        row["kind"] in {"ClusterRole", "Secret", "Node"} for row in model["items"]
    )
    deployment = next(row for row in model["items"] if row["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert all("hostPath" not in volume for volume in pod["volumes"])
    assert "/run/docker.sock" not in json.dumps(model)
    assert pod["containers"][0]["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert pod["containers"][0]["command"][-1] == "fencing-serve"


@pytest.mark.parametrize(
    "suffix",
    [
        "FENCING_KUBERNETES_NAMESPACE",
        "FENCING_KUBERNETES_SECRET_PROVIDER_CLASS",
        "NODE_2_KUBERNETES_POD",
    ],
)
def test_required_configuration_cannot_be_omitted(suffix: str) -> None:
    configuration = values()
    del configuration["AELEV_INFRASTRUCTURE_POSTGRESQL_" + suffix]
    with pytest.raises(ValueError):
        RENDERER.render(configuration)


def test_duplicate_pod_names_are_rejected() -> None:
    configuration = values()
    configuration["AELEV_INFRASTRUCTURE_POSTGRESQL_NODE_2_KUBERNETES_POD"] = (
        configuration["AELEV_INFRASTRUCTURE_POSTGRESQL_NODE_1_KUBERNETES_POD"]
    )
    with pytest.raises(ValueError):
        RENDERER.render(configuration)
