"""Owner-local real-container and browser lifecycle; no orchestration catalogue."""

from pathlib import Path

from aelev_platform_ui_client.testing import lifecycle


if __name__ == "__main__":
    lifecycle(
        Path(__file__).resolve().parents[1],
        "AELEV_INFRASTRUCTURE_KAFKA_",
        {"broker": "kafka", "publisher": "kafka-status-publisher"},
    )
