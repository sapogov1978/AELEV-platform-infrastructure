"""Owner-local real-container and browser lifecycle; no orchestration catalogue."""

from pathlib import Path

from aelev_platform_ui_client.testing import lifecycle

if __name__ == "__main__":
    lifecycle(
        Path(__file__).resolve().parents[1],
        "AELEV_INFRASTRUCTURE_POSTGRESQL_",
        {
            "postgresql-1": "postgresql-1",
            "postgresql-2": "postgresql-2",
            "postgresql-3": "postgresql-3",
            "etcd-1": "etcd-1",
            "etcd-2": "etcd-2",
            "etcd-3": "etcd-3",
            "publisher": "postgresql-status-publisher",
        },
        compose_options=["-f", "compose.yml"],
    )
