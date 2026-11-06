"""The public client projection reads no keys and changes no operator state."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_renewal import installed

import provision
import renewal

_REGISTERED = installed


def snapshot(root: Path) -> dict[str, tuple[bytes, int, int, int]]:
    return {
        str(path.relative_to(root)): (
            path.read_bytes(),
            path.stat().st_ino,
            path.stat().st_mtime_ns,
            path.stat().st_mode,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def test_description_is_closed_public_projection_and_never_mutates(
    installed: tuple[Path, Path, Path, Path],
) -> None:
    state, client, _, _ = installed
    before = snapshot(state.parent)
    value = renewal.describe(state)
    assert value == {
        "owner": "public-egress-gateway",
        "schema_version": 1,
        "code": "EGRESS_RENEWAL_BINDING_VERIFIED",
        "identity": "spiffe://example.test/workload/client-a",
        "ca_sha256": hashlib.sha256((client / "ca.crt").read_bytes()).hexdigest(),
        "scope_sha256": renewal.grant_digest(
            renewal.scope(renewal.read_json(client / "grant.json"))
        ),
        "certificate_path": str(client / "client.crt"),
        "ca_path": str(client / "ca.crt"),
        "status_path": str(state.parent / "status/status.json"),
    }
    assert renewal.describe(state) == value
    assert snapshot(state.parent) == before


def test_description_has_no_dependency_on_private_keys_or_status(
    installed: tuple[Path, Path, Path, Path],
) -> None:
    state, client, _, _ = installed
    (client / "client.key").unlink()
    (state.parent / "issuer/ca.key").unlink()
    before = snapshot(state.parent)
    assert renewal.describe(state)["code"] == "EGRESS_RENEWAL_BINDING_VERIFIED"
    assert snapshot(state.parent) == before
    assert not (state.parent / "status/status.json").exists()


def test_scope_binding_survives_an_approved_same_scope_renewal(
    installed: tuple[Path, Path, Path, Path],
) -> None:
    state, _, _, _ = installed
    before = renewal.describe(state)
    assert renewal.run(state, renew_now=True)["healthy"]
    after = snapshot(state.parent)
    assert renewal.describe(state) == before
    assert snapshot(state.parent) == after


@pytest.mark.parametrize(
    "case",
    [
        "revoked",
        "reapplied",
        "scope",
        "missing",
        "expired",
        "issuer",
        "audit",
        "lock",
        "server",
        "approval",
        "pending",
        "expected",
        "sequence",
        "fifo-lock",
        "fifo-ca",
        "fifo-grant",
    ],
)
def test_description_fails_closed_without_repairing_anything(
    installed: tuple[Path, Path, Path, Path],
    case: str,
) -> None:
    state, client, grants, audit = installed
    approval = renewal.read_json(state / "approval.json")
    identity = approval["identity"]
    path = grants / provision.identity_filename(identity)
    if case in {"revoked", "reapplied"}:
        provision.revoke(identity, grants, audit, "test-operator")
        if case == "reapplied":
            provision.apply(client / "grant.json", grants, audit, "test-operator")
    elif case == "scope":
        value = renewal.read_json(path) | {"max_concurrency": 2}
        changed = state / "changed.json"
        renewal.store(changed, value)
        provision.apply(changed, grants, audit, "test-operator")
    elif case == "missing":
        path.unlink()
    elif case == "expired":
        renewal.store(
            path, renewal.read_json(path) | {"expires_at": "2000-01-01T00:00:00+00:00"}
        )
    elif case == "issuer":
        (state.parent / "issuer/ca.pem").write_bytes(b"changed-public-CA")
    elif case == "audit":
        (audit / "operator-audit.jsonl").write_bytes(b"broken\n")
    elif case == "lock":
        (audit / ".operator-mutation.lock").unlink()
    elif case == "server":
        renewal.store(state / "approval.json", approval | {"kind": "SERVER"}, 0o600)
    elif case == "pending":
        provision.audit(
            audit,
            "apply",
            identity,
            "",
            "test-operator",
            "interrupted",
            "INTENT_RECORDED",
        )
    elif case == "expected":
        renewal.store(state / "expected-grant.json", {"digest": "0" * 64}, 0o600)
    elif case == "sequence":
        renewal.store(
            state / "approval.json", approval | {"audit_sequence": 1000000}, 0o600
        )
    elif case.startswith("fifo-"):
        special = {
            "fifo-lock": audit / ".operator-mutation.lock",
            "fifo-ca": state.parent / "issuer/ca.pem",
            "fifo-grant": path,
        }[case]
        special.unlink()
        os.mkfifo(special, 0o600)
    else:
        renewal.store(
            state / "approval.json", approval | {"schema_version": True}, 0o600
        )
    before = snapshot(state.parent)
    with pytest.raises((ValueError, OSError)):
        renewal.describe(state)
    assert snapshot(state.parent) == before


def test_description_does_not_wait_for_or_interrupt_a_mutation(
    installed: tuple[Path, Path, Path, Path],
) -> None:
    state, _, _, audit = installed
    with (audit / ".operator-mutation.lock").open("rb") as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        with pytest.raises(
            renewal.RenewalBlocked, match="OPERATOR_MUTATION_IN_PROGRESS"
        ):
            renewal.describe(state)


def test_actual_cli_has_machine_readable_stdout_and_no_writes(
    installed: tuple[Path, Path, Path, Path],
) -> None:
    state, _, _, _ = installed
    before = snapshot(state.parent)
    answer = subprocess.run(
        [
            sys.executable,
            "-B",
            str(Path(renewal.__file__)),
            "describe",
            "--state-directory",
            str(state),
        ],
        capture_output=True,
        check=True,
        timeout=10,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    value: dict[str, Any] = json.loads(answer.stdout)
    assert value == renewal.describe(state)
    assert not answer.stderr
    assert snapshot(state.parent) == before
