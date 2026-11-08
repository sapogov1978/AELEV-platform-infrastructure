"""Fail-closed checks for the isolated real lifecycle model."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PATH = Path(__file__).with_name("lifecycle.py")
SPEC = importlib.util.spec_from_file_location("egress_lifecycle", PATH)
assert SPEC and SPEC.loader
egress_lifecycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(egress_lifecycle)


def model(root: Path, token: str) -> dict[str, Any]:
    bind = root / "secrets"
    bind.mkdir()
    return {
        "services": {
            "public-egress-gateway": {
                "volumes": [
                    {"type": "bind", "source": str(bind), "target": "/run/secrets"}
                ]
            }
        },
        "networks": {"access": {"name": token + "-access"}},
        "volumes": {"telemetry": {"name": token + "-telemetry"}},
    }


def test_isolated_model_accepts_only_existing_temporary_resources(
    tmp_path: Path,
) -> None:
    egress_lifecycle.validate_isolated_model(model(tmp_path, "test"), "test")


def test_isolated_model_rejects_missing_bind_source(tmp_path: Path) -> None:
    description = model(tmp_path, "test")
    description["services"]["public-egress-gateway"]["volumes"][0]["source"] = str(
        tmp_path / "missing"
    )
    with pytest.raises(RuntimeError, match="/run/secrets"):
        egress_lifecycle.validate_isolated_model(description, "test")


def test_isolated_model_rejects_shared_resource_name(tmp_path: Path) -> None:
    description = model(tmp_path, "test")
    description["networks"]["access"]["name"] = "shared-network"
    with pytest.raises(RuntimeError, match="non-temporary networks: access"):
        egress_lifecycle.validate_isolated_model(description, "test")


def test_start_failure_keeps_only_fixed_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1,
            stdout="credential=must-not-survive\n",
            stderr="bind source path does not exist: token=must-not-survive\n",
        )

    monkeypatch.setattr(egress_lifecycle.subprocess, "run", failed)
    with pytest.raises(
        RuntimeError,
        match="command failed: up public-egress-gateway; reason=BIND_MOUNT_UNAVAILABLE",
    ) as failure:
        egress_lifecycle.run(["docker", "compose", "up", "public-egress-gateway"])
    assert "must-not-survive" not in str(failure.value)
