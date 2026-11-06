"""Owner-local real-container and browser lifecycle; no orchestration catalogue."""

from pathlib import Path

from aelev_platform_ui_client.testing import lifecycle


if __name__ == "__main__":
    lifecycle(
        Path(__file__).resolve().parents[1],
        "AELEV_INFRASTRUCTURE_MINIO_",
        {
            "site-a-1": "minio-site-a-1",
            "site-a-2": "minio-site-a-2",
            "site-a-3": "minio-site-a-3",
            "site-a-4": "minio-site-a-4",
            "site-b-1": "minio-site-b-1",
            "site-b-2": "minio-site-b-2",
            "site-b-3": "minio-site-b-3",
            "site-b-4": "minio-site-b-4",
            "publisher": "minio-status-publisher",
        },
    )
