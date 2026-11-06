"""Upgrade only the exact existing timer and preserve its canonical journal."""

from __future__ import annotations

import hashlib
import platform
import plistlib
import sys
from pathlib import Path
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery
from test_recovery import enrolled as base_enrolled_fixture  # noqa: F401


@pytest.fixture
def fixture(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    data = cast(dict[str, Any], request.getfixturevalue("base_enrolled_fixture"))
    root = data["root"]
    raw = b"# isolated predecessor\n"
    script = root / ("recovery-" + hashlib.sha256(raw).hexdigest() + ".py")
    script.write_bytes(raw)
    label = "com.aelev.recovery." + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    agents = tmp_path / "home/Library/LaunchAgents"
    agents.mkdir(parents=True)
    plist = agents / (label + ".plist")
    value = {
        "Label": label,
        "ProgramArguments": [
            "/pinned/python3.13",
            str(script),
            "once",
            "--state",
            str(root),
        ],
        "StartInterval": 15,
        "RunAtLoad": True,
        "EnvironmentVariables": {"PATH": "/fixed/bin"},
    }
    plist.write_bytes(plistlib.dumps(value))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    active = [True]
    calls: list[list[str]] = []
    monkeypatch.setattr(recovery, "timer_loaded", lambda _: active[0])

    def execute(arguments: list[str], *, timeout: float = 20) -> bytes:
        calls.append(arguments)
        if arguments[1] == "bootout":
            # The source tick must have finished before we stop its timer.
            import fcntl

            with (root / ".lock").open("a") as probe:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        active[0] = arguments[1] == "bootstrap"
        return b""

    monkeypatch.setattr(recovery, "execute", execute)
    return {
        **data,
        "active": active,
        "timer_calls": calls,
        "plist": plist,
        "settings": value,
    }


def test_upgrade_keeps_state_interpreter_timer_and_old_artifacts(
    fixture: dict[str, Any],
) -> None:
    root = fixture["root"]
    before = (root / "state.json").read_bytes()
    old = fixture["plist"].read_bytes()
    result = recovery.upgrade(root)
    assert result["journal_preserved"] is True and fixture["active"][0]
    assert (root / "state.json").read_bytes() == before
    settings = plistlib.loads(fixture["plist"].read_bytes())
    assert settings["ProgramArguments"][0] == "/pinned/python3.13"
    assert settings["ProgramArguments"][2:] == ["watch", "--state", str(root)]
    assert settings["StartInterval"] == 15 and settings["EnvironmentVariables"] == {
        "PATH": "/fixed/bin"
    }
    assert (
        root / "launchagent-history" / (hashlib.sha256(old).hexdigest() + ".plist")
    ).read_bytes() == old
    assert Path(fixture["settings"]["ProgramArguments"][1]).exists()
    assert recovery.upgrade(root)["code"] == "RECOVERY_TIMER_CURRENT"
    assert [c[1] for c in fixture["timer_calls"]] == ["bootout", "bootstrap"]


def test_upgrade_reconciles_lost_bootstrap_ack(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = recovery.execute

    def lost(arguments: list[str], *, timeout: float = 20) -> bytes:
        value = original(arguments, timeout=timeout)
        if arguments[1] == "bootstrap":
            raise RuntimeError("lost bootstrap response")
        return value

    monkeypatch.setattr(recovery, "execute", lost)
    with pytest.raises(RuntimeError):
        recovery.upgrade(fixture["root"])
    assert fixture["active"][0]
    assert recovery.upgrade(fixture["root"])["code"] == "RECOVERY_TIMER_CURRENT"
    assert [c[1] for c in fixture["timer_calls"]].count("bootstrap") == 1


def test_upgrade_waits_for_watch_to_finish_after_bootout(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = [0]

    def loaded(_domain: str) -> bool:
        if fixture["timer_calls"] and fixture["timer_calls"][-1][1] == "bootout":
            reads[0] += 1
            return reads[0] < 3
        return bool(fixture["active"][0])

    monkeypatch.setattr(recovery, "timer_loaded", loaded)
    result = recovery.upgrade(fixture["root"])
    assert result["journal_preserved"] and fixture["active"][0]
    assert reads[0] >= 3


def test_upgrade_does_not_enable_manually_stopped_timer(
    fixture: dict[str, Any],
) -> None:
    fixture["active"][0] = False
    with pytest.raises(ValueError, match="MANUALLY_STOPPED"):
        recovery.upgrade(fixture["root"])
    assert fixture["timer_calls"] == []


def test_completed_upgrade_does_not_enable_later_manually_stopped_timer(
    fixture: dict[str, Any],
) -> None:
    recovery.upgrade(fixture["root"])
    before = list(fixture["timer_calls"])
    fixture["active"][0] = False
    with pytest.raises(ValueError, match="MANUALLY_STOPPED"):
        recovery.upgrade(fixture["root"])
    assert fixture["timer_calls"] == before


def test_upgrade_does_not_accept_changed_completed_plist(
    fixture: dict[str, Any],
) -> None:
    recovery.upgrade(fixture["root"])
    settings = plistlib.loads(fixture["plist"].read_bytes())
    settings["StartInterval"] = 1
    fixture["plist"].write_bytes(plistlib.dumps(settings))
    with pytest.raises(ValueError, match="CHANGED_DURING_UPGRADE"):
        recovery.upgrade(fixture["root"])


def test_upgrade_refuses_changed_script_and_container_window(
    fixture: dict[str, Any],
) -> None:
    root = fixture["root"]
    marker = root / "container-maintenance.json"
    marker.write_text('{"phase":"DISPATCH_INTENT"}')
    with pytest.raises(ValueError, match="MAINTENANCE_PENDING"):
        recovery.upgrade(root)
    marker.unlink()
    Path(fixture["settings"]["ProgramArguments"][1]).write_text("changed")
    with pytest.raises(ValueError, match="SCRIPT_CHANGED"):
        recovery.upgrade(root)
    assert fixture["timer_calls"] == []
