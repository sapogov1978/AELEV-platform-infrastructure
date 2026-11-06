"""Observe only the configured Kafka broker; never create or list topics."""

from __future__ import annotations

import json
import socket
import struct

from aelev_platform_ui_client import Settings, aggregate, observe, run

PREFIX = "AELEV_INFRASTRUCTURE_KAFKA_"


def receive(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise ValueError("truncated Kafka response")
        result.extend(chunk)
    return bytes(result)


def api_versions(cfg: Settings) -> str:
    """Validate ApiVersions v0, not just TCP acceptance, on our own broker."""
    client = b"aelev-kafka-health"
    request = struct.pack(">hhih", 18, 0, 1, len(client)) + client
    with socket.create_connection(("kafka", 9092), timeout=cfg.timeout) as connection:
        connection.sendall(struct.pack(">i", len(request)) + request)
        size = struct.unpack(">i", receive(connection, 4))[0]
        if not 10 <= size <= 65536:
            raise ValueError("invalid Kafka response length")
        response = receive(connection, size)
    correlation, error, count = struct.unpack(">ihi", response[:10])
    if correlation != 1 or count < 0 or len(response) != 10 + count * 6:
        raise ValueError("invalid ApiVersions response")
    versions = list(struct.iter_unpack(">hhh", response[10:]))
    if len({key for key, _, _ in versions}) != count:
        raise ValueError("duplicate Kafka API")
    if error:
        return "OFFLINE"
    if not any(key == 3 and minimum <= maximum for key, minimum, maximum in versions):
        raise ValueError("broker metadata API unavailable")
    return "ONLINE"


def description(cfg: Settings) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "module_id": cfg.module_id,
            "revision": 2,
            "title": {
                "ru": "Межсервисное взаимодействие",
                "en": "Inter-service communication",
            },
            "service_kind": "INFRASTRUCTURE",
            "sections": [],
            "components": [
                {
                    "id": "broker",
                    "title": {
                        "ru": "Kafka broker / controller",
                        "en": "Kafka broker / controller",
                    },
                },
                {
                    "id": "publisher",
                    "title": {
                        "ru": "Издатель состояния Kafka",
                        "en": "Kafka status publisher",
                    },
                },
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def probe(cfg: Settings) -> tuple[str, list[dict[str, str]]]:
    components = [observe("broker", lambda: api_versions(cfg))]
    return aggregate(components), components


if __name__ == "__main__":
    settings = Settings.environment(PREFIX)
    run(settings, "kafka-status", description(settings), lambda: probe(settings))
