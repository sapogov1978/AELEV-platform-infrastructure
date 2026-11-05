"""The public operator adapter delegates without replenishing existing budgets."""

from __future__ import annotations

import copy
import io
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery
import recovery_cli as cli
from test_recovery import enrolled as base_enrolled_fixture  # noqa: F401


@pytest.fixture
def fixture(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    enrolled: dict[str, Any] = request.getfixturevalue("base_enrolled_fixture")
    root, row = enrolled["root"], enrolled["row"]
    root.chmod(0o700)
    (root / ".lock").touch(mode=0o600)
    row["Image"] = "sha256:" + "b" * 64
    row["Config"]["Labels"]["com.docker.compose.config-hash"] = "c" * 64
    current = json.loads((root / "state.json").read_text())
    current["targets"]["worker"]["expected"] = recovery.fingerprint(row)
    current["targets"]["unrelated"] = {"untouched": True}
    recovery.commit(root, current)
    monkeypatch.setattr(cli, "transport", recovery.docker)
    value = {
        "schema_version": 1,
        "daemon_id": "same-daemon",
        "action": "status",
        "targets": [{"name": "worker", "expected": recovery.fingerprint(row)}],
    }
    return {**enrolled, "request": value}


def test_status_never_writes_or_returns_other_targets(fixture: dict[str, Any]) -> None:
    root = fixture["root"]
    calls_before = len(fixture["calls"])
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    answer = cli.run(root, cli.request(json.dumps(fixture["request"]).encode()))
    assert set(answer["targets"]) == {"worker"}
    assert answer["targets"]["worker"]["attempts"] == 0
    assert answer["targets"]["worker"]["automatic_recovery"] is False
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    assert not any(
        c[0] in {"restart", "update"} for c in fixture["calls"][calls_before:]
    )


@pytest.mark.parametrize("action", ["status", "suspend", "replace"])
def test_public_actions_report_selected_automatic_mode_without_reset(
    fixture: dict[str, Any], action: str
) -> None:
    root, row = fixture["root"], fixture["row"]
    current = cli.state(root)
    current["schema_version"] = 2
    target = current["targets"]["worker"]
    target.update(
        attempts=674,
        policy_limit=0,
        automatic={"version": 1, "phase": "ACTIVE", "burst_attempts": 3},
    )
    row["HostConfig"]["RestartPolicy"] = {"Name": "no", "MaximumRetryCount": 0}
    value = fixture["request"]
    value["action"] = action
    if action == "replace":
        target.update(blocked=True, code="OPERATOR_SUSPENDED")
        value["targets"][0].update(
            previous_native_count=0, replacement_image="sha256:" + "d" * 64
        )
        row.update(Id="e" * 64, Image="sha256:" + "d" * 64)
    recovery.commit(root, current)
    answer = cli.run(root, value, host_timer_quiesced=True)
    public = answer["targets"]["worker"]
    assert public["automatic_recovery"] is True
    assert public["attempts"] == 674 and public["policy_limit"] == 0
    after = cli.state(root)
    assert after["targets"]["worker"]["automatic"]["burst_attempts"] == 3
    assert after["targets"]["unrelated"] == {"untouched": True}
    if action == "suspend":
        assert public["blocked"] is True and public["code"] == "OPERATOR_SUSPENDED"
    elif action == "replace":
        assert public["blocked"] is False and public["code"] == "AUTOMATIC_REPLACED"


@pytest.mark.parametrize("action", ["once", "reset", "enroll", "install", "exec", "*"])
def test_controller_and_reset_are_not_exposed(
    fixture: dict[str, Any], action: str
) -> None:
    fixture["request"]["action"] = action
    with pytest.raises(ValueError):
        cli.request(json.dumps(fixture["request"]).encode())


def test_desktop_write_requires_host_window(fixture: dict[str, Any]) -> None:
    fixture["request"]["action"] = "suspend"
    before = (fixture["root"] / "state.json").read_bytes()
    with pytest.raises(ValueError, match="HOST_MAINTENANCE_REQUIRED"):
        cli.run(fixture["root"], fixture["request"])
    assert (fixture["root"] / "state.json").read_bytes() == before


def test_suspend_uses_same_core_and_preserves_full_journal(
    fixture: dict[str, Any],
) -> None:
    root = fixture["root"]
    current = json.loads((root / "state.json").read_text())
    current["targets"]["worker"].update(attempts=2, native_count=1, policy_limit=1)
    recovery.commit(root, current)
    fixture["request"]["action"] = "suspend"
    cli.run(root, fixture["request"], host_timer_quiesced=True)
    after = json.loads((root / "state.json").read_text())
    expected = copy.deepcopy(current)
    expected["targets"]["worker"].update(blocked=True, code="OPERATOR_SUSPENDED")
    suspension_id = after["targets"]["worker"]["suspension_id"]
    assert len(suspension_id) == 64
    expected["targets"]["worker"]["suspension_id"] = suspension_id
    assert after == expected
    assert (
        after["docker"] == "/bin/docker" and after["endpoint"] == "unix:///local.sock"
    )


@pytest.mark.parametrize("attempts", [0, 2, 3, 5])
def test_replacement_reuses_core_budget_and_lost_ack(
    fixture: dict[str, Any], attempts: int
) -> None:
    root, row = fixture["root"], fixture["row"]
    current = json.loads((root / "state.json").read_text())
    current["targets"]["worker"].update(
        attempts=attempts, code="OPERATOR_SUSPENDED", blocked=True
    )
    recovery.commit(root, current)
    value = fixture["request"]
    value["action"] = "replace"
    value["targets"][0].update(
        previous_native_count=0, replacement_image="sha256:" + "d" * 64
    )
    row.update(Id="e" * 64, Image="sha256:" + "d" * 64)
    first = cli.run(root, value, host_timer_quiesced=True)
    before_retry = (root / "state.json").read_bytes()
    assert cli.run(root, value, host_timer_quiesced=True) == first
    assert (root / "state.json").read_bytes() == before_retry
    assert first["targets"]["worker"]["attempts"] == attempts
    assert first["targets"]["worker"]["policy_limit"] == max(0, 3 - attempts)
    assert json.loads(before_retry)["targets"]["unrelated"] == {"untouched": True}


def relocation_request(fixture: dict[str, Any]) -> tuple[dict[str, Any], str]:
    root, row = fixture["root"], fixture["row"]
    current = json.loads((root / "state.json").read_text())
    target = current["targets"]["worker"]
    target.update(
        attempts=11,
        policy_limit=0,
        automatic={"version": 1, "phase": "ACTIVE", "burst_attempts": 3},
    )
    recovery.commit(root, current)
    replacement_name = "aelev_research_management-policy-management-api-1"
    row.update(
        Id="e" * 64,
        Image="sha256:" + "d" * 64,
        Name="/" + replacement_name,
        RestartCount=0,
    )
    row["Config"]["Labels"].update(
        {
            "com.docker.compose.project": "aelev_research_management",
            "com.docker.compose.service": "policy-management-api",
            "com.docker.compose.config-hash": "f" * 64,
        }
    )
    row["HostConfig"]["RestartPolicy"] = {"Name": "no", "MaximumRetryCount": 0}
    value = fixture["request"]
    value["action"] = "relocate"
    value["targets"][0].update(
        previous_native_count=0,
        replacement_name=replacement_name,
        replacement=recovery.fingerprint(row),
    )
    return value, replacement_name


def test_relocation_changes_identity_without_replenishing_history(
    fixture: dict[str, Any],
) -> None:
    value, replacement_name = relocation_request(fixture)
    answer = cli.run(fixture["root"], value, host_timer_quiesced=True)
    assert set(answer["targets"]) == {replacement_name}
    public = answer["targets"][replacement_name]
    assert public["attempts"] == 11 and public["policy_limit"] == 0
    assert public["automatic_recovery"] is True
    current = cli.state(fixture["root"])
    assert "worker" not in current["targets"]
    target = current["targets"][replacement_name]
    assert target["code"] == "AUTOMATIC_RELOCATED" and not target["blocked"]
    assert target["relocation"]["previous_name"] == "worker"
    assert target["relocation"]["phase"] == "COMPLETE"
    before = (fixture["root"] / "state.json").read_bytes()
    assert cli.run(fixture["root"], value, host_timer_quiesced=True) == answer
    assert (fixture["root"] / "state.json").read_bytes() == before


def test_relocation_reconciles_lost_native_policy_ack(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    value, replacement_name = relocation_request(fixture)
    original = recovery.docker
    interrupted = [False]

    def docker(state: dict[str, Any], *args: str) -> bytes:
        answer = original(state, *args)
        if args[0] == "update" and not interrupted[0]:
            interrupted[0] = True
            raise RuntimeError("lost acknowledgement")
        return answer

    monkeypatch.setattr(recovery, "docker", docker)
    monkeypatch.setattr(cli, "transport", docker)
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        cli.run(fixture["root"], value, host_timer_quiesced=True)
    pending = cli.state(fixture["root"])["targets"]["worker"]
    assert pending["relocation"]["phase"] == "POLICY_PENDING"
    answer = cli.run(fixture["root"], value, host_timer_quiesced=True)
    assert answer["targets"][replacement_name]["attempts"] == 11
    assert "worker" not in cli.state(fixture["root"])["targets"]


def test_relocation_preserves_and_explicitly_releases_existing_block(
    fixture: dict[str, Any],
) -> None:
    value, replacement_name = relocation_request(fixture)
    current = cli.state(fixture["root"])
    current["targets"]["worker"].update(
        blocked=True, code="AUTOMATIC_EXIT_UNCONFIRMED"
    )
    recovery.commit(fixture["root"], current)
    row = value["targets"][0]
    recovery.relocate(
        fixture["root"],
        row["name"],
        row["expected"]["id"],
        row["previous_native_count"],
        row["replacement_name"],
        row["replacement"],
        preserve_block=True,
    )
    current = cli.state(fixture["root"])
    target = current["targets"][replacement_name]
    assert target["blocked"] and target["code"] == "AUTOMATIC_EXIT_UNCONFIRMED"
    assert target["relocation"]["preserved_block"] == {
        "code": "AUTOMATIC_EXIT_UNCONFIRMED"
    }
    proof = recovery.relocation_release_proof(target)
    release = {
        "schema_version": 1,
        "daemon_id": "same-daemon",
        "action": "release-relocation",
        "targets": [
            {
                "name": replacement_name,
                "expected": row["replacement"],
                "relocation_proof": proof,
            }
        ],
    }
    answer = cli.run(fixture["root"], release, host_timer_quiesced=True)
    public = answer["targets"][replacement_name]
    assert not public["blocked"] and public["code"] == "AUTOMATIC_RELOCATED"
    after = cli.state(fixture["root"])["targets"][replacement_name]
    assert after["attempts"] == 11
    assert after["relocation"]["release"]["proof"] == proof


def test_relocation_release_does_not_replenish_exhausted_manual_budget(
    fixture: dict[str, Any],
) -> None:
    value, replacement_name = relocation_request(fixture)
    current = cli.state(fixture["root"])
    target = current["targets"]["worker"]
    target.pop("automatic")
    target.update(attempts=recovery.LIMIT, blocked=True, code="CIRCUIT_OPEN")
    recovery.commit(fixture["root"], current)
    row = value["targets"][0]
    recovery.relocate(
        fixture["root"],
        row["name"],
        row["expected"]["id"],
        row["previous_native_count"],
        row["replacement_name"],
        row["replacement"],
        preserve_block=True,
    )
    target = cli.state(fixture["root"])["targets"][replacement_name]
    recovery.release_relocation(
        fixture["root"],
        replacement_name,
        row["replacement"]["id"],
        recovery.relocation_release_proof(target),
    )
    after = cli.state(fixture["root"])["targets"][replacement_name]
    assert after["attempts"] == recovery.LIMIT
    assert after["blocked"] is True and after["code"] == "CIRCUIT_OPEN"


def test_all_scope_checked_before_first_suspend(fixture: dict[str, Any]) -> None:
    value = fixture["request"]
    value["action"] = "suspend"
    value["targets"].append({**value["targets"][0], "name": "unregistered"})
    before = (fixture["root"] / "state.json").read_bytes()
    with pytest.raises(ValueError, match="NOT_ENROLLED"):
        cli.run(fixture["root"], value, host_timer_quiesced=True)
    assert (fixture["root"] / "state.json").read_bytes() == before


def test_wrong_daemon_and_foreign_id_fail_before_write(fixture: dict[str, Any]) -> None:
    value = fixture["request"]
    value["targets"][0]["expected"]["id"] = "f" * 64
    with pytest.raises(ValueError, match="TARGET_CHANGED"):
        cli.run(fixture["root"], value)
    value["daemon_id"] = "other"
    with pytest.raises(ValueError, match="DAEMON_MISMATCH"):
        cli.run(fixture["root"], value)


def test_transport_remap_does_not_modify_state(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(return_value=b"ok")
    monkeypatch.setattr(recovery, "execute", run)
    current = {"docker": "/host/docker", "endpoint": "unix:///host/socket"}
    assert cli.transport(current, "info") == b"ok"
    assert current == {"docker": "/host/docker", "endpoint": "unix:///host/socket"}
    run.assert_called_once_with(
        ["/usr/local/bin/docker", "--host", "unix:///var/run/docker.sock", "info"]
    )


def test_private_journal_links_and_permissions_rejected(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    real = tmp_path / "real"
    real.write_text('{"schema_version":1,"targets":{}}')
    (tmp_path / "state.json").symlink_to(real)
    with pytest.raises(OSError):
        cli.state(tmp_path)
    (tmp_path / "state.json").unlink()
    os.link(real, tmp_path / "state.json")
    with pytest.raises(ValueError, match="JOURNAL_INVALID"):
        cli.state(tmp_path)


def test_container_protocol_reads_a_line_not_eof(
    fixture: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = Mock(spec=io.BufferedReader)
    stream.readline.return_value = json.dumps(fixture["request"]).encode() + b"\n"
    stream.read.side_effect = AssertionError("must not wait for interactive EOF")
    monkeypatch.setattr(sys, "stdin", Mock(buffer=stream))
    monkeypatch.setattr(sys, "argv", ["recovery_cli.py"])
    run = Mock(return_value={"confirmed": True})
    monkeypatch.setattr(cli, "run", run)
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"confirmed": True}
    stream.readline.assert_called_once_with(cli.LIMIT + 1)
    stream.read.assert_not_called()
