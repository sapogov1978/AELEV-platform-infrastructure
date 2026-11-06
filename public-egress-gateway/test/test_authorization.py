"""Generic policy isolation; never embed a functional client's declaration."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from test_gateway import _config, _fetch_document  # pyright: ignore[reportPrivateUsage]

import authorization
import gateway
import provision


def grant(
    identity: str = "spiffe://example.test/workload/client-a", **changes: object
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": 1,
        "identity": identity,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "mode": "EXPLORATION",
        "max_response_bytes": 131072,
        "timeout_seconds": 1,
        "max_concurrency": 1,
        "per_host_interval_seconds": 0.1,
        "credential_scopes": {},
        "post_scopes": {},
    }
    return result | changes


def install(root: Path, declaration: dict[str, object]) -> None:
    source = root / "requested-grant"
    source.write_text(json.dumps(declaration))
    audit_root = root.parent / "audit"
    audit_root.mkdir(exist_ok=True)
    provision.apply(source, root, audit_root, "unit-operator")


def test_identities_cannot_borrow_regular_scopes(tmp_path: Path) -> None:
    first = "spiffe://example.test/workload/client-a"
    second = "spiffe://example.test/workload/client-b"
    install(tmp_path, grant(first))
    install(
        tmp_path,
        grant(
            second,
            mode="REGULAR",
            credential_scopes={"authorization": ["api.example.org"]},
            post_scopes={"api.example.org": ["/session"]},
        ),
    )
    identities = gateway.IdentityApplications(
        replace(_config(), grants_directory=tmp_path)
    )
    document = _fetch_document(
        "https://api.example.org/session",
        headers={"authorization": "opaque-unit-test-input"},
    )
    identities.authorized({second}).parse_fetch(document)
    with pytest.raises(gateway.GatewayRejected, match="CREDENTIAL_BLOCKED"):
        identities.authorized({first}).parse_fetch(document)
    with pytest.raises(authorization.GrantRejected, match="IDENTITY_BLOCKED"):
        identities.authorized({"spiffe://example.test/workload/unknown"})
    with pytest.raises(authorization.GrantRejected, match="IDENTITY_BLOCKED"):
        identities.authorized({first, second})
    request = _fetch_document("http://127.0.0.1/", headers={})
    app = identities.authorized({second})
    with pytest.raises(gateway.GatewayRejected, match="ADDRESS_NOT_GLOBAL"):
        app.addresses.resolve(app.parse_fetch(request).hostname, 80)


def test_quota_and_rate_state_are_independent_and_survive_grant_edits(
    tmp_path: Path,
) -> None:
    first = "spiffe://example.test/workload/client-a"
    second = "spiffe://example.test/workload/client-b"
    install(tmp_path, grant(first))
    install(tmp_path, grant(second))
    identities = gateway.IdentityApplications(
        replace(_config(), grants_directory=tmp_path)
    )
    a, b = identities.authorized({first}), identities.authorized({second})
    assert a.concurrency.acquire(limit=a.config.max_concurrency)
    assert not a.concurrency.acquire(limit=a.config.max_concurrency)
    assert b.concurrency.acquire(limit=b.config.max_concurrency)
    assert a.rate_limiter is not b.rate_limiter
    install(tmp_path, grant(first, max_response_bytes=50000))
    updated = identities.authorized({first})
    assert updated.concurrency is a.concurrency
    assert not updated.concurrency.acquire(limit=updated.config.max_concurrency)
    assert updated.config.max_response_bytes == 50000
    a.concurrency.release()
    b.concurrency.release()


def test_hot_expiry_revoke_and_global_ceilings(tmp_path: Path) -> None:
    document = grant(
        max_response_bytes=10000000,
        timeout_seconds=500,
        max_concurrency=100,
        per_host_interval_seconds=0,
    )
    identity = cast(str, document["identity"])
    install(tmp_path, document)
    base = replace(_config(), grants_directory=tmp_path, per_host_interval_seconds=0.25)
    identities = gateway.IdentityApplications(base)
    scoped = identities.authorized({identity}).config
    assert (
        scoped.max_response_bytes,
        scoped.timeout_seconds,
        scoped.max_concurrency,
        scoped.per_host_interval_seconds,
    ) == (1000000, 2, 4, 0.25)
    path = tmp_path / authorization.identity_filename(identity)
    document["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(document))
    with pytest.raises(authorization.GrantRejected, match="EXPIRED"):
        identities.authorized({identity})
    install(tmp_path, grant())
    identities.authorized({identity})
    provision.revoke(identity, tmp_path, tmp_path.parent / "audit", "unit-operator")
    with pytest.raises(authorization.GrantRejected, match="IDENTITY_BLOCKED"):
        identities.authorized({identity})


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "OTHER"},
        {"mode": []},
        {"max_concurrency": True},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"max_response_bytes": 0},
        {"per_host_interval_seconds": -1},
        {"expires_at": "2026-10-12T10:00:00"},
        {"identity": "spiffe://example.test/workload/../other"},
        {"identity": "spiffe://["},
        {"extra": True},
        {"credential_scopes": {"authorization": ["api.example.org"]}},
        {"mode": "REGULAR", "credential_scopes": {"cookie": ["api.example.org"]}},
        {"mode": "REGULAR", "post_scopes": {"*.example.org": ["/session"]}},
    ],
)
def test_grants_reject_invalid_or_self_expanding_input(
    changes: dict[str, object],
) -> None:
    with pytest.raises(authorization.GrantRejected):
        authorization.Grant.parse(grant() | changes)


def test_duplicate_json_and_external_symlinks_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(authorization.GrantRejected):
        authorization.strict_json('{"identity":"a","identity":"b"}')
    declaration = grant()
    source = tmp_path / "source"
    source.write_text(json.dumps(declaration))
    directory = tmp_path / "grants"
    directory.mkdir()
    (
        directory / authorization.identity_filename(cast(str, declaration["identity"]))
    ).symlink_to(source)
    with pytest.raises(authorization.GrantRejected):
        authorization.GrantStore(directory).load(cast(str, declaration["identity"]))


def test_projected_grant_within_trusted_mount_is_valid(tmp_path: Path) -> None:
    document = grant()
    name = authorization.identity_filename(cast(str, document["identity"]))
    snapshot = tmp_path / ".snapshot"
    snapshot.mkdir()
    (snapshot / name).write_text(json.dumps(document))
    (tmp_path / "..data").symlink_to(snapshot, target_is_directory=True)
    (tmp_path / name).symlink_to(Path("..data") / name)
    assert (
        authorization.GrantStore(tmp_path).load(cast(str, document["identity"])).mode
        == "EXPLORATION"
    )


def test_foreign_trust_domain_cannot_use_an_approved_file(tmp_path: Path) -> None:
    identity = "spiffe://foreign.test/workload/client-a"
    install(tmp_path, grant(identity))
    identities = gateway.IdentityApplications(
        replace(_config(), grants_directory=tmp_path)
    )
    with pytest.raises(authorization.GrantRejected, match="IDENTITY_BLOCKED"):
        identities.authorized({identity})


def test_request_cannot_inject_identity_or_boolean_limits() -> None:
    app = gateway.GatewayApplication(_config(mode="REGULAR"))
    with pytest.raises(gateway.GatewayRejected, match="REQUEST_INVALID"):
        app.parse_fetch(
            _fetch_document("https://example.org")
            | {"identity": "spiffe://example.test/workload/client-b"}
        )
    with pytest.raises(gateway.GatewayRejected, match="LIMIT_INVALID"):
        app.parse_fetch(_fetch_document("https://example.org") | {"max_bytes": True})
    with pytest.raises(gateway.GatewayRejected, match="CREDENTIAL_BLOCKED"):
        app.parse_fetch(
            _fetch_document(
                "https://download.example",
                headers={"authorization": "opaque-unit-test-input"},
                truncate=True,
            )
        )


def test_failed_local_audit_prevents_admission_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declaration = grant()
    source = tmp_path / "source"
    source.write_text(json.dumps(declaration))

    def fail(*arguments: object) -> None:
        del arguments
        raise OSError("isolated audit failure")

    monkeypatch.setattr(provision, "audit", fail)
    with pytest.raises(OSError):
        audit_root = tmp_path / "audit"
        audit_root.mkdir()
        provision.apply(source, tmp_path, audit_root, "unit-operator")
    assert not (
        tmp_path / authorization.identity_filename(cast(str, declaration["identity"]))
    ).exists()


def test_operator_audit_is_hash_chained_and_detects_tampering(tmp_path: Path) -> None:
    grants = tmp_path / "grants"
    audit_root = tmp_path / "audit"
    grants.mkdir()
    audit_root.mkdir()
    source = tmp_path / "requested.json"
    declaration = grant()
    source.write_text(json.dumps(declaration), encoding="utf-8")
    identity = cast(str, declaration["identity"])

    provision.apply(source, grants, audit_root, "operator-a")
    provision.revoke(identity, grants, audit_root, "operator-a")

    journal = audit_root / "operator-audit.jsonl"
    assert provision.verify_audit(journal)[0] == 4
    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [record["phase"] for record in records] == [
        "INTENT_RECORDED",
        "MUTATION_COMMITTED",
        "INTENT_RECORDED",
        "MUTATION_COMMITTED",
    ]
    assert "operator-a" not in journal.read_text()
    records[1]["phase"] = "TAMPERED"
    journal.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    with pytest.raises(ValueError, match="chain verification"):
        provision.verify_audit(journal)


def test_runtime_configuration_has_no_env_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "LISTEN_HOST": "0.0.0.0",
        "LISTEN_PORT": "8080",
        "DOCS_LISTEN_PORT": "8081",
        "PUBLIC_URL": "https://gateway.example.test:18497",
        "MAX_RESPONSE_BYTES": "1024",
        "TIMEOUT_SECONDS": "5",
        "MAX_CONCURRENCY": "4",
        "PER_HOST_INTERVAL_SECONDS": "0.25",
        "DENY_CIDRS": "",
        "SERVER_CERT_PATH": "/identity/server.pem",
        "SERVER_KEY_PATH": "/identity/server.key",
        "CLIENT_CA_PATH": "/identity/client-ca.pem",
        "GRANTS_DIRECTORY": "/grants",
        "TRUST_DOMAIN": "example.test",
    }
    for name, value in values.items():
        monkeypatch.setenv(gateway.PREFIX + name, value)
    assert gateway.GatewayConfig.from_env().max_response_bytes == 1024
    monkeypatch.setenv(gateway.PREFIX + "MAX_CONCURRENCY", "97")
    with pytest.raises(RuntimeError, match="MAX_CONCURRENCY"):
        gateway.GatewayConfig.from_env()
    monkeypatch.setenv(gateway.PREFIX + "MAX_CONCURRENCY", "4")
    monkeypatch.delenv(gateway.PREFIX + "MAX_RESPONSE_BYTES")
    with pytest.raises(RuntimeError, match="MAX_RESPONSE_BYTES"):
        gateway.GatewayConfig.from_env()
