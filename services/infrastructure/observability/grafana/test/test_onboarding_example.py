"""Offline safety regressions for the documented deployment example."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "owner-metrics"
SPEC = importlib.util.spec_from_file_location(
    "example_grafana_api", EXAMPLE / "grafana_api.py"
)
assert SPEC is not None and SPEC.loader is not None
api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api)


@pytest.mark.parametrize(
    "uid", ["", "../other", "bad uid", "x" * 41, "aelev-infrastructure"]
)
def test_unsafe_or_reserved_uid(uid: str) -> None:
    with pytest.raises(ValueError):
        api.identifier(uid)


def test_private_token_without_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "token"
    path.write_text("test-only-value\n")
    path.chmod(0o600)
    assert api.read_secret(path) == "test-only-value"
    assert not capsys.readouterr().out
    path.chmod(0o644)
    with pytest.raises(ValueError):
        api.read_secret(path)


class MemoryClient(api.Client):  # type: ignore[name-defined,misc]
    def __init__(self, current: dict[str, Any] | None) -> None:
        self.current = current
        self.writes: list[dict[str, Any]] = []

    def call(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        if method == "GET" and "/folders/" in path:
            return {"uid": "owner-demo"}
        if method == "GET":
            if self.current is None:
                raise api.ApiError(404)
            return self.current
        assert data is not None
        self.writes.append(data)
        return {"status": "success", "uid": "owner-demo-metrics"}


def test_publication_prevents_cross_folder_takeover() -> None:
    client = MemoryClient({"meta": {"folderUid": "other-demo"}})
    with pytest.raises(ValueError, match="another folder"):
        client.publish("owner-demo", {"uid": "owner-demo-metrics"})
    assert not client.writes


def test_publication_preserves_version_for_conflict_detection() -> None:
    client = MemoryClient(
        {
            "meta": {"folderUid": "owner-demo"},
            "dashboard": {"id": 7, "version": 3},
        }
    )
    client.publish("owner-demo", {"uid": "owner-demo-metrics"})
    assert client.writes[0]["dashboard"]["id"] == 7
    assert client.writes[0]["dashboard"]["version"] == 3
    assert client.writes[0]["overwrite"] is False


def test_generic_dashboard_and_local_remote_write() -> None:
    dashboard = json.loads((EXAMPLE / "dashboard.json").read_text())
    assert dashboard["uid"] == "owner-demo-metrics"
    assert dashboard["panels"][0]["datasource"]["uid"] == "aelev-prometheus"
    assert dashboard["panels"][0]["fieldConfig"]["defaults"]["unit"] == "short"
    config = (EXAMPLE / "prometheus.yml").read_text()
    assert "url: https://localhost:4318/api/v1/write" in config
    assert "protobuf_message: prometheus.WriteRequest" in config
    assert "insecure_skip_verify" not in config
    assert json.loads((EXAMPLE / "sender.json").read_text())["mode"] == "sender"
