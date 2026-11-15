"""Protect local operator credentials, narrow authority and ambiguous issuance."""

from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest

SPEC = importlib.util.spec_from_file_location(
    "grafana_local_publisher",
    Path(__file__).resolve().parents[1] / "local-publisher.py",
)
assert SPEC and SPEC.loader
operator: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operator)


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:13000",
        "https://grafana.example:13000",
        "https://user:password@localhost:13000",
        "https://localhost:13000/path",
        "https://localhost:13000?query=private",
    ],
)
def test_local_password_login_cannot_target_remote_or_untrusted_origin(
    value: str,
) -> None:
    with pytest.raises(operator.ProvisionError, match="LOCAL_HTTPS_ORIGIN_REQUIRED"):
        operator.origin(value)


def test_isolated_cleanup_rejects_a_neighbor_volume_even_without_its_process() -> None:
    spec = importlib.util.spec_from_file_location(
        "grafana_isolated_operator", Path(__file__).with_name("local_publisher_e2e.py")
    )
    assert spec and spec.loader
    e2e: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(e2e)
    model = {
        "name": "isolated-identity",
        "services": {"keycloak": {}},
        "volumes": {
            "database": {"name": "isolated-database"},
            "unused-status": {"name": "working-status-volume"},
        },
    }
    with pytest.raises(RuntimeError, match="ISOLATED_RESOURCE_SCOPE_INVALID"):
        e2e.validate_scope(model, "isolated", "isolated-identity")


def test_redirect_cannot_forward_operator_request_to_another_origin() -> None:
    redirect = operator.Redirects(
        {"https://localhost:13000", "https://localhost:18443"}
    )
    with pytest.raises(operator.ProvisionError, match="REDIRECT_REJECTED"):
        redirect.redirect_request(
            Request("https://localhost:18443", data=b"private-password"),
            None,
            302,
            "Found",
            {},
            "https://other.example/login",
        )


def test_shared_password_and_symlinks_are_rejected(tmp_path: Path) -> None:
    secret = tmp_path / "password"
    secret.write_text("private")
    secret.chmod(0o644)
    with pytest.raises(operator.ProvisionError, match="PRIVATE_FILE_REQUIRED"):
        operator.private(secret)
    secret.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(secret)
    with pytest.raises(operator.ProvisionError, match="PRIVATE_FILE_REQUIRED"):
        operator.private(link)


def test_oidc_redirect_drops_form_body_and_idp_origin_at_grafana() -> None:
    redirect = operator.Redirects(
        {"https://localhost:13000", "https://localhost:18443"}
    )
    request = Request(
        "https://localhost:18443/realms/aelev/login-actions/authenticate",
        data=b"password=private",
        headers={"Origin": "https://localhost:18443"},
    )
    result = redirect.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://localhost:13000/login/generic_oauth?code=one-time-code",
    )
    assert result.get_method() == "GET"
    assert result.data is None
    assert result.get_header("Origin") is None


class API:
    grafana = "https://localhost:13000"

    def __init__(self, *, lose_token_response: bool = False) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.accounts: dict[int, dict[str, Any]] = {}
        self.token_names: set[str] = set()
        self.lose_token_response = lose_token_response

    def call(
        self, method: str, path: str, value: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, path, value))
        if path == "/api/folders":
            assert value
            return {"uid": value["uid"]}
        if path == "/api/serviceaccounts":
            assert value
            identifier = len(self.accounts) + 10
            self.accounts[identifier] = {"id": identifier, **value}
            return self.accounts[identifier]
        if path.endswith("/tokens"):
            assert value
            if value["name"] in self.token_names:
                raise operator.ProvisionError("GRAFANA_LOCAL_HTTP_400")
            self.token_names.add(value["name"])
            if self.lose_token_response:
                raise operator.ProvisionError("GRAFANA_LOCAL_RESULT_UNKNOWN")
            return {"id": 42, "key": "private-one-time-value"}
        if path.endswith("/permissions"):
            return {"message": "updated"}
        identifier = int(path.rsplit("/", 1)[1])
        if identifier not in self.accounts:
            raise operator.ProvisionError("GRAFANA_LOCAL_HTTP_404")
        if method == "DELETE":
            del self.accounts[identifier]
            return {"message": "deleted"}
        return self.accounts[identifier]


def test_viewer_receives_only_own_folder_edit_and_admin_is_separate(
    tmp_path: Path,
) -> None:
    api = API()
    output = tmp_path / "tokens"
    receipt = operator.issue(api, output, "owner-test", "Owner", True)
    assert receipt["status"] == "TOKENS_ISSUED"
    assert [row["role"] for row in api.accounts.values()] == ["Viewer", "Admin"]
    permissions = [
        (path, value) for _, path, value in api.calls if path.endswith("/permissions")
    ]
    assert permissions == [
        (
            "/api/folders/owner-test/permissions",
            {"items": [{"userId": 10, "permission": 2}]},
        )
    ]
    assert "private-one-time-value" not in (output / "receipt.json").read_text()
    for path in output.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    tokens = [value for _, path, value in api.calls if path.endswith("/tokens")]
    assert [value["secondsToLive"] for value in tokens if value] == [86400, 900]
    before = len(api.calls)
    with pytest.raises(FileExistsError):
        operator.issue(api, output, "owner-test", "Owner", True)
    assert len(api.calls) == before


def test_lost_one_time_token_response_preserves_intent_without_retry(
    tmp_path: Path,
) -> None:
    api = API(lose_token_response=True)
    output = tmp_path / "tokens"
    with pytest.raises(operator.ProvisionError, match="RESULT_UNKNOWN"):
        operator.issue(api, output, "owner-test", "Owner", True)
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["status"] == "INSPECTION_REQUIRED"
    assert receipt["pending"] == {
        "method": "POST",
        "path": "/api/serviceaccounts/10/tokens",
    }
    assert len([1 for _, path, _ in api.calls if path.endswith("/tokens")]) == 1
    assert not (output / "publisher.token").exists()
    assert len(api.accounts) == 1


def test_retire_removes_only_exact_temporary_identity_and_keeps_publisher(
    tmp_path: Path,
) -> None:
    api = API()
    output = tmp_path / "tokens"
    operator.issue(api, output, "owner-test", "Owner", True)
    value = operator.retire(api, output)
    assert value["status"] == "PROVISIONER_RETIRED"
    assert list(api.accounts) == [10]
    assert api.accounts[10]["role"] == "Viewer"
    before = len(api.calls)
    operator.retire(api, output)
    assert len(api.calls) == before


def test_retire_rejects_replaced_identity(tmp_path: Path) -> None:
    api = API()
    output = tmp_path / "tokens"
    operator.issue(api, output, "owner-test", "Owner", True)
    api.accounts[11]["name"] = "different-owner"
    with pytest.raises(operator.ProvisionError, match="PROVISIONER_MISMATCH"):
        operator.retire(api, output)
    assert all(method != "DELETE" for method, _, _ in api.calls)


def test_retirement_reconciles_lost_delete_response_by_read_only_lookup(
    tmp_path: Path,
) -> None:
    api = API()
    output = tmp_path / "tokens"
    operator.issue(api, output, "owner-test", "Owner", True)
    receipt_path = output / "receipt.json"
    value = json.loads(receipt_path.read_text())
    value["status"] = "RETIREMENT_PENDING"
    operator.journal(receipt_path, value, replace=True)
    del api.accounts[11]
    before = len(api.calls)
    assert operator.retire(api, output)["status"] == "PROVISIONER_RETIRED"
    assert api.calls[before:] == [("GET", "/api/serviceaccounts/11", None)]
