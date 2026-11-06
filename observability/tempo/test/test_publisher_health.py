"""The owner uses the released standalone publisher heartbeat probe."""

import hashlib
from pathlib import Path

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
        'test: ["CMD", "python", "-S", "/opt/aelev/publisher-health.py", '
        '"AELEV_INFRASTRUCTURE_TEMPO_"]'
    ) in compose
    assert '"status_publisher.py", "health"' not in compose
    assert '"status_publisher.py" , "health"' not in compose


def test_publisher_pins_sqlite_and_finite_recovery() -> None:
    dockerfile = (ROOT / "Dockerfile.status").read_text()
    assert (
        "aelev/observability-sender:1.0.7@sha256:44e5063c37fd5268d221bf7886a66abb4a256a595190604470bbf1c5e5500739"
        in dockerfile
    )
    assert "sqlite3.sqlite_version_info == (3, 53, 4)" in dockerfile
    compose = (ROOT / "compose.yml").read_text()
    assert "AELEV_INFRASTRUCTURE_TEMPO_UI_RESTART_POLICY:?" in compose
    assert (
        "AELEV_INFRASTRUCTURE_TEMPO_UI_RESTART_POLICY=no"
        in (ROOT / ".env.example").read_text()
    )
