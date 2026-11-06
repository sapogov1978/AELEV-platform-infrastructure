"""An administrator's retry must not silently issue a new authorization."""

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from aelev_spire import operator
from aelev_spire.bootstrap_export import consume
from aelev_spire.files import atomic

Setup = tuple[dict[str, Any], Path, Path, Path, dict[str, Any]]


@pytest.fixture
def setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], Path, Path, Path, dict[str, Any]]:
    request = {
        "owner": "infrastructure-spire",
        "schema_version": 1,
        "environment": "test",
        "trust_domain": "test.example",
        "svid_ttl_seconds": 20,
        "resource_owner": "fixture-owner",
        "operation_id": str(uuid.uuid4()),
        "node_id": str(uuid.uuid4()),
        "spiffe_id": "spiffe://test.example/fixture-owner/component/instance",
        "dns_names": ["localhost"],
        "actor": "fixture-operator",
        "server_address": "spire-server",
    }
    path = tmp_path / "request.json"
    atomic(path, request)
    socket = tmp_path / "admin/api.sock"
    atomic(
        socket.parent / "deployment.json",
        {
            "owner": "infrastructure-spire",
            "environment": "test",
            "trust_domain": "test.example",
        },
    )
    monkeypatch.setattr(operator, "SERVER_SOCKET", str(socket))
    state: dict[str, Any] = {"entries": [], "token_calls": 0, "create_calls": 0}

    def native(*arguments: str) -> bytes:
        if arguments[:2] == ("token", "generate"):
            state["token_calls"] += 1
            return json.dumps({"value": str(uuid.uuid4())}).encode()
        if arguments[:2] == ("entry", "create"):
            state["create_calls"] += 1
            state["entries"] = [
                {
                    "id": request["operation_id"],
                    "spiffe_id": {
                        "trust_domain": "test.example",
                        "path": "/fixture-owner/component/instance",
                    },
                    "parent_id": {
                        "trust_domain": "test.example",
                        "path": f"/node/fixture-owner/{request['node_id']}",
                    },
                    "x509_svid_ttl": 20,
                    "selectors": [
                        {"type": "unix", "value": "uid:10001"},
                        {"type": "unix", "value": "path:" + operator.AGENT},
                    ],
                    "dns_names": ["localhost"],
                }
            ]
            return b"{}"
        if arguments[:2] == ("entry", "delete"):
            state["entries"] = []
            return b"{}"
        if arguments[:2] == ("entry", "show"):
            return json.dumps({"entries": state["entries"]}).encode()
        if arguments[:2] == ("bundle", "show"):
            return b"isolated fixture, not real trust"
        raise AssertionError("Unexpected fixed native command")

    monkeypatch.setattr(operator, "native", native)
    return request, path, tmp_path / "operator", tmp_path / "bootstrap", state


def test_repeat_preserves_one_authorization_and_revoke_is_terminal(
    setup: Setup,
) -> None:
    _, path, root, output, state = setup
    operator.operate("authorize", path, root, output)
    operator.operate("authorize", path, root, output)
    assert state["token_calls"] == state["create_calls"] == 1
    operator.operate("revoke", path, root, output)
    with pytest.raises(ValueError, match="REVOKED"):
        operator.operate("authorize", path, root, output)
    assert state["entries"] == []
    assert state["token_calls"] == state["create_calls"] == 1


def test_external_removal_is_not_reconciled_back_into_existence(setup: Setup) -> None:
    _, path, root, output, state = setup
    operator.operate("authorize", path, root, output)
    state["entries"] = []
    with pytest.raises(ValueError, match="REVOKED"):
        operator.operate("authorize", path, root, output)
    assert state["create_calls"] == state["token_calls"] == 1


def test_ambiguous_token_issue_does_not_mint_another_right(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, path, root, output, state = setup
    native = operator.native

    def lose_response(*arguments: str) -> bytes:
        native(*arguments)
        raise OSError("Lost native acknowledgement")

    monkeypatch.setattr(operator, "native", lose_response)
    with pytest.raises(OSError):
        operator.operate("authorize", path, root, output)
    monkeypatch.setattr(operator, "native", native)
    with pytest.raises(ValueError, match="AMBIGUOUS_TOKEN"):
        operator.operate("authorize", path, root, output)
    assert state["token_calls"] == 1


def test_unknown_privilege_or_owner_prefix_is_refused(setup: Setup) -> None:
    request, _, _, _, _ = setup
    for changed in (
        {**request, "admin": True},
        {**request, "spiffe_id": "spiffe://test.example/another-owner/instance"},
    ):
        with pytest.raises(ValueError):
            operator.validate(changed)


def test_lost_entry_ack_reuses_exact_entry_and_audit_has_no_token(
    setup: Setup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, path, root, output, state = setup
    native = operator.native

    def lose_create_ack(*arguments: str) -> bytes:
        result = native(*arguments)
        if arguments[:2] == ("entry", "create"):
            raise OSError("Lost create acknowledgement")
        return result

    monkeypatch.setattr(operator, "native", lose_create_ack)
    with pytest.raises(OSError):
        operator.operate("authorize", path, root, output)
    monkeypatch.setattr(operator, "native", native)
    operator.operate("authorize", path, root, output)
    assert state["create_calls"] == state["token_calls"] == 1
    history = (root / "actions.jsonl").read_text()
    assert (output / "join-token").read_text() not in history
    assert '"token"' not in history
    operator.operate("revoke", path, root, output)
    operator.operate("revoke", path, root, output)


def test_native_not_found_is_not_confused_with_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def result(error: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            [],
            1,
            json.dumps({"error": error}).encode(),
            b"",
        )

    error = "failed to show: rpc error: code = NotFound desc = missing entry"

    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return result(error)

    monkeypatch.setattr(operator.subprocess, "run", run)
    with pytest.raises(operator.EntryNotFound):
        operator.native("entry", "show", "-entryID", "fixture")
    error = "rpc error: code = DeadlineExceeded desc = request timed out"
    with pytest.raises(ValueError, match="NATIVE_REQUEST_FAILED"):
        operator.native("entry", "show", "-entryID", "fixture")


def test_export_reuses_exact_material_and_never_issues_another_right(
    setup: Setup,
) -> None:
    request, path, root, output, state = setup
    operator.operate("authorize", path, root, output)
    target = output.parent / "export"
    first = operator.operate("export-bootstrap", path, root, output, export_path=target)
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    second = operator.operate(
        "export-bootstrap", path, root, output, export_path=target
    )
    assert first == second
    assert first["phase"] == "EXPORTED"
    assert first["node_id"] == request["node_id"]
    assert before == {p.name: p.read_bytes() for p in target.iterdir()}
    assert (target / "join-token").read_bytes() == (output / "join-token").read_bytes()
    assert (target / "join-token").stat().st_mode & 0o777 == 0o600
    assert set(before) == {
        "owner.json",
        "plan.json",
        "join-token",
        "bundle.crt",
        "receipt.json",
    }
    assert state["create_calls"] == state["token_calls"] == 1
    assert state.get("token") is None
    assert (target / "join-token").read_text() not in json.dumps(first)


@pytest.mark.parametrize(
    "failure", ["expired", "revoked", "removed", "scope", "intent"]
)
def test_export_fails_closed_without_native_mutation(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    request, path, root, output, state = setup
    operator.operate("authorize", path, root, output)
    target = output.parent / "export"
    saved = json.loads(
        (root / "operations" / (request["operation_id"] + ".json")).read_bytes()
    )
    if failure == "expired":
        monkeypatch.setattr(
            operator.time, "time", lambda: saved["token_expires_at"] + 1
        )
    elif failure == "revoked":
        operator.operate("revoke", path, root, output)
    elif failure == "removed":
        state["entries"] = []
    elif failure == "scope":
        state["entries"][0]["admin"] = True
    else:
        saved["phase"] = "ISSUING_TOKEN"
        atomic(root / "operations" / (request["operation_id"] + ".json"), saved)
    with pytest.raises(ValueError):
        operator.operate("export-bootstrap", path, root, output, export_path=target)
    assert not target.exists()
    assert state["create_calls"] == state["token_calls"] == 1


@pytest.mark.parametrize("failure", ["token", "plan", "link", "drift", "foreign"])
def test_export_preserves_unknown_or_changed_material(
    setup: Setup, failure: str
) -> None:
    request, path, root, output, state = setup
    operator.operate("authorize", path, root, output)
    target = output.parent / "export"
    if failure == "token":
        atomic(output / "join-token", b"another-value")
    elif failure == "plan":
        atomic(output / "plan.json", {"node_id": request["node_id"]})
    elif failure == "link":
        (output / "join-token").unlink()
        (output / "join-token").symlink_to(output / "bundle.crt")
    elif failure == "foreign":
        atomic(target / "keep.txt", b"foreign")
    else:
        operator.operate("export-bootstrap", path, root, output, export_path=target)
        atomic(target / "join-token", b"changed")
    with pytest.raises(ValueError):
        operator.operate("export-bootstrap", path, root, output, export_path=target)
    assert state["create_calls"] == state["token_calls"] == 1
    if failure == "drift":
        assert (target / "join-token").read_bytes() == b"changed"


def test_export_without_authorization_does_not_create_intent(setup: Setup) -> None:
    _, path, root, output, state = setup
    with pytest.raises(ValueError, match="INTENT_NOT_FOUND"):
        operator.operate(
            "export-bootstrap", path, root, output, export_path=output.parent / "export"
        )
    assert not (root / "operations").exists()
    assert state["create_calls"] == state["token_calls"] == 0


def test_exported_package_consumption_checks_original_expiry_and_exact_bytes(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, path, root, output, _ = setup
    operator.operate("authorize", path, root, output)
    target = output.parent / "export"
    result = operator.operate(
        "export-bootstrap", path, root, output, export_path=target
    )
    assert (
        consume(target, request["operation_id"])["join-token"]
        == (output / "join-token").read_bytes()
    )
    with pytest.raises(ValueError, match="PACKAGE_INVALID"):
        consume(target, str(uuid.uuid4()))
    atomic(target / "bundle.crt", b"changed trust")
    with pytest.raises(ValueError, match="PACKAGE_INVALID"):
        consume(target, request["operation_id"])
    atomic(target / "bundle.crt", (output / "bundle.crt").read_bytes())
    monkeypatch.setattr(operator.time, "time", lambda: result["expires_at"] + 1)
    with pytest.raises(ValueError, match="AUTHORIZATION_EXPIRED"):
        consume(target, request["operation_id"])
