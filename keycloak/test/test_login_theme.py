"""Check theme activation without touching credentials or realm timeouts."""

from unittest.mock import Mock
from pathlib import Path

import pytest

from status_publisher import configure_login_theme


def test_import_fixture_derives_paths_from_its_realm_name() -> None:
    source = (Path(__file__).parent / "login-expiry.cjs").read_text()
    assert "join(directory, `${realmName}-realm.json`)" in source
    assert "/data/import/${realmName}-realm.json,readonly`" in source
    assert "realm: realmName," in source


@pytest.mark.parametrize("previous", [None, "", "keycloak", "keycloak.v2"])
def test_theme_activation_updates_only_its_own_setting(previous: str | None) -> None:
    admin = Mock(side_effect=[{"loginTheme": previous}, None, {"loginTheme": "aelev"}])
    configure_login_theme(admin)
    assert admin.call_args_list[1].args == ("PUT", "", {"loginTheme": "aelev"})
    assert admin.call_count == 3


def test_theme_activation_is_idempotent() -> None:
    admin = Mock(return_value={"loginTheme": "aelev"})
    configure_login_theme(admin)
    admin.assert_called_once_with("GET", "", None)


def test_custom_theme_is_not_overwritten() -> None:
    admin = Mock(return_value={"loginTheme": "custom"})
    with pytest.raises(ValueError, match="custom login theme"):
        configure_login_theme(admin)
    assert admin.call_count == 1


def test_rejected_configuration_does_not_report_success() -> None:
    admin = Mock(side_effect=[{}, None, {}])
    with pytest.raises(RuntimeError, match="not applied"):
        configure_login_theme(admin)
