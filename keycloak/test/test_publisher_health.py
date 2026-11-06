"""The owner uses the released standalone publisher heartbeat probe."""

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_publisher_uses_pinned_standalone_probe() -> None:
    wheel = ROOT / "vendor/aelev_platform_ui_client-1.1.1-py3-none-any.whl"
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == (
        "6137c8f99afcbf317f9313b4ea739490564c8fe3a68892a811d323b0e29fb61b"
    )
    assert wheel.name in (ROOT / "pyproject.toml").read_text()
    dockerfile = (ROOT / "Dockerfile.status").read_text()
    assert "aelev_platform_ui_client/health.py " in dockerfile
    compose = (ROOT / "compose.yml").read_text()
    assert (
        'test: ["CMD", "python", "-S", "/opt/keycloak-status/publisher-health.py", '
        '"AELEV_INFRASTRUCTURE_KEYCLOAK_"]'
    ) in compose
    assert '"status_publisher.py", "health"' not in compose
    assert '"status_publisher.py" , "health"' not in compose


def load_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "standalone_keycloak_health", ROOT / "publisher_health_probe.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_keycloak_keeps_original_journal_and_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = load_probe()
    calls: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(sys, "argv", ["probe", probe.PREFIX])
    monkeypatch.setenv(probe.PREFIX + "UI_INTERVAL_SECONDS", "30")
    monkeypatch.setenv(probe.PREFIX + "UI_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv(probe.PREFIX + "UI_MODULE_ID", "owner")
    monkeypatch.setattr(
        probe.runpy,
        "run_path",
        lambda path: {"check": lambda *args: calls.append((path, args))},
    )
    probe.main()
    assert len(calls) == 1
    assert Path(calls[0][0]).name == "publisher_health.py"
    assert calls[0][1] == (
        Path("/var/lib/keycloak-status/outbox.sqlite"),
        "owner",
        100,
    )
    assert "ssl" not in probe.__dict__
    assert "threading" not in probe.__dict__
    assert "status_publisher" not in probe.__dict__


def test_keycloak_local_failure_has_only_safe_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = load_probe()
    monkeypatch.setattr(sys, "argv", ["probe", probe.PREFIX])
    monkeypatch.setenv(probe.PREFIX + "UI_INTERVAL_SECONDS", "30")
    monkeypatch.setenv(probe.PREFIX + "UI_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv(probe.PREFIX + "UI_MODULE_ID", "owner")

    def fail(*_: object) -> None:
        raise OSError("sensitive details must not escape")

    monkeypatch.setattr(probe.runpy, "run_path", lambda _: {"check": fail})
    with pytest.raises(SystemExit) as error:
        probe.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "PUBLISHER_LOCAL_HEALTH_UNAVAILABLE\n"


@pytest.mark.parametrize("argument", [[], ["AELEV_OTHER_"]])
def test_keycloak_rejects_missing_or_foreign_prefix(
    monkeypatch: pytest.MonkeyPatch, argument: list[str]
) -> None:
    probe = load_probe()
    monkeypatch.setattr(sys, "argv", ["probe", *argument])
    with pytest.raises(SystemExit) as error:
        probe.main()
    assert error.value.code == 2


def test_publisher_pins_sqlite_and_finite_recovery() -> None:
    dockerfile = (ROOT / "Dockerfile.status").read_text()
    assert (
        "ghcr.io/sapogov1978/aelev-platform-infrastructure-observability-sender@sha256:162f07e6fd043eb3d9e04e7c415bee07771321a6f7f3864fcba0ce1f80dd941e"
        in dockerfile
    )
    assert "sqlite3.sqlite_version_info == (3, 53, 4)" in dockerfile
    compose = (ROOT / "compose.yml").read_text()
    assert "AELEV_INFRASTRUCTURE_KEYCLOAK_UI_RESTART_POLICY:?" in compose
    assert (
        "AELEV_INFRASTRUCTURE_KEYCLOAK_UI_RESTART_POLICY=no"
        in (ROOT / ".env.example").read_text()
    )
