"""User schedulers delegate to the same finite controller and journal."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery
import recovery_host as host
from test_recovery_host import fixture as base_host_fixture  # noqa: F401

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


@pytest.fixture
def host_fixture(request: pytest.FixtureRequest) -> dict[str, Any]:
    return request.getfixturevalue("base_host_fixture")  # type: ignore[no-any-return]


@pytest.fixture(params=["Linux", "Windows"])
def scheduler(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    root.joinpath("state.json").write_bytes(b'{"targets":{"worker":{"attempts":3}}}')
    monkeypatch.setattr(platform, "system", lambda: request.param)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    active = [False]
    registered: list[bytes] = []
    calls: list[Any] = []

    def execute(argv: list[str], **kwargs: Any) -> bytes:
        calls.append(argv)
        assert argv[:2] == ["systemctl", "--user"]
        if argv[2] in {"start", "stop"}:
            active[0] = argv[2] == "start"
        if argv[2] == "show":
            if argv[-1] == "--property=LoadState,FragmentPath":
                return b"LoadState=not-found\nFragmentPath=\n"
            if "FragmentPath" in argv[-1]:
                path = tmp_path / "home/.config/systemd/user" / argv[3]
                return f"FragmentPath={path}\nNeedDaemonReload=no\n".encode()
            return f"LoadState=loaded\nActiveState={'active' if active[0] else 'inactive'}\nUnitFileState=enabled\n".encode()
        return b""

    def powershell(script: str) -> bytes:
        calls.append(script)
        if "GetCurrent" in script:
            return b"S-1-5-21-123"
        if "Register-ScheduledTask" in script:
            raw = next(root.glob("*.xml")).read_bytes()
            registered[:] = [raw]
            active[0] = True
        if "Export-ScheduledTask" in script:
            return registered[0]
        if "'PRESENT'" in script:
            return b"PRESENT" if registered else b"ABSENT"
        if "Disable-ScheduledTask" in script:
            active[0] = False
        if "Enable-ScheduledTask" in script:
            active[0] = True
        if "[bool]$task.Settings.Enabled" in script:
            return b"True" if active[0] else b"False"
        return b""

    monkeypatch.setattr(recovery, "execute", execute)
    monkeypatch.setattr(recovery, "powershell", powershell)
    return dict(
        root=root,
        system=request.param,
        active=active,
        registered=registered,
        calls=calls,
    )


def test_install_check_pause_and_restore_preserve_journal(
    scheduler: dict[str, Any],
) -> None:
    root = scheduler["root"]
    before = (root / "state.json").read_bytes()
    recovery.install(root)
    domain, path, digest = recovery.scheduler(root)
    assert recovery.scheduler_loaded(domain)
    recovery.scheduler_switch(domain, enabled=False)
    assert not recovery.scheduler_loaded(domain)
    assert recovery.scheduler(root) == (domain, path, digest)
    recovery.scheduler_switch(domain, enabled=True)
    assert (root / "state.json").read_bytes() == before
    assert json.loads(path.read_bytes())["phase"] == "COMPLETE"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert not any(
        "sudo" in str(c) or "Stop-ScheduledTask" in str(c) for c in scheduler["calls"]
    )


def test_repeat_install_does_not_restore_manually_disabled_task(
    scheduler: dict[str, Any],
) -> None:
    recovery.install(scheduler["root"])
    scheduler["active"][0] = False
    before = list(scheduler["calls"])
    with pytest.raises(ValueError, match="MANUALLY_STOPPED"):
        recovery.install(scheduler["root"])
    assert not scheduler["active"][0]
    assert not any(
        "start" in c or "Enable-ScheduledTask" in str(c)
        for c in scheduler["calls"][len(before) :]
    )


def test_changed_script_or_task_cannot_be_paused(scheduler: dict[str, Any]) -> None:
    root = scheduler["root"]
    recovery.install(root)
    script = next(root.glob("recovery-*.py"))
    script.write_bytes(b"changed")
    with pytest.raises(ValueError, match="SCRIPT_CHANGED"):
        recovery.scheduler(root)


def test_changed_manager_and_unknown_state_fail_closed(
    scheduler: dict[str, Any],
) -> None:
    root = scheduler["root"]
    recovery.install(root)
    if scheduler["system"] == "Windows":
        scheduler["registered"][0] = scheduler["registered"][0].replace(
            b"PT1M", b"PT2M"
        )
        with pytest.raises(ValueError, match="MANAGER_CHANGED"):
            recovery.scheduler(root)
    else:
        directory = Path.home() / ".config/systemd/user"
        next(directory.glob("*.service")).write_text("changed")
        with pytest.raises(ValueError, match="TIMER_CHANGED"):
            recovery.scheduler(root)


def test_unavailable_scheduler_is_not_absence(
    scheduler: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery.install(scheduler["root"])
    domain, _, _ = recovery.scheduler(scheduler["root"])
    fail = Mock(side_effect=subprocess.TimeoutExpired("native scheduler", 20))
    monkeypatch.setattr(recovery, "execute", fail)
    monkeypatch.setattr(recovery, "powershell", fail)
    with pytest.raises(subprocess.TimeoutExpired):
        recovery.scheduler_loaded(domain)


def test_lost_registration_or_activation_ack_reconciles_same_task(
    scheduler: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "powershell" if scheduler["system"] == "Windows" else "execute"
    original = getattr(recovery, name)
    failed = [False]

    def interrupted(command: Any, **kwargs: Any) -> bytes:
        value = original(command, **kwargs)
        match = (
            "Register-ScheduledTask" in str(command)
            if name == "powershell"
            else command[2] == "start"
        )
        if match and not failed[0]:
            failed[0] = True
            raise subprocess.TimeoutExpired("lost ACK", 20)
        return cast(bytes, value)

    monkeypatch.setattr(recovery, name, interrupted)
    with pytest.raises(subprocess.TimeoutExpired):
        recovery.install(scheduler["root"])
    recovery.install(scheduler["root"])
    assert (
        json.loads((scheduler["root"] / "scheduler.json").read_bytes())["phase"]
        == "COMPLETE"
    )
    calls = scheduler["calls"]
    if name == "powershell":
        assert sum("Register-ScheduledTask" in str(c) for c in calls) == 1
    else:
        assert sum(isinstance(c, list) and c[2] == "start" for c in calls) == 1


def test_linux_units_escape_argv_without_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    settings = recovery.scheduler_settings(
        tmp_path / 'a % $ " b', tmp_path / "script.py"
    )
    service = next(v.decode() for k, v in settings.items() if k.endswith(".service"))
    assert "Type=oneshot" in service and "NoNewPrivileges=yes" in service
    assert "%%" in service and "$$" in service and '\\"' in service
    assert "sh -c" not in service and "TimeoutStartSec=infinity" in service
    with pytest.raises(ValueError, match="PATH_INVALID"):
        recovery.unit_quote("bad\npath")


def test_windows_task_is_per_user_no_privilege_or_failure_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(recovery, "powershell", lambda _: b"S-1-5-21-123")
    raw = next(
        iter(recovery.scheduler_settings(tmp_path, tmp_path / "a b.py").values())
    )
    tree = ET.fromstring(raw)
    assert (
        tree.findtext("t:Principals/t:Principal/t:LogonType", namespaces=NS)
        == "InteractiveToken"
    )
    assert (
        tree.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=NS)
        == "LeastPrivilege"
    )
    assert (
        tree.findtext("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", namespaces=NS)
        == "PT1M"
    )
    assert tree.find("t:Settings/t:RestartOnFailure", NS) is None
    assert len(tree.findall("t:Actions/*", NS)) == 1
    assert " watch --state " in (
        tree.findtext("t:Actions/t:Exec/t:Arguments", namespaces=NS) or ""
    )
    assert tree.findtext("t:Settings/t:AllowHardTerminate", namespaces=NS) == "false"
    changed = raw.replace(b"LeastPrivilege", b"HighestAvailable")
    with pytest.raises(ValueError, match="REGISTRATION_UNCONFIRMED"):
        recovery.verify_task(raw, changed)


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_native_window_preserves_budget_and_retries_only_idempotent_apply(
    host_fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch, system: str
) -> None:
    root = host_fixture["root"]
    path = root / "scheduler.json"
    path.write_bytes(b"fixed")
    monkeypatch.setattr(
        host,
        "timer",
        lambda _: (
            system + ":exact",
            path,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        ),
    )
    calls: list[bool] = []

    def switch(_: str, path: Path, *, enabled: bool) -> None:
        calls.append(enabled)
        host_fixture["loaded"][0] = enabled

    monkeypatch.setattr(host, "switch", switch)
    monkeypatch.setattr(host, "preflight", lambda *a, **kw: None)
    monkeypatch.setattr(
        host,
        "result",
        lambda *a: {"daemon_id": "daemon", "targets": {"worker": {"attempts": 3}}},
    )
    apply = Mock(side_effect=[subprocess.TimeoutExpired("policy ACK", 20), None])
    monkeypatch.setattr(host, "apply", apply)
    with pytest.raises(subprocess.TimeoutExpired):
        host.maintain(root, "sha256:" + "f" * 64, host_fixture["request"])
    assert not host_fixture["loaded"][0]
    result = host.maintain(root, "sha256:" + "f" * 64, host_fixture["request"])
    assert result["targets"]["worker"]["attempts"] == 3
    assert calls == [False, True]
    host.maintain(root, "sha256:" + "f" * 64, host_fixture["request"])
    assert apply.call_count == 2  # No third apply after the completed receipt.


def test_posix_barrier_is_really_exclusive(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    with path.open("a") as first, path.open("a") as second:
        recovery.lock(first)
        with pytest.raises(BlockingIOError):
            recovery.lock(second)


def test_operator_can_wait_for_the_active_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".lock"
    with path.open("a") as first, path.open("a") as second:
        recovery.lock(first)
        monkeypatch.setattr(recovery.time, "sleep", lambda _seconds: first.close())
        recovery.lock(second, timeout=1)


def test_windows_barrier_uses_native_nonblocking_byte_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = SimpleNamespace(locking=Mock(), LK_NBLCK=2)
    monkeypatch.setattr(recovery, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        recovery, "importlib", SimpleNamespace(import_module=lambda _: native)
    )
    with (tmp_path / ".lock").open("a") as stream:
        recovery.lock(stream)
        native.locking.assert_called_once_with(stream.fileno(), 2, 1)
        native.locking.side_effect = PermissionError(13, "busy")
        with pytest.raises(BlockingIOError):
            recovery.lock(stream)


def test_windows_acl_is_verified_not_assumed_from_posix_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recovery, "os", SimpleNamespace(name="nt"))
    run = Mock(return_value=b"PRIVATE")
    monkeypatch.setattr(recovery, "powershell", run)
    recovery.private(tmp_path, create=True)
    script = run.call_args.args[0]
    assert "SetAccessRuleProtection($true, $false)" in script
    assert "GetOwner" in script and "$rules.Count -ne 1" in script
    run.return_value = b"unconfirmed"
    with pytest.raises(ValueError, match="PRIVATE_OWNED"):
        recovery.private(tmp_path)


def test_timer_status_is_read_only(scheduler: dict[str, Any]) -> None:
    root = scheduler["root"]
    recovery.install(root)
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert recovery.scheduler_status(root)["active"] is True
    assert before == {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}


@pytest.mark.parametrize("name", ["scheduler.json", "container-maintenance.json"])
def test_real_once_cli_does_not_touch_pending_operator_journal(
    tmp_path: Path, name: str
) -> None:
    tmp_path.chmod(0o700)
    state = tmp_path / "state.json"
    state.write_bytes(b'{"targets":{},"sentinel":"unchanged"}')
    (tmp_path / name).write_text('{"phase":"REGISTER_INTENT"}')
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(Path(recovery.__file__)),
            "once",
            "--state",
            str(tmp_path),
        ],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert state.read_bytes() == b'{"targets":{},"sentinel":"unchanged"}'
    assert not (tmp_path / "controller-error.json").exists()
