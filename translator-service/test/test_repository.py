"""Repository tests use only an explicitly isolated PostgreSQL database."""

import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from jsonschema import ValidationError
from sqlalchemy import text

from translator_service.contracts import digest, parse, result_bytes
from translator_service.repository import Repository
from translator_service.restore import verify


def request(*, value: str = "climate observatory", identity: str | None = None) -> bytes:
    identity = identity or str(uuid.uuid4())
    timestamp = datetime.now(UTC).isoformat()
    event: dict[str, object] = {
        "specversion": "1.0",
        "id": identity,
        "source": "urn:aelev:test-client",
        "type": "com.aelev.translation-requested.v1",
        "time": timestamp,
        "subject": "translation/" + identity,
        "datacontenttype": "application/json",
        "dataschema": "https://schemas.aelev.dev/events/v1#/$defs/TranslationRequested",
        "correlationid": identity,
        "causationid": identity,
        "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        "data": {
            "schema_version": "1.0.0",
            "recorded_at": timestamp,
            "provenance": {
                "owner_context": "TEST_CLIENT",
                "producer_id": "test-client",
                "producer_version": "0.1.0",
                "input_refs": [],
            },
            "request_id": identity,
            "operation": "TRANSLATE_TEXT",
            "text": value,
            "source_digest": digest(value.encode()),
            "source_language": "en",
            "target_language": "de",
            "translator_ref": "google/madlad400-3b-mt-q4k@sha256:" + "a" * 64,
        },
    }
    return json.dumps(event, ensure_ascii=False, sort_keys=True).encode()


@pytest.fixture
def repo() -> Iterator[Repository]:
    url = os.environ.get("AELEV_TRANSLATOR_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated PostgreSQL URL not configured")
    # Require an explicitly named disposable database, not a working service DB.
    if "/translator_test" not in url:
        raise ValueError("test database must be named translator_test")
    repo = Repository(url)
    with repo.engine.begin() as connection:
        connection.execute(text("TRUNCATE translation_job CASCADE"))
        connection.execute(text("TRUNCATE translation_control_command"))
        connection.execute(
            text(
                "UPDATE translation_control SET revision=1, desired_state='RUNNING',"
                "priority_streak=0"
            )
        )
    yield repo
    repo.engine.dispose()


def success() -> dict[str, Any]:
    return {
        "status": "SUCCEEDED",
        "translated_text": "Klimaobservatorium",
        "warnings": [],
        "error_code": "",
    }


def opaque_request() -> bytes:
    """A customer asks for translation without selecting any implementation."""
    event = json.loads(request())
    event["type"] = "com.aelev.translation-requested.v2"
    event["dataschema"] += "V2"
    event["data"]["schema_version"] = "2.0.0"
    del event["data"]["translator_ref"]
    del event["data"]["operation"]
    return json.dumps(event).encode()


def test_v2_never_accepts_customer_execution_configuration() -> None:
    raw = opaque_request()
    assert parse(raw, "TranslationRequestedV2")["data"]["text"] == "climate observatory"
    for key in ("translator_ref", "model", "execution_ref", "operation"):
        event = json.loads(raw)
        event["data"][key] = "customer-choice"
        with pytest.raises(ValidationError, match="Additional properties"):
            parse(json.dumps(event).encode(), "TranslationRequestedV2")


def test_execution_duration_is_optional_and_bounded_without_changing_v1() -> None:
    raw = opaque_request()
    original = result_bytes(json.loads(raw), digest(raw), success())
    assert (
        "execution_duration_ms" not in parse(original, "TranslationCompletedV2")["data"]
    )
    for milliseconds in (0, 1250, 9007199254740991):
        measured = result_bytes(
            json.loads(raw),
            digest(raw),
            {**success(), "execution_duration_ms": milliseconds},
        )
        assert (
            parse(measured, "TranslationCompletedV2")["data"]["execution_duration_ms"]
            == milliseconds
        )
    for invalid in (-1, 1.5, True, None, 9007199254740992):
        with pytest.raises(ValidationError):
            result_bytes(
                json.loads(raw),
                digest(raw),
                {**success(), "execution_duration_ms": invalid},
            )
    legacy = request()
    assert (
        "execution_duration_ms"
        not in parse(
            result_bytes(json.loads(legacy), digest(legacy), success()),
            "TranslationCompleted",
        )["data"]
    )


def test_v2_uses_current_execution_without_exposing_it(repo: Repository) -> None:
    raw = opaque_request()
    repo.execution_ref = "owner-execution-a"
    repo.accept(raw, priority=0)
    repo.execution_ref = "owner-execution-b"
    repo.accept(raw, priority=0)
    lease = repo.lease()
    assert lease is not None
    assert lease["execution_ref"] == "owner-execution-b"
    assert bytes(lease["payload"]) == raw
    assert repo.complete(lease, {**success(), "execution_duration_ms": 1250})
    saved = bytes(repo.pending()[0]["payload"])
    result = parse(saved, "TranslationCompletedV2")
    assert "translator_ref" not in result["data"]
    assert "execution_ref" not in result["data"]
    assert result["data"]["execution_duration_ms"] == 1250
    repo.published(lease["request_id"])
    repo.execution_ref = "owner-execution-b"
    repo.accept(raw)
    assert bytes(repo.pending()[0]["payload"]) == saved
    assert verify(repo) == {"status": "VERIFIED", "jobs": 1}


def test_new_v2_attempt_can_use_updated_execution_and_change_translation(
    repo: Repository,
) -> None:
    raw = opaque_request()
    repo.execution_ref = "owner-execution-a"
    repo.accept(raw)
    lease = repo.lease()
    assert lease is not None
    assert repo.complete(lease, success())
    fresh = json.loads(raw)
    identity = str(uuid.uuid4())
    fresh["id"] = identity
    fresh["data"]["request_id"] = identity
    repo.execution_ref = "owner-execution-b"
    repo.accept(json.dumps(fresh).encode())
    newer = repo.lease()
    assert newer is not None
    assert newer["execution_ref"] == "owner-execution-b"
    response = {**success(), "translated_text": "Klimaobservatorium, aktualisiert"}
    assert repo.complete(newer, response)
    assert verify(repo) == {"status": "VERIFIED", "jobs": 2}


def test_unfinished_v2_request_can_continue_with_updated_execution(
    repo: Repository,
) -> None:
    repo.execution_ref = "owner-execution-a"
    repo.accept(opaque_request())
    previous = repo.lease()
    assert previous is not None
    with repo.engine.begin() as connection:
        connection.execute(
            text("UPDATE translation_job SET lease_until=now()-interval '1 second'")
        )
    repo.execution_ref = "owner-execution-b"
    current = repo.lease()
    assert current is not None
    assert current["execution_ref"] == "owner-execution-b"
    assert current["token"] != previous["token"]
    assert not repo.complete(previous, success())
    assert repo.complete(
        current, {**success(), "translated_text": "Updated translation"}
    )


def test_segment_checkpoints_resume_and_are_fenced_by_execution(
    repo: Repository,
) -> None:
    event = json.loads(opaque_request())
    event["data"]["text"] = "first\nsecond"
    event["data"]["source_digest"] = digest(b"first\nsecond")
    raw = json.dumps(event).encode()
    repo.execution_ref = "owner-execution-a"
    repo.accept(raw)
    first = repo.lease()
    assert first is not None
    segments = ("first", "second")
    assert repo.prepare_segments(first, segments, "owner-execution-a") == {}
    assert repo.complete_segment(first, 0, "erste", ["UNVERIFIED"])
    with repo.engine.begin() as connection:
        connection.execute(
            text("UPDATE translation_job SET lease_until=now()-interval '1 second'")
        )
    resumed = repo.lease()
    assert resumed is not None
    assert repo.prepare_segments(resumed, segments, "owner-execution-a") == {
        0: ("erste", ["UNVERIFIED"])
    }
    with repo.engine.begin() as connection:
        connection.execute(
            text("UPDATE translation_job SET lease_until=now()-interval '1 second'")
        )
    repo.execution_ref = "owner-execution-b"
    changed = repo.lease()
    assert changed is not None
    assert repo.prepare_segments(changed, segments, "owner-execution-b") == {}


def test_request_contract_and_digest_are_checked() -> None:
    raw = request()
    assert parse(raw, "TranslationRequested")["data"]["text"] == "climate observatory"
    event = json.loads(raw)
    event["data"]["text"] = "changed"
    with pytest.raises(ValueError, match="DIGEST"):
        parse(json.dumps(event).encode(), "TranslationRequested")


def test_contract_accepts_an_independent_technical_client() -> None:
    event = json.loads(request())
    event["source"] = "urn:aelev:another-client"
    event["data"]["provenance"]["owner_context"] = "ANOTHER_CLIENT"
    event["data"]["provenance"]["producer_id"] = "another-client"
    assert parse(json.dumps(event).encode(), "TranslationRequested")["source"] == (
        "urn:aelev:another-client"
    )


def test_duplicate_computes_once_and_replays_exact_saved_result(
    repo: Repository,
) -> None:
    raw = request()
    repo.accept(raw)
    repo.accept(raw)
    lease = repo.lease()
    assert lease is not None
    assert repo.lease() is None
    assert repo.complete(lease, success())
    original = bytes(repo.pending()[0]["payload"])
    repo.published(lease["request_id"])
    assert repo.pending() == []
    repo.accept(raw)
    assert repo.lease() is None
    assert bytes(repo.pending()[0]["payload"]) == original
    assert verify(repo) == {"status": "VERIFIED", "jobs": 1}


def test_request_id_collision_preserves_original(repo: Repository) -> None:
    raw = request()
    repo.accept(raw)
    event = json.loads(raw)
    event["data"]["text"] = "biodiversity monitoring"
    event["data"]["source_digest"] = digest(b"biodiversity monitoring")
    with pytest.raises(ValueError, match="ID_CONFLICT"):
        repo.accept(json.dumps(event).encode())
    lease = repo.lease()
    assert lease is not None and bytes(lease["payload"]) == raw


def test_operator_result_requires_exact_request_and_outbox_ack(
    repo: Repository,
) -> None:
    raw = request()
    identity = parse(raw, "TranslationRequested")["id"]
    repo.accept(raw)
    lease = repo.lease()
    assert lease is not None
    assert repo.complete(lease, success())
    assert repo.delivered_result(identity, digest(raw)) is None
    repo.published(identity)
    result = repo.delivered_result(identity, digest(raw))
    assert result is not None
    assert (
        parse(result, "TranslationCompleted")["data"]["translated_text"]
        == "Klimaobservatorium"
    )
    assert repo.delivered_result(identity, digest(b"foreign")) is None


def test_retry_cooldown_does_not_block_ready_work(repo: Repository) -> None:
    first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
    repo.accept(request(identity=first_id))
    first = repo.lease()
    assert first is not None
    repo.retry(first)
    repo.accept(request(identity=second_id, value="biodiversity monitoring"))
    second = repo.lease()
    assert second is not None and second["request_id"] == second_id
    assert repo.lease() is None


@pytest.mark.parametrize("priority", [0, 100])
def test_eligible_retry_retains_original_age_and_bytes(
    repo: Repository, priority: int
) -> None:
    first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
    raw = request(identity=first_id)
    repo.accept(raw, priority=priority)
    first = repo.lease()
    assert first is not None
    with repo.engine.connect() as connection:
        created_at = connection.execute(
            text("SELECT created_at FROM translation_job WHERE request_id=:id"),
            {"id": first_id},
        ).scalar_one()
    repo.accept(request(identity=second_id), priority=priority)
    for _ in range(2):
        repo.retry(first, "TRANSLATOR_DEADLINE_EXCEEDED")
        with repo.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE translation_job SET available_at=clock_timestamp() "
                    "WHERE request_id=:id"
                ),
                {"id": first_id},
            )
        first = repo.lease()
        assert first is not None and first["request_id"] == first_id
        assert bytes(first["payload"]) == raw
    with repo.engine.connect() as connection:
        saved = connection.execute(
            text(
                "SELECT created_at,error_code,attempt_count FROM translation_job "
                "WHERE request_id=:id"
            ),
            {"id": first_id},
        ).one()
    assert saved.created_at == created_at
    assert saved.error_code == "TRANSLATOR_DEADLINE_EXCEEDED"
    assert saved.attempt_count == 3


def test_expired_lease_retains_age_and_fences_previous_attempt(
    repo: Repository,
) -> None:
    first_id, second_id = str(uuid.uuid4()), str(uuid.uuid4())
    repo.accept(request(identity=first_id))
    previous = repo.lease()
    assert previous is not None
    repo.accept(request(identity=second_id))
    with repo.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE translation_job SET lease_until=now()-interval '1 second',"
                "available_at=now()+interval '1 day' "
                "WHERE request_id=:id"
            ),
            {"id": first_id},
        )
    current = repo.lease()
    assert current is not None and current["request_id"] == first_id
    assert current["token"] != previous["token"]
    assert not repo.complete(previous, success())


def test_equal_age_is_ordered_by_request_id_not_retry_availability(
    repo: Repository,
) -> None:
    low, high = sorted((str(uuid.uuid4()), str(uuid.uuid4())))
    repo.accept(request(identity=high))
    repo.accept(request(identity=low))
    with repo.engine.begin() as connection:
        connection.execute(
            text("UPDATE translation_job SET created_at=now()-interval '1 day'")
        )
        connection.execute(
            text("UPDATE translation_job SET available_at=now() WHERE request_id=:id"),
            {"id": low},
        )
    first = repo.lease()
    assert first is not None and first["request_id"] == low


def test_priority_burst_always_releases_normal_work(repo: Repository) -> None:
    normal_id = str(uuid.uuid4())
    repo.accept(request(identity=normal_id), priority=100)
    priority_ids = [str(uuid.uuid4()) for _ in range(3)]
    for identity in priority_ids:
        repo.accept(request(identity=identity), priority=0)
    first = repo.lease(high_priority_burst=2)
    second = repo.lease(high_priority_burst=2)
    third = repo.lease(high_priority_burst=2)
    assert first is not None and first["request_id"] in priority_ids
    assert second is not None and second["request_id"] in priority_ids
    assert third is not None and third["request_id"] == normal_id


def test_restore_rejects_valid_but_mismatched_result_lineage(repo: Repository) -> None:
    repo.accept(request())
    lease = repo.lease()
    assert lease is not None and repo.complete(lease, success())
    result = json.loads(bytes(repo.pending()[0]["payload"]))
    result["data"]["target_language"] = "fr"
    changed = json.dumps(result).encode()
    parse(changed, "TranslationCompleted")
    with repo.engine.begin() as connection:
        connection.execute(
            text("UPDATE translation_job SET result=:raw"), {"raw": changed}
        )
        connection.execute(
            text("UPDATE translation_outbox SET payload=:raw"), {"raw": changed}
        )
    with pytest.raises(ValueError, match="RESTORE_RESULT_MISMATCH"):
        verify(repo)


def test_expired_lease_fences_old_result_and_retry_never_cancels(
    repo: Repository,
) -> None:
    repo.accept(request())
    first = repo.lease()
    assert first is not None
    with repo.engine.begin() as connection:
        connection.execute(
            text("UPDATE translation_job SET lease_until=now()-interval '1 second'")
        )
    second = repo.lease()
    assert second is not None and second["token"] != first["token"]
    assert not repo.complete(first, success())
    repo.retry(first)
    repo.retry(second)
    assert repo.lease() is None
    with repo.engine.begin() as connection:
        row = connection.execute(
            text("SELECT state,attempt_count FROM translation_job")
        ).one()
        assert row == ("PENDING", 2)
        connection.execute(text("UPDATE translation_job SET available_at=now()"))
    third = repo.lease()
    assert third is not None and repo.complete(third, success())


def test_pause_reset_preserves_complete_and_command_history(repo: Repository) -> None:
    repo.accept(request())
    lease = repo.lease()
    assert lease is not None
    assert (
        repo.control(
            "PAUSE",
            command_id="pause",
            actor="operator",
            expected_revision=1,
            rationale="maintenance",
        )
        == 2
    )
    assert repo.lease() is None
    with pytest.raises(ValueError, match="DRAIN_REQUIRED"):
        repo.control(
            "RESET",
            command_id="reset",
            actor="operator",
            expected_revision=2,
            rationale="retry",
        )
    assert repo.complete(lease, success())
    repo.accept(request(value="research"))
    assert (
        repo.control(
            "RESET",
            command_id="reset",
            actor="operator",
            expected_revision=2,
            rationale="retry",
        )
        == 3
    )
    assert (
        repo.control(
            "RESET",
            command_id="reset",
            actor="operator",
            expected_revision=2,
            rationale="retry",
        )
        == 3
    )
    with pytest.raises(ValueError, match="ID_CONFLICT"):
        repo.control(
            "PAUSE",
            command_id="reset",
            actor="operator",
            expected_revision=2,
            rationale="retry",
        )
    assert len(repo.pending()) == 1
    assert repo.lease() is not None


def test_invalid_completion_rolls_back_and_original_remains_leased(
    repo: Repository,
) -> None:
    repo.accept(request())
    lease = repo.lease()
    assert lease is not None
    with pytest.raises(ValueError, match="RESULT_INVALID"):
        repo.complete(lease, {**success(), "translated_text": ""})
    assert repo.pending() == []
    assert repo.complete(lease, success())
