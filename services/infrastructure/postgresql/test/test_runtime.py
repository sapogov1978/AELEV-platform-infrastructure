"""Unit checks of delivery configuration, separate from real cluster tests."""

from __future__ import annotations

from pathlib import Path
import secrets
from typing import Any

import pytest

from aelev_postgresql import __main__ as runtime


def test_synchronous_safety_is_not_an_environment_fallback() -> None:
    config = runtime.safety_config()
    assert config["synchronous_mode"] == "quorum"
    assert config["synchronous_mode_strict"] is True
    assert config["synchronous_node_count"] == 1
    assert config["check_timeline"] is True
    assert config["failsafe_mode"] is False
    assert config["postgresql"]["parameters"]["synchronous_commit"] == "on"


def test_capacity_has_one_canonical_source(monkeypatch: Any) -> None:
    monkeypatch.setenv(runtime.PREFIX + "MAX_CONNECTIONS", "120")
    monkeypatch.setenv(runtime.PREFIX + "SHARED_BUFFERS", "64MB")
    parameters = runtime.desired_cluster_config()["postgresql"]["parameters"]
    assert parameters["max_connections"] == 120
    assert parameters["shared_buffers"] == "64MB"


def test_capacity_cannot_starve_cluster_operations(monkeypatch: Any) -> None:
    monkeypatch.setenv(runtime.PREFIX + "MAX_CONNECTIONS", "1")
    with pytest.raises(ValueError, match="MAX_CONNECTIONS"):
        runtime.desired_cluster_config()


@pytest.mark.parametrize("mode", ["docker", "kubernetes", "watchdog"])
def test_native_patroni_hook_matches_the_selected_mechanism(
    monkeypatch: Any, tmp_path: Path, mode: str
) -> None:
    # Configuration only: no TLS handshake is claimed by this unit fixture.
    for name in (
        "server.crt",
        "server.key",
        "admin.crt",
        "admin.key",
        "replication.crt",
        "replication.key",
        "etcd-client.crt",
        "etcd-client.key",
        "ca.crt",
        "etcd-ca.crt",
    ):
        (tmp_path / name).touch()
    monkeypatch.setattr(runtime, "SECRETS", tmp_path)
    path = Path(__file__).resolve().parents[1] / ".env.example"
    for line in path.read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            monkeypatch.setenv(key, value)
    monkeypatch.setenv(runtime.PREFIX + "NODE_ID", "1")
    monkeypatch.setenv(runtime.PREFIX + "FENCING_MODE", mode)
    monkeypatch.setattr(runtime, "secret", lambda name: secrets.token_urlsafe(16))
    config = runtime.node_config()
    assert config["watchdog"]["mode"] == ("required" if mode == "watchdog" else "off")
    assert ("pre_promote" in config["postgresql"]) == (mode != "watchdog")


def test_required_value_has_no_default(monkeypatch: Any) -> None:
    monkeypatch.delenv(runtime.PREFIX + "CLUSTER", raising=False)
    with pytest.raises(ValueError, match="CLUSTER"):
        runtime.setting("CLUSTER")


def test_secret_errors_do_not_expose_values(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(runtime, "SECRETS", tmp_path)
    (tmp_path / "admin-password").write_text("")
    with pytest.raises(ValueError, match="Empty secret file"):
        runtime.secret("admin-password")


def test_unowned_data_is_never_adopted(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(runtime, "DATA", tmp_path)
    monkeypatch.setenv(runtime.PREFIX + "CLUSTER", "unit-cluster")
    (tmp_path / "PG_VERSION").write_text("17")
    with pytest.raises(ValueError, match="unmanaged"):
        runtime.start()
    assert (tmp_path / "PG_VERSION").read_text() == "17"


def test_wrong_cluster_volume_is_never_adopted(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(runtime, "DATA", tmp_path)
    monkeypatch.setenv(runtime.PREFIX + "CLUSTER", "unit-cluster")
    (tmp_path / "aelev-cluster").write_text("another-cluster")
    with pytest.raises(ValueError, match="different cluster"):
        runtime.start()
