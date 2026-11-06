"""Kafka cleanup must prove ownership of every record before administrative IO."""

import json
import runpy
import sys
import uuid
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from translator_service import runtime
from translator_service.contracts import (
    NORMAL_REQUEST_TOPIC_V2,
    PRIORITY_REQUEST_TOPIC_V2,
    RESULT_TOPIC_V2,
    digest,
    parse,
    result_bytes,
)
from translator_service.operator_translation import TranslationInput, request_bytes

ROOT = Path(__file__).resolve().parents[1]
PROBE = runpy.run_path(str(ROOT / "test/index_model.py"))["PROBE"]


@pytest.mark.parametrize("variant", ["own", "foreign", "altered", "result"])
def test_every_record_is_bound_before_trimming(
    monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    identity = str(uuid.uuid4())
    payload = TranslationInput(
        queue="normal", source_language="en", target_language="de", text="fixture"
    )
    raw = request_bytes(payload, "sha256:" + "b" * 64, identity)
    bound = digest(raw)
    topic = NORMAL_REQUEST_TOPIC_V2
    if variant == "foreign":
        raw = request_bytes(payload, "sha256:" + "b" * 64, str(uuid.uuid4()))
    elif variant == "altered":
        changed = payload.model_copy(update={"text": "other fixture"})
        raw = request_bytes(changed, "sha256:" + "b" * 64, identity)
    elif variant == "result":
        topic = RESULT_TOPIC_V2
        raw = result_bytes(
            parse(raw, "TranslationRequestedV2"),
            bound,
            {
                "status": "SUCCEEDED",
                "translated_text": "Ergebnis",
                "warnings": [],
                "error_code": "",
            },
        )
    topics = (NORMAL_REQUEST_TOPIC_V2, PRIORITY_REQUEST_TOPIC_V2, RESULT_TOPIC_V2)
    spans = {
        name: [{"partition": 0, "low": 0, "high": 1 if name == topic else 0}]
        for name in topics
    }
    admin = Mock()
    admin.delete_records.return_value = {"partition": Mock()}
    consumers: list[Any] = []

    class Consumer:
        def __init__(self, _: object) -> None:
            self.closed = False
            self.sent = False
            consumers.append(self)

        def list_topics(self, **kwargs: Any) -> Any:
            return Mock(topics={name: Mock(partitions={0: None}) for name in topics})

        def get_watermark_offsets(
            self, partition: Any, **kwargs: Any
        ) -> tuple[int, int]:
            return (0, 1 if partition.topic == topic else 0)

        def assign(self, _: object) -> None:
            self.sent = False

        def poll(self, _: object) -> Any:
            if self.sent:
                return None
            self.sent = True
            return Mock(
                error=Mock(return_value=None),
                value=Mock(return_value=raw),
                offset=Mock(return_value=0),
            )

        def close(self) -> None:
            self.closed = True

    kafka = import_module("confluent_kafka")
    administrative = import_module("confluent_kafka.admin")
    monkeypatch.setattr(kafka, "Consumer", Consumer)
    monkeypatch.setattr(administrative, "AdminClient", Mock(return_value=admin))
    monkeypatch.setattr(runtime, "kafka_configuration", Mock(return_value={}))
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe", "trim", json.dumps({"offsets": spans, "digests": {identity: bound}})],
    )
    if variant in {"own", "result"}:
        with pytest.raises(SystemExit) as exit_info:
            exec(PROBE, {"__name__": "index_probe"})
        assert exit_info.value.code is None
        admin.delete_records.assert_called_once()
    else:
        with pytest.raises(
            AssertionError,
            match=(
                "FOREIGN_KAFKA_RECORD"
                if variant == "foreign"
                else "KAFKA_RECORD_DIGEST_MISMATCH"
            ),
        ):
            exec(PROBE, {"__name__": "index_probe"})
        admin.delete_records.assert_not_called()
    assert consumers and all(client.closed for client in consumers)
