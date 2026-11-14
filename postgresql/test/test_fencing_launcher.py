"""Forced node launcher rejects unowned engines, volumes and image tags."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

SPEC = importlib.util.spec_from_file_location(
    "fencing_launcher", Path(__file__).resolve().parents[1] / "fencing-node.py"
)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


@pytest.fixture
def config(tmp_path: Path) -> Any:
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    result = {
        "schema": 1,
        "cluster": "owned",
        "node": "postgresql-2",
        "daemon_id": "owned-daemon-id",
        "image": "ghcr.io/owner/fencer@sha256:" + "a" * 64,
        "state_volume": "owned-node-2",
        "secrets_root": str(secrets),
        "etcd": [{"host": f"etcd-{n}.invalid", "port": 2379} for n in range(3)],
        "timeout_seconds": 20,
    }
    path = tmp_path / "node.json"
    path.write_text(json.dumps(result))
    path.chmod(0o600)
    return path, result


@pytest.mark.parametrize(
    "key,value",
    [
        ("image", "fencer:latest"),
        ("state_volume", "--privileged"),
        ("schema", True),
        ("node", "other"),
        ("timeout_seconds", 0),
        ("command", "sh"),
    ],
)
def test_invalid_operator_input_is_rejected(config: Any, key: str, value: Any) -> None:
    path, data = config
    data[key] = value
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        launcher.configuration(path)


def test_world_readable_input_is_rejected(config: Any) -> None:
    path, _ = config
    path.chmod(0o644)
    with pytest.raises(ValueError):
        launcher.configuration(path)


@pytest.mark.parametrize("foreign", ["engine", "volume", "architecture"])
def test_foreign_runtime_is_rejected(
    config: Any, monkeypatch: Any, foreign: str
) -> None:
    path, _ = config
    data = launcher.configuration(path)

    def inspect(arguments: list[str]) -> Any:
        if arguments[0] == "info":
            return {
                "ID": "foreign" if foreign == "engine" else "owned-daemon-id",
                "Architecture": "aarch64",
            }
        if arguments[0] == "volume":
            return [
                {
                    "Labels": {
                        launcher.LABEL
                        + "cluster": "foreign" if foreign == "volume" else "owned",
                        launcher.LABEL + "node": "postgresql-2",
                    }
                }
            ]
        return [
            {
                "Os": "linux",
                "Architecture": "amd64" if foreign == "architecture" else "arm64",
            }
        ]

    monkeypatch.setattr(launcher, "inspect", inspect)
    with pytest.raises(ValueError):
        launcher.command(data, initialize=False)


def test_launcher_uses_only_preexisting_native_owner_resources(
    config: Any, monkeypatch: Any
) -> None:
    path, _ = config
    calls: list[list[str]] = []

    def inspect(arguments: list[str]) -> Any:
        calls.append(arguments)
        if arguments[0] == "info":
            return {"ID": "owned-daemon-id", "Architecture": "aarch64"}
        if arguments[0] == "volume":
            return [
                {
                    "Labels": {
                        launcher.LABEL + "cluster": "owned",
                        launcher.LABEL + "node": "postgresql-2",
                    }
                }
            ]
        return [{"Os": "linux", "Architecture": "arm64"}]

    monkeypatch.setattr(launcher, "inspect", inspect)
    args = launcher.command(launcher.configuration(path), initialize=False)
    assert "--pull" in args and args[args.index("--pull") + 1] == "never"
    assert args[-3:] == ["-m", "aelev_postgresql.fencing_node", "execute"]
    assert all(arguments[0] in {"info", "volume", "image"} for arguments in calls)
    assert "--privileged" not in args


@pytest.mark.parametrize("name", ["--privileged", "owned-node-2"])
def test_credentials_volume_must_be_distinct_and_named(config: Any, name: str) -> None:
    path, data = config
    data.pop("secrets_root")
    data["secrets_volume"] = name
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        launcher.configuration(path)


@pytest.mark.parametrize("foreign", [False, True])
def test_credentials_volume_has_explicit_owner_and_purpose(
    config: Any, monkeypatch: Any, foreign: bool
) -> None:
    path, data = config
    data.pop("secrets_root")
    data["secrets_volume"] = "owned-node-2-credentials"
    path.write_text(json.dumps(data))
    validated = launcher.configuration(path)

    def inspect(arguments: list[str]) -> Any:
        if arguments[0] == "info":
            return {"ID": "owned-daemon-id", "Architecture": "aarch64"}
        if arguments[0] == "image":
            return [{"Os": "linux", "Architecture": "arm64"}]
        labels = {
            launcher.LABEL + "cluster": "owned",
            launcher.LABEL + "node": "postgresql-2",
        }
        if arguments[-1] == "owned-node-2-credentials":
            labels[launcher.LABEL + "fencing-resource"] = (
                "state" if foreign else "credentials"
            )
        return [{"Labels": labels}]

    monkeypatch.setattr(launcher, "inspect", inspect)
    if foreign:
        with pytest.raises(ValueError, match="SECRETS_NOT_OWNED"):
            launcher.command(validated, initialize=False)
    else:
        command = launcher.command(validated, initialize=False)
        assert (
            "type=volume,source=owned-node-2-credentials,target=/run/aelev/secrets,readonly"
            in command
        )
