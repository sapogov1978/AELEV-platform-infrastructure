"""The owner uses the released standalone publisher heartbeat probe."""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_publisher_uses_pinned_standalone_probe() -> None:
    wheel = ROOT / "vendor/aelev_platform_ui_client-1.1.2-py3-none-any.whl"
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == (
        "d7fc03eee7df98d906a5390505a15e67c008fbeccd856f73ac0d5c67f62efb86"
    )
    assert wheel.name in (ROOT / "pyproject.toml").read_text()
    dockerfile = (ROOT / "Dockerfile.status").read_text()
    assert "aelev_platform_ui_client/health.py " in dockerfile
    compose = (ROOT / "compose.yml").read_text()
    assert (
        'test: ["CMD", "python", "-S", "/opt/aelev/publisher-health.py", '
        '"AELEV_INFRASTRUCTURE_LOKI_"]'
    ) in compose
    assert '"status_publisher.py", "health"' not in compose
    assert '"status_publisher.py" , "health"' not in compose


def test_publisher_pins_sqlite_and_finite_recovery() -> None:
    dockerfile = (ROOT / "Dockerfile.status").read_text()
    assert (
        "ghcr.io/sapogov1978/aelev-platform-infrastructure-observability-sender@sha256:162f07e6fd043eb3d9e04e7c415bee07771321a6f7f3864fcba0ce1f80dd941e"
        in dockerfile
    )
    assert "sqlite3.sqlite_version_info == (3, 53, 4)" in dockerfile
    compose = (ROOT / "compose.yml").read_text()
    assert "AELEV_INFRASTRUCTURE_LOKI_UI_RESTART_POLICY:?" in compose
    assert (
        "AELEV_INFRASTRUCTURE_LOKI_UI_RESTART_POLICY=no"
        in (ROOT / ".env.example").read_text()
    )
