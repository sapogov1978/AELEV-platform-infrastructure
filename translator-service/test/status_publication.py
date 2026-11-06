"""Owner-local real-container and browser lifecycle; no orchestration catalogue."""

from pathlib import Path

from aelev_platform_ui_client.testing import lifecycle

if __name__ == "__main__":
    lifecycle(
        Path(__file__).resolve().parents[1],
        "AELEV_INFRASTRUCTURE_TRANSLATOR_",
        {
            "database": "translator-database",
            "priority-consumer": "translator-priority-consumer",
            "normal-consumer": "translator-normal-consumer",
            "provider": "translation-provider",
            "worker": "translator-worker",
            "outbox": "translator-outbox",
            "api": "translator-api",
            "publisher": "translator-service-status-publisher",
        },
        compose_options=["-f", "compose.remote.yml", "--profile", "bundled-database"],
        presence_ttl=30,
    )
