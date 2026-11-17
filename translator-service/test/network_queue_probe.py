"""Operator probe for a newly created isolated Translator qualification store.

Executed inside the exact core image. No working database or Kafka is selected.
Raw exchange output is captured privately by network_queue_lifecycle.py.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import ssl
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from confluent_kafka import OFFSET_BEGINNING, Consumer, Producer, TopicPartition
from confluent_kafka.admin import (
    AclBinding,
    AclOperation,
    AclPermissionType,
    AdminClient,
    ResourcePatternType,
    ResourceType,
)
from sqlalchemy import text

from translator_service import runtime
from translator_service.contracts import RESULT_TOPIC_V2, digest, parse

REQUEST = Path("/run/queue-fixture/request.bin")


def main() -> None:
    action = sys.argv[1]
    raw = REQUEST.read_bytes()
    event = parse(raw, "TranslationRequestedV2")
    request_id = event["data"]["request_id"]
    config = runtime.kafka_configuration()
    if action == "provider-calls":
        from urllib.parse import urlsplit

        target = urlsplit(os.environ["AELEV_TRANSLATOR_PROVIDER_URL"])
        assert target.hostname is not None
        root = Path(os.environ["AELEV_TRANSLATOR_PROVIDER_TLS_ROOT"])
        context = ssl.create_default_context(cafile=str(root / "ca.crt"))
        context.load_cert_chain(str(root / "client.crt"), str(root / "client.key"))
        connection = http.client.HTTPSConnection(
            target.hostname, target.port, context=context, timeout=10
        )
        try:
            connection.request("GET", "/fixture/calls")
            response = connection.getresponse()
            assert response.status == 200
            print(response.read().decode())
        finally:
            connection.close()
        return
    if action == "acls":
        from translator_service.topic_provisioner import required_topics

        admin = AdminClient(config)
        rules = []

        def add(
            principal: str, kind: ResourceType, name: str, operation: AclOperation
        ) -> None:
            rules.append(
                AclBinding(
                    kind,
                    name,
                    ResourcePatternType.LITERAL,
                    "User:CN=" + principal,
                    "*",
                    operation,
                    AclPermissionType.ALLOW,
                )
            )

        for role in ("normal", "priority"):
            principal = "queue-" + role
            for version in ("v1", "v2"):
                topic = (
                    "translation.requests"
                    + (".priority" if role == "priority" else "")
                    + "."
                    + version
                )
                for operation in (AclOperation.READ, AclOperation.DESCRIBE):
                    add(principal, ResourceType.TOPIC, topic, operation)
            add(
                principal,
                ResourceType.GROUP,
                os.environ["QUEUE_GROUP_PREFIX"] + "-" + role,
                AclOperation.READ,
            )
        for topic in required_topics():
            for operation in (
                AclOperation.READ,
                AclOperation.WRITE,
                AclOperation.DESCRIBE,
            ):
                add("queue-probe", ResourceType.TOPIC, topic, operation)
        add(
            "queue-probe",
            ResourceType.GROUP,
            os.environ["QUEUE_GROUP_PREFIX"] + "-probe",
            AclOperation.READ,
        )
        for operation in (AclOperation.WRITE, AclOperation.DESCRIBE):
            add("queue-outbox", ResourceType.TOPIC, RESULT_TOPIC_V2, operation)
        for principal in ("queue-probe", "queue-outbox"):
            # confluent-kafka 2.11.1 calls ACL resource type 4 BROKER.
            add(
                principal,
                ResourceType.BROKER,
                "kafka-cluster",
                AclOperation.IDEMPOTENT_WRITE,
            )
        for future in admin.create_acls(rules).values():
            future.result(timeout=30)
        print(
            json.dumps(
                {
                    "status": "ACL_CREATED",
                    "count": len(rules),
                    "runtime_is_superuser": False,
                }
            )
        )
        return
    if action == "send":
        producer = Producer({**config, "acks": "all", "enable.idempotence": True})
        delivered = []
        producer.produce(
            os.environ["QUEUE_REQUEST_TOPIC"],
            key=request_id.encode(),
            value=raw,
            on_delivery=lambda error, message: delivered.append(error),
        )
        if producer.flush(30) or delivered != [None]:
            raise RuntimeError("QUEUE_PROBE_REQUEST_DELIVERY_FAILED")
        print(json.dumps({"request_id": request_id, "sha256": digest(raw)}))
        return
    if action.startswith("receive-"):
        expected = int(action.split("-")[-1])
        consumer = Consumer(
            {
                **config,
                "group.id": os.environ["QUEUE_GROUP_PREFIX"] + "-probe",
                "enable.auto.commit": False,
                "isolation.level": "read_committed",
            }
        )
        partition = TopicPartition(RESULT_TOPIC_V2, 0, OFFSET_BEGINNING)
        consumer.assign([partition])
        records: list[bytes] = []
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and len(records) < expected:
                message = consumer.poll(1)
                if message is None:
                    continue
                if message.error():
                    raise RuntimeError("QUEUE_PROBE_RESULT_READ_FAILED")
                body = message.value()
                if body is None:
                    raise RuntimeError("QUEUE_PROBE_EMPTY_RESULT")
                result = parse(body, "TranslationCompletedV2")["data"]
                if result["request_id"] != request_id:
                    raise RuntimeError("QUEUE_PROBE_FOREIGN_RESULT")
                assert result["request_digest"] == digest(raw)
                assert result["source_digest"] == event["data"]["source_digest"]
                assert result["source_language"] == event["data"]["source_language"]
                assert result["target_language"] == event["data"]["target_language"]
                assert result["status"] == "SUCCEEDED" and not result["error_code"]
                assert result["translated_text"].strip()
                assert type(result["execution_duration_ms"]) is int
                records.append(body)
            assert len(records) == expected and len(set(records)) == 1
            low, high = consumer.get_watermark_offsets(
                TopicPartition(RESULT_TOPIC_V2, 0), timeout=15
            )
            assert low == 0 and high == expected
            print(
                json.dumps(
                    {
                        "records": [
                            base64.b64encode(body).decode() for body in records
                        ],
                        "sha256": digest(records[0]),
                        "wire_records": high,
                    }
                )
            )
        finally:
            consumer.close()
        return
    if action == "crash-outbox":
        repository = runtime.repository()
        producer = Producer({**config, "acks": "all", "enable.idempotence": True})

        class CrashAfterAcknowledgement:
            def produce(self, *arguments: Any, **kwargs: Any) -> None:
                callback = kwargs["on_delivery"]

                def acknowledged(error: Any, message: Any) -> None:
                    callback(error, message)
                    if error is None:
                        os._exit(73)

                producer.produce(*arguments, **{**kwargs, "on_delivery": acknowledged})

            def flush(self, timeout: float) -> int:
                return int(producer.flush(timeout))

        runtime.publish_once(repository, CrashAfterAcknowledgement())
        raise RuntimeError("QUEUE_PROBE_CRASH_POINT_NOT_REACHED")
    repository = runtime.repository()
    try:
        with repository.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT state,attempt_count,error_code,request_digest,"
                        "lease_token "
                        "FROM translation_job WHERE request_id=:id"
                    ),
                    {"id": request_id},
                )
                .mappings()
                .first()
            )
            pending = (
                connection.execute(
                    text(
                        "SELECT state,payload FROM translation_outbox "
                        "WHERE request_id=:id"
                    ),
                    {"id": request_id},
                )
                .mappings()
                .first()
            )
            segments = connection.execute(
                text("SELECT count(*) FROM translation_segment WHERE request_id=:id"),
                {"id": request_id},
            ).scalar_one()
            sql_tls = dict(
                connection.execute(
                    text(
                        "SELECT ssl,client_dn FROM pg_stat_ssl "
                        "WHERE pid=pg_backend_pid()"
                    )
                )
                .mappings()
                .one()
            )
        # PostgreSQL 17 X509_NAME_to_cstring uses slash-separated DN fields.
        assert sql_tls["ssl"] and sql_tls["client_dn"] == "/CN=queue-owner", sql_tls
        if action == "bound" and row and row["attempt_count"] >= 1:
            control = repository.status()["control"]
            if control["desired_state"] == "RUNNING":
                repository.control(
                    "PAUSE",
                    command_id=str(
                        uuid.uuid5(uuid.UUID(request_id), "bound-one-inference")
                    ),
                    actor="isolated-queue-qualification",
                    expected_revision=control["revision"],
                    rationale="Bound this new test queue to one compute lease",
                )
        state = dict(row) if row else {}
        if state.get("lease_token") is not None:
            state["lease_token"] = str(state["lease_token"])
        state.update(
            outbox_state=pending["state"] if pending else None,
            segment_count=segments,
            control=repository.status()["control"],
            postgres_tls=sql_tls,
        )
        if pending:
            state["result_sha256"] = hashlib.sha256(
                bytes(pending["payload"])
            ).hexdigest()
        print(json.dumps(state))
    finally:
        repository.engine.dispose()


if __name__ == "__main__":
    main()
