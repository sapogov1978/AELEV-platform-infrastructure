from __future__ import annotations

from pathlib import Path

import pytest

from translator_service import observability


def configure_environment(
    monkeypatch: pytest.MonkeyPatch, environment: str, endpoint: str
) -> None:
    monkeypatch.setenv("AELEV_ENVIRONMENT", environment)
    monkeypatch.setenv("AELEV_TRANSLATOR_OTLP_HTTP_ENDPOINT", endpoint)


def test_local_mode_allows_telemetry_to_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_environment(monkeypatch, "local", "")
    assert observability.configure("worker") is None


def test_production_requires_https_durable_sender(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_environment(monkeypatch, "production", "")
    with pytest.raises(RuntimeError, match="TELEMETRY_REQUIRED"):
        observability.configure("worker")
    configure_environment(monkeypatch, "production", "http://sender:4318")
    with pytest.raises(RuntimeError, match="HTTPS_REQUIRED"):
        observability.configure("worker")
    configure_environment(monkeypatch, "production", "https://sender:4318")
    for name, value in {
        "AELEV_TRANSLATOR_TELEMETRY_CA_FILE": tmp_path / "ca",
        "AELEV_TRANSLATOR_TELEMETRY_CLIENT_CERT_FILE": tmp_path / "cert",
        "AELEV_TRANSLATOR_TELEMETRY_CLIENT_KEY_FILE": tmp_path / "key",
    }.items():
        monkeypatch.setenv(name, str(value))
    with pytest.raises(RuntimeError, match="CREDENTIAL_MISSING"):
        observability.configure("worker")
