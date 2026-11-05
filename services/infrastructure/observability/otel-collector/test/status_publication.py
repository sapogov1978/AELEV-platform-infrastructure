"""Real owner lifecycle and proof that peer publications continue."""

from pathlib import Path

from aelev_platform_ui_client.testing import lifecycle

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    lifecycle(
        root,
        "AELEV_INFRASTRUCTURE_OTEL_COLLECTOR_",
        {"server": "otel-collector", "publisher": "otel-collector-status-publisher"},
        compose_root=root,
        compose_options=[],
        verify_peers=True,
    )
