from __future__ import annotations

from pathlib import Path

import pytest

import oidc_provision


def test_only_platform_operator_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AELEV_INFRASTRUCTURE_GRAFANA_OIDC_OPERATOR_USER", "platform-operator"
    )
    assert oidc_provision.exact_operator() == "platform-operator"
    monkeypatch.setenv(
        "AELEV_INFRASTRUCTURE_GRAFANA_OIDC_OPERATOR_USER", "grafana-admin"
    )
    with pytest.raises(
        RuntimeError, match="GRAFANA_OPERATOR_MUST_BE_PLATFORM_OPERATOR"
    ):
        oidc_provision.exact_operator()


def test_operator_role_is_owned_by_grafana() -> None:
    assert oidc_provision.OPERATOR_ROLE == "grafana.operator"
    assert oidc_provision.OPERATOR_ROLE_DESCRIPTION == "Grafana organization operator"


def test_client_is_confidential_pkce_and_exact_redirect() -> None:
    client = oidc_provision.client_description("grafana", "https://grafana.test:3000")
    assert client["publicClient"] is False
    assert client["standardFlowEnabled"] is True
    assert client["implicitFlowEnabled"] is False
    assert client["directAccessGrantsEnabled"] is False
    assert client["serviceAccountsEnabled"] is False
    assert client["fullScopeAllowed"] is False
    assert client["redirectUris"] == ["https://grafana.test:3000/login/generic_oauth"]
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["attributes"]["aelev.owner"] == "grafana"


def test_client_secret_is_replaced_atomically(tmp_path: Path) -> None:
    destination = tmp_path / "oidc" / "client-secret"
    oidc_provision._write_secret("first", destination)
    assert destination.read_text() == "first\n"
    assert destination.stat().st_mode & 0o777 == 0o640
    oidc_provision._write_secret("second", destination)
    assert destination.read_text() == "second\n"
    assert not destination.with_suffix(".pending").exists()


def test_main_reports_only_failure_type(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> None:
        raise FileNotFoundError("sensitive-local-path")

    monkeypatch.setattr(oidc_provision, "provision", fail)
    with pytest.raises(FileNotFoundError):
        oidc_provision.main()
    diagnostic = capsys.readouterr().err
    assert diagnostic.strip() == "Provisioning failed: FileNotFoundError"
    assert "sensitive-local-path" not in diagnostic


def test_admin_permission_error_is_stage_specific(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*args: object, **kwargs: object) -> str:
        raise PermissionError("sensitive-local-path")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(oidc_provision.GrafanaAdminSecretPermissionError):
        oidc_provision._read_admin("admin-password")
