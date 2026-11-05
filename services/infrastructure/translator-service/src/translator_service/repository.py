"""Durable inbox, fenced leases and immutable replayable results."""

import json
import uuid
from typing import Any

from sqlalchemy import URL, create_engine, text

from translator_service.contracts import digest, event_name, parse, result_bytes
from translator_service.schema import verify_schema


class Repository:
    def __init__(self, url: str | URL, *, execution_ref: str | None = None) -> None:
        self.execution_ref = execution_ref
        self.engine = create_engine(
            url,
            pool_size=2,
            max_overflow=2,
            pool_timeout=5,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={"options": "-c statement_timeout=5000 -c lock_timeout=1000"},
        )
        verify_schema(self.engine)

    def accept(self, raw: bytes, priority: int = 100) -> None:
        if priority not in {0, 100}:
            raise ValueError("TRANSLATION_PRIORITY_INVALID")
        event = parse(raw, event_name(raw, "TranslationRequested"))
        request_id = event["data"]["request_id"]
        with self.engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO translation_job
                (request_id,request_digest,payload,priority)
                VALUES(:id,:digest,:raw,:priority)
                ON CONFLICT DO NOTHING"""),
                {
                    "id": request_id,
                    "digest": digest(raw),
                    "raw": raw,
                    "priority": priority,
                },
            )
            row = (
                connection.execute(
                    text("""SELECT request_digest,state FROM translation_job
                WHERE request_id=:id FOR UPDATE"""),
                    {"id": request_id},
                )
                .mappings()
                .one()
            )
            if row["request_digest"] != digest(raw):
                raise ValueError("TRANSLATION_REQUEST_ID_CONFLICT")
            connection.execute(
                text("""UPDATE translation_job SET priority=LEAST(priority,:priority)
                WHERE request_id=:id AND state IN ('PENDING','RUNNING')"""),
                {"id": request_id, "priority": priority},
            )
            if row["state"] in {"SUCCEEDED", "FAILED"}:
                connection.execute(
                    text("""UPDATE translation_outbox SET state='PENDING'
                    WHERE request_id=:id"""),
                    {"id": request_id},
                )

    def lease(self, high_priority_burst: int = 4) -> dict[str, Any] | None:
        if high_priority_burst < 1:
            raise ValueError("TRANSLATION_PRIORITY_BURST_INVALID")
        token = str(uuid.uuid4())
        with self.engine.begin() as connection:
            control = (
                connection.execute(
                    text(
                        "SELECT desired_state,priority_streak "
                        "FROM translation_control FOR UPDATE"
                    )
                )
                .mappings()
                .one()
            )
            if control["desired_state"] != "RUNNING":
                return None
            prefer_normal = control["priority_streak"] >= high_priority_burst
            row = (
                connection.execute(
                    text("""SELECT request_id,payload,request_digest,priority,
                execution_ref
                FROM translation_job WHERE
                (state='PENDING' AND available_at<=now()
                 OR state='RUNNING' AND lease_until<=now())
                AND EXISTS(SELECT 1 FROM translation_control
                           WHERE desired_state='RUNNING')
                ORDER BY
                  CASE WHEN :prefer_normal AND priority=100 THEN 0
                       WHEN NOT :prefer_normal AND priority=0 THEN 0 ELSE 1 END,
                  priority,created_at,request_id
                FOR UPDATE SKIP LOCKED LIMIT 1"""),
                    {"prefer_normal": prefer_normal},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            v2 = json.loads(row["payload"])["data"]["schema_version"] == "2.0.0"
            execution_ref = self.execution_ref if v2 else None
            if v2 and not execution_ref:
                raise RuntimeError("TRANSLATOR_EXECUTION_NOT_CONFIGURED")
            connection.execute(
                text("""UPDATE translation_job SET state='RUNNING',
                lease_token=:token,lease_until=now()+interval '360 seconds',
                attempt_count=attempt_count+1,execution_ref=:execution_ref
                WHERE request_id=:id"""),
                {
                    "id": row["request_id"],
                    "token": token,
                    "execution_ref": execution_ref,
                },
            )
            connection.execute(
                text(
                    "UPDATE translation_control SET priority_streak="
                    "CASE WHEN :priority=0 THEN priority_streak+1 ELSE 0 END"
                ),
                {"priority": row["priority"]},
            )
            return {**row, "token": token, "execution_ref": execution_ref}

    def status(self) -> dict[str, Any]:
        """Return aggregate operational state without request data or identities."""
        with self.engine.connect() as connection:
            control = dict(
                connection.execute(
                    text(
                        "SELECT desired_state,revision,priority_streak "
                        "FROM translation_control"
                    )
                )
                .mappings()
                .one()
            )
            jobs: dict[str, int] = {}
            rows = connection.execute(
                text(
                    "SELECT state,priority,count(*) AS count FROM translation_job "
                    "GROUP BY state,priority"
                )
            ).mappings()
            for row in rows:
                queue = "priority" if row["priority"] == 0 else "normal"
                jobs[f"{row['state']}:{queue}"] = int(row["count"])
            return {"control": control, "jobs": jobs}

    def complete(self, lease: dict[str, Any], response: dict[str, Any]) -> bool:
        raw = result_bytes(
            json.loads(lease["payload"]), lease["request_digest"], response
        )
        with self.engine.begin() as connection:
            changed = connection.execute(
                text("""UPDATE translation_job SET state=:state,
                result=:raw, completed_at=now(),lease_token=NULL,lease_until=NULL,
                error_code=:error
                WHERE request_id=:id AND state='RUNNING'
                AND lease_token=:token AND lease_until>now()
                RETURNING request_id"""),
                {
                    "id": lease["request_id"],
                    "token": lease["token"],
                    "state": response["status"],
                    "error": response["error_code"] or None,
                    "raw": raw,
                },
            ).first()
            if changed is None:
                return False
            connection.execute(
                text("""INSERT INTO translation_outbox(request_id,payload)
                VALUES(:id,:raw)"""),
                {"id": lease["request_id"], "raw": raw},
            )
            return True

    def prepare_segments(
        self,
        lease: dict[str, Any],
        segments: tuple[str, ...],
        execution_ref: str,
    ) -> dict[int, tuple[str, list[str]]]:
        """Create or resume exact per-segment checkpoints for one fenced lease."""
        if not segments or not execution_ref:
            raise ValueError("TRANSLATOR_SEGMENTS_INVALID")
        expected = [(digest(value.encode()), value) for value in segments]
        with self.engine.begin() as connection:
            owned = connection.execute(
                text(
                    "SELECT request_id FROM translation_job WHERE request_id=:id "
                    "AND state='RUNNING' AND lease_token=:token "
                    "AND lease_until>now() FOR UPDATE"
                ),
                {"id": lease["request_id"], "token": lease["token"]},
            ).first()
            if owned is None:
                raise RuntimeError("TRANSLATOR_STALE_LEASE")
            rows = list(
                connection.execute(
                    text(
                        "SELECT segment_index,execution_ref,source_digest,source_text,"
                        "translated_text,warnings FROM translation_segment "
                        "WHERE request_id=:id ORDER BY segment_index"
                    ),
                    {"id": lease["request_id"]},
                ).mappings()
            )
            matches = len(rows) == len(expected) and all(
                row["segment_index"] == index
                and row["execution_ref"] == execution_ref
                and row["source_digest"] == source_digest
                and row["source_text"] == source_text
                for index, (row, (source_digest, source_text)) in enumerate(
                    zip(rows, expected, strict=True)
                )
            )
            if rows and not matches:
                connection.execute(
                    text("DELETE FROM translation_segment WHERE request_id=:id"),
                    {"id": lease["request_id"]},
                )
                rows = []
            if not rows:
                connection.execute(
                    text(
                        "INSERT INTO translation_segment "
                        "(request_id,segment_index,execution_ref,"
                        "source_digest,source_text) "
                        "VALUES(:id,:index,:execution_ref,:digest,:text)"
                    ),
                    [
                        {
                            "id": lease["request_id"],
                            "index": index,
                            "execution_ref": execution_ref,
                            "digest": source_digest,
                            "text": source_text,
                        }
                        for index, (source_digest, source_text) in enumerate(expected)
                    ],
                )
                return {}
            return {
                int(row["segment_index"]): (
                    str(row["translated_text"]),
                    list(row["warnings"]),
                )
                for row in rows
                if row["translated_text"] is not None
                and isinstance(row["warnings"], list)
            }

    def complete_segment(
        self,
        lease: dict[str, Any],
        index: int,
        translated_text: str,
        warnings: list[str],
    ) -> bool:
        """Persist one segment and renew only the still-owned job lease."""
        with self.engine.begin() as connection:
            changed = connection.execute(
                text(
                    "UPDATE translation_segment s SET translated_text=:translated,"
                    "warnings=CAST(:warnings AS jsonb),completed_at=now() "
                    "FROM translation_job j WHERE s.request_id=:id "
                    "AND s.segment_index=:index AND j.request_id=s.request_id "
                    "AND j.state='RUNNING' AND j.lease_token=:token "
                    "AND j.lease_until>now() RETURNING s.segment_index"
                ),
                {
                    "id": lease["request_id"],
                    "index": index,
                    "token": lease["token"],
                    "translated": translated_text,
                    "warnings": json.dumps(warnings),
                },
            ).first()
            if changed is None:
                return False
            connection.execute(
                text(
                    "UPDATE translation_job SET "
                    "lease_until=now()+interval '360 seconds' "
                    "WHERE request_id=:id AND lease_token=:token"
                ),
                {"id": lease["request_id"], "token": lease["token"]},
            )
            return True

    def retry(
        self, lease: dict[str, Any], error_code: str = "TRANSLATOR_UNAVAILABLE"
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""UPDATE translation_job SET state='PENDING',
                lease_token=NULL,lease_until=NULL,
                available_at=now()+interval '30 seconds',error_code=:error
                WHERE request_id=:id AND state='RUNNING' AND lease_token=:token"""),
                {
                    "id": lease["request_id"],
                    "token": lease["token"],
                    "error": error_code,
                },
            )

    def quarantine(self, identity: str, raw: bytes, error: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO translation_quarantine(identity,payload,error_code)
                VALUES(:id,:raw,:error) ON CONFLICT DO NOTHING"""),
                {"id": identity, "raw": raw, "error": error},
            )

    def pending(self) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in (
                    connection.execute(
                        text("""SELECT request_id,payload FROM translation_outbox
                WHERE state='PENDING' ORDER BY request_id LIMIT 25""")
                    ).mappings()
                )
            ]

    def published(self, identity: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""UPDATE translation_outbox SET state='PUBLISHED',
                published_at=now(),attempt_count=attempt_count+1
                WHERE request_id=:id"""),
                {"id": identity},
            )

    def delivered_result(self, identity: str, request_digest: str) -> bytes | None:
        """Read an exact own request's result only after the outbox Kafka ACK."""
        with self.engine.connect() as connection:
            raw = connection.execute(
                text(
                    "SELECT o.payload FROM translation_outbox o "
                    "JOIN translation_job j USING(request_id) "
                    "WHERE j.request_id=:id AND j.request_digest=:digest "
                    "AND o.state='PUBLISHED' AND o.published_at IS NOT NULL"
                ),
                {"id": identity, "digest": request_digest},
            ).scalar_one_or_none()
            return bytes(raw) if raw is not None else None

    def control(
        self,
        action: str,
        *,
        command_id: str,
        actor: str,
        expected_revision: int,
        rationale: str,
    ) -> int:
        if (
            action not in {"PAUSE", "RESUME", "RESET"}
            or not command_id
            or not actor
            or not rationale
        ):
            raise ValueError("TRANSLATOR_CONTROL_INVALID")
        with self.engine.begin() as connection:
            current = connection.execute(
                text("SELECT revision FROM translation_control FOR UPDATE")
            ).scalar_one()
            previous = (
                connection.execute(
                    text(
                        "SELECT * FROM translation_control_command WHERE command_id=:id"
                    ),
                    {"id": command_id},
                )
                .mappings()
                .first()
            )
            if previous:
                if any(
                    previous[key] != value
                    for key, value in {
                        "actor": actor,
                        "action": action,
                        "expected_revision": expected_revision,
                        "rationale": rationale,
                    }.items()
                ):
                    raise ValueError("TRANSLATOR_COMMAND_ID_CONFLICT")
                return previous["resulting_revision"]
            if current != expected_revision:
                raise ValueError("TRANSLATOR_CONTROL_REVISION_CONFLICT")
            if action == "RESET":
                if connection.execute(
                    text(
                        "SELECT count(*) FROM translation_job "
                        "WHERE state='RUNNING' AND lease_until>now()"
                    )
                ).scalar_one():
                    raise ValueError("TRANSLATOR_CONTROL_DRAIN_REQUIRED")
                connection.execute(
                    text(
                        "UPDATE translation_job SET state='PENDING', "
                        "available_at=now(),lease_token=NULL,lease_until=NULL "
                        "WHERE state IN ('PENDING','RUNNING')"
                    )
                )
            revision = current + 1
            connection.execute(
                text(
                    "UPDATE translation_control "
                    "SET desired_state=:state,revision=:revision"
                ),
                {
                    "state": "PAUSED" if action == "PAUSE" else "RUNNING",
                    "revision": revision,
                },
            )
            connection.execute(
                text("""INSERT INTO translation_control_command
                (command_id,actor,action,expected_revision,resulting_revision,rationale)
                VALUES(:id,:actor,:action,:expected,:result,:rationale)"""),
                {
                    "id": command_id,
                    "actor": actor,
                    "action": action,
                    "expected": expected_revision,
                    "result": revision,
                    "rationale": rationale,
                },
            )
            return revision
