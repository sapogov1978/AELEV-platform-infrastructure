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
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "aelev_platform_ui_client/health.py " in dockerfile
    compose = (ROOT / "compose.yml").read_text()
    assert (
        'test: ["CMD", "python", "-S", "/opt/platform-ui/publisher-health.py", '
        '"AELEV_INFRASTRUCTURE_PLATFORM_UI_"]'
    ) in compose
    assert '"status_publisher.py", "health"' not in compose
    assert '"status_publisher.py" , "health"' not in compose


def test_publisher_image_does_not_replace_the_api_image() -> None:
    compose = (ROOT / "compose.yml").read_text()
    publisher = compose.split("  platform-ui-status-publisher:", 1)[1].split(
        "\n  platform-ui-status-provision:", 1
    )[0]
    assert "image: ${AELEV_INFRASTRUCTURE_PLATFORM_UI_STATUS_IMAGE:?" in publisher
    assert compose.count("AELEV_INFRASTRUCTURE_PLATFORM_UI_STATUS_IMAGE") == 1
    assert ":-" not in compose
    assert "target: publisher" in publisher
    example = (ROOT / ".env.example").read_text()
    images = dict(
        line.split("=", 1)
        for line in example.splitlines()
        if line.startswith("AELEV_INFRASTRUCTURE_PLATFORM_UI_") and "=" in line
    )
    assert (
        images["AELEV_INFRASTRUCTURE_PLATFORM_UI_STATUS_IMAGE"]
        != images["AELEV_INFRASTRUCTURE_PLATFORM_UI_API_IMAGE"]
    )


def test_publisher_pins_sqlite_and_finite_recovery() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert (
        "ghcr.io/sapogov1978/aelev-platform-infrastructure-observability-sender@sha256:162f07e6fd043eb3d9e04e7c415bee07771321a6f7f3864fcba0ce1f80dd941e"
        in dockerfile
    )
    assert "sqlite3.sqlite_version_info == (3, 53, 4)" in dockerfile
    compose = (ROOT / "compose.yml").read_text()
    assert "AELEV_INFRASTRUCTURE_PLATFORM_UI_UI_RESTART_POLICY:?" in compose
    assert (
        "AELEV_INFRASTRUCTURE_PLATFORM_UI_UI_RESTART_POLICY=no"
        in (ROOT / ".env.example").read_text()
    )
