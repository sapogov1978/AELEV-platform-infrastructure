"""Idempotently provision translator-owned Kafka topics."""

# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

import logging
import os
from typing import Any

from confluent_kafka.admin import AdminClient, ConfigResource, NewTopic

from translator_service.contracts import (
    NORMAL_REQUEST_TOPIC,
    NORMAL_REQUEST_TOPIC_V2,
    PRIORITY_REQUEST_TOPIC,
    PRIORITY_REQUEST_TOPIC_V2,
    RESULT_TOPIC,
    RESULT_TOPIC_V2,
)
from translator_service.runtime import JsonFormatter, kafka_configuration

LOGGER = logging.getLogger(__name__)


def required_topics() -> dict[str, dict[str, str]]:
    common = {
        "cleanup.policy": "delete",
        "retention.ms": os.environ["AELEV_TRANSLATOR_TOPIC_RETENTION_MS"],
        "retention.bytes": os.environ["AELEV_TRANSLATOR_TOPIC_RETENTION_BYTES"],
        "max.message.bytes": os.environ["AELEV_TRANSLATOR_TOPIC_MAX_MESSAGE_BYTES"],
        "min.insync.replicas": os.environ["AELEV_TRANSLATOR_TOPIC_MIN_INSYNC_REPLICAS"],
    }
    return {
        PRIORITY_REQUEST_TOPIC: dict(common),
        NORMAL_REQUEST_TOPIC: dict(common),
        RESULT_TOPIC: dict(common),
        PRIORITY_REQUEST_TOPIC_V2: dict(common),
        NORMAL_REQUEST_TOPIC_V2: dict(common),
        RESULT_TOPIC_V2: dict(common),
    }


def main() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    admin = AdminClient(kafka_configuration())
    metadata = admin.list_topics(timeout=15)
    topics = required_topics()
    missing = [
        NewTopic(
            name,
            num_partitions=int(os.environ["AELEV_TRANSLATOR_TOPIC_PARTITIONS"]),
            replication_factor=int(
                os.environ["AELEV_TRANSLATOR_TOPIC_REPLICATION_FACTOR"]
            ),
            config=config,
        )
        for name, config in topics.items()
        if name not in metadata.topics
    ]
    if missing:
        for name, future in admin.create_topics(missing).items():
            future.result(timeout=30)
            LOGGER.info("TRANSLATOR_TOPIC_CREATED:%s", name)
    metadata = admin.list_topics(timeout=15)
    expected_partitions = int(os.environ["AELEV_TRANSLATOR_TOPIC_PARTITIONS"])
    expected_replicas = int(os.environ["AELEV_TRANSLATOR_TOPIC_REPLICATION_FACTOR"])
    for name in topics:
        partitions = metadata.topics[name].partitions
        if len(partitions) != expected_partitions or any(
            len(partition.replicas) != expected_replicas
            for partition in partitions.values()
        ):
            raise RuntimeError(f"TRANSLATOR_TOPIC_TOPOLOGY_MISMATCH:{name}")
    resources = [ConfigResource(ConfigResource.Type.TOPIC, name) for name in topics]
    actual: dict[str, Any] = {
        resource.name: future.result(timeout=15)
        for resource, future in admin.describe_configs(resources).items()
    }
    for name, expected in topics.items():
        values = {key: entry.value for key, entry in actual[name].items()}
        mismatches = {
            key: (values.get(key), value)
            for key, value in expected.items()
            if values.get(key) != value
        }
        if mismatches:
            raise RuntimeError(f"TRANSLATOR_TOPIC_CONFIG_MISMATCH:{name}:{mismatches}")


if __name__ == "__main__":
    main()
