"""Operator renewal preserves approval, revocation, TLS and runtime isolation."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import ssl
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_authorization import grant
from test_gateway import _config  # pyright: ignore[reportPrivateUsage]

import gateway
import prepare
import provision
import renewal


@pytest.fixture
def installed(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    issuer, client, grants, audit, status, state = (
        tmp_path / name
        for name in ("issuer", "client", "grants", "audit", "status", "state")
    )
    for directory in (issuer, grants, audit, status):
        directory.mkdir()
    prepare.root_certificate(issuer)
    identity = "spiffe://example.test/workload/client-a"
    prepare.issue(issuer, client, "client", "URI:" + identity, "clientAuth")
    (client / "client.pem").rename(client / "client.crt")
    renewal.renew_certificate(
        client / "client.crt", client / "client.key", issuer, identity=identity
    )
    shutil.copyfile(issuer / "ca.pem", client / "ca.crt")
    source = client / "grant.json"
    source.write_text(json.dumps(grant(identity)))
    provision.apply(source, grants, audit, "test-operator")
    renewal.enroll(
        argparse.Namespace(
            state_directory=state,
            grant=source,
            issuer=issuer,
            client_directory=client,
            status_directory=status,
            grants_directory=grants,
            audit_directory=audit,
            server_directory=None,
            actor="test-operator",
        )
    )
    return state, client, grants, audit


def test_renewal_extends_only_expiry_and_preserves_key(
    installed: tuple[Path, Path, Path, Path],
) -> None:
    state, client, grants, audit = installed
    before = renewal.read_json(client / "grant.json")
    old_key = (client / "client.key").read_bytes()
    old_certificate = (client / "client.crt").read_bytes()
    result = renewal.run(state, renew_now=True)
    assert result["healthy"] is True
    assert result["code"] == "READY"
    after = renewal.read_json(grants / provision.identity_filename(before["identity"]))
    assert renewal.scope(after) == renewal.scope(before)
    assert after["expires_at"] != before["expires_at"]
    assert (client / "client.key").read_bytes() == old_key
    assert (client / "client.crt").read_bytes() != old_certificate
    assert renewal.run(state)["last_renewed_at"] == result["last_renewed_at"]
    assert provision.verify_audit(audit / "operator-audit.jsonl")[0] >= 8


@pytest.mark.parametrize("reapply", [False, True])
def test_revocation_is_final_even_after_reapply(
    installed: tuple[Path, Path, Path, Path], reapply: bool
) -> None:
    state, client, grants, audit = installed
    identity = renewal.read_json(client / "grant.json")["identity"]
    provision.revoke(identity, grants, audit, "test-operator")
    if reapply:
        provision.apply(client / "grant.json", grants, audit, "test-operator")
    result = renewal.run(state, renew_now=True)
    assert result["healthy"] is False
    assert result["code"] == "APPROVAL_REVOKED"
    assert (grants / provision.identity_filename(identity)).exists() is reapply


@pytest.mark.parametrize(
    "change",
    ["scope", "expiry_changed", "expired", "missing", "issuer", "audit", "pending"],
)
def test_fail_closed_with_diagnostic(
    installed: tuple[Path, Path, Path, Path], change: str
) -> None:
    state, client, grants, audit = installed
    approval = renewal.read_json(state / "approval.json")
    target = grants / provision.identity_filename(approval["identity"])
    before = (client / "client.crt").read_bytes()
    if change == "scope":
        value = renewal.read_json(target) | {"max_concurrency": 2}
        source = state / "changed.json"
        source.write_text(json.dumps(value))
        provision.apply(source, grants, audit, "test-operator")
    elif change in {"expired", "expiry_changed"}:
        value = renewal.read_json(target) | {
            "expires_at": (
                datetime.now(UTC) + timedelta(seconds=-1 if change == "expired" else 60)
            ).isoformat()
        }
        target.write_text(json.dumps(value))
    elif change == "missing":
        target.unlink()
    elif change == "issuer":
        (Path(approval["issuer"]) / "ca.pem").write_text("invalid")
    elif change == "audit":
        (audit / "operator-audit.jsonl").write_text("invalid\n")
    else:
        provision.audit(
            audit,
            "apply",
            approval["identity"],
            "",
            "test-operator",
            "interrupted",
            "INTENT_RECORDED",
        )
    result = renewal.run(state, renew_now=True)
    expected = {
        "scope": "APPROVED_SCOPE_CHANGED",
        "expiry_changed": "CURRENT_GRANT_CHANGED",
        "expired": "EGRESS_GRANT_EXPIRED",
        "missing": "GRANT_REVOKED_OR_MISSING",
        "issuer": "ISSUER_CHANGED",
        "audit": "EGRESS_GRANT_INVALID",
        "pending": "AUDIT_RECONCILIATION_REQUIRED",
    }
    assert result["code"] == expected[change]
    assert result["healthy"] is False
    assert result["last_failure_code"] == result["code"]
    assert (client / "client.crt").read_bytes() == before


def test_ca_replacement_is_not_automatic(
    installed: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    state, _, _, _ = installed
    original = renewal.expiry

    def short_ca(path: Path) -> datetime:
        return (
            datetime.now(UTC) + timedelta(days=5)
            if path.name == "ca.pem"
            else original(path)
        )

    monkeypatch.setattr(renewal, "expiry", short_ca)
    assert renewal.run(state, renew_now=True)["code"] == "CA_REPLACEMENT_REQUIRED"


def test_hot_reload_real_tls_preserves_listener_and_bad_candidate(
    tmp_path: Path,
) -> None:
    issuer, server = tmp_path / "issuer", tmp_path / "server"
    issuer.mkdir()
    prepare.root_certificate(issuer)
    prepare.issue(issuer, server, "server", "DNS:localhost,IP:127.0.0.1", "serverAuth")
    config = replace(
        _config(),
        bind_host="127.0.0.1",
        port=0,
        server_cert_path=str(server / "server.pem"),
        server_key_path=str(server / "server.key"),
        client_ca_path=str(issuer / "ca.pem"),
    )
    listener = gateway.build_server(config)
    assert isinstance(listener, gateway.GatewayServer)
    thread = threading.Thread(target=listener.serve_forever, daemon=True)
    thread.start()
    context = ssl.create_default_context(cafile=str(issuer / "ca.pem"))

    def probe() -> str:
        connection = http.client.HTTPSConnection(
            "localhost", listener.server_port, context=context, timeout=3
        )
        connection.connect()
        assert connection.sock is not None
        digest = hashlib.sha256(
            connection.sock.getpeercert(binary_form=True) or b""
        ).hexdigest()
        connection.request("GET", "/healthz")
        assert connection.getresponse().status == 200
        connection.close()
        return digest

    try:
        first = probe()
        renewal.renew_certificate(
            server / "server.pem", server / "server.key", issuer, identity=None
        )
        listener.tls_checked_at = 0
        second = probe()
        assert second != first
        valid = (server / "server.pem").read_bytes()
        renewal.atomic(server / "server.pem", b"invalid")
        listener.tls_checked_at = 0
        assert probe() == second
        renewal.atomic(server / "server.pem", valid)
        assert listener.context.verify_mode == ssl.CERT_OPTIONAL
    finally:
        listener.shutdown()
        thread.join(timeout=3)
        listener.server_close()


def test_private_approval_and_no_runtime_operator_code(
    installed: tuple[Path, Path, Path, Path],
) -> None:
    state, _, _, _ = installed
    assert os.stat(state / "approval.json").st_mode & 0o777 == 0o600
    assert os.stat(state).st_mode & 0o777 == 0o700
    dockerfile = Path("Dockerfile").read_text().split("FROM base AS test")[0]
    assert "renewal.py" not in dockerfile
    assert "provision.py" not in dockerfile


def test_server_renewal_is_independent_of_client_approval_and_revoke(
    installed: tuple[Path, Path, Path, Path],
) -> None:
    state, _client, grants, audit = installed
    approval = renewal.read_json(state / "approval.json")
    issuer = Path(approval["issuer"])
    server = state.parent / "server"
    prepare.issue(issuer, server, "server", "DNS:localhost,IP:127.0.0.1", "serverAuth")
    server_state = state.parent / "server-state"
    renewal.enroll_server(
        argparse.Namespace(
            state_directory=server_state,
            issuer=issuer,
            server_directory=server,
            status_directory=Path(approval["status_directory"]),
            audit_directory=audit,
            identity="spiffe://example.test/workload/gateway-server",
            actor="test-operator",
        )
    )
    # A legacy monitor path must not give a client task server-renewal authority.
    approval["server_directory"] = str(server)
    renewal.store(state / "approval.json", approval, 0o600)
    before = (server / "server.pem").read_bytes()
    assert renewal.run(state, renew_now=True)["healthy"] is True
    assert (server / "server.pem").read_bytes() == before
    provision.revoke(approval["identity"], grants, audit, "test-operator")
    assert renewal.run(state, renew_now=True)["code"] == "APPROVAL_REVOKED"
    assert renewal.run(server_state, renew_now=True)["healthy"] is True
    assert (server / "server.pem").read_bytes() != before
