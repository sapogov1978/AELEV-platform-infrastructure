"""Create translator-owned durable queues without touching a caller's storage."""

from alembic import op

DDL = """
CREATE TABLE translation_job (
 request_id text PRIMARY KEY, request_digest text NOT NULL,
 payload bytea NOT NULL, state text NOT NULL DEFAULT 'PENDING', result bytea,
 lease_token text, lease_until timestamptz,
 available_at timestamptz NOT NULL DEFAULT now(),
 attempt_count integer NOT NULL DEFAULT 0, error_code text,
 created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
 CHECK (state IN ('PENDING','RUNNING','SUCCEEDED','FAILED'))
);
CREATE INDEX translation_job_queue ON translation_job(state,available_at,created_at);
CREATE TABLE translation_outbox (
 request_id text PRIMARY KEY REFERENCES translation_job,
 payload bytea NOT NULL, state text NOT NULL DEFAULT 'PENDING',
 published_at timestamptz, attempt_count integer NOT NULL DEFAULT 0
);
CREATE TABLE translation_quarantine (
 identity text PRIMARY KEY, payload bytea NOT NULL, error_code text NOT NULL,
 recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE translation_control (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 desired_state text NOT NULL DEFAULT 'RUNNING'
 CHECK(desired_state IN ('RUNNING','PAUSED')),
 revision integer NOT NULL DEFAULT 1
);
INSERT INTO translation_control(singleton) VALUES(true);
CREATE TABLE translation_control_command (
 command_id text PRIMARY KEY, actor text NOT NULL, action text NOT NULL,
 expected_revision integer NOT NULL, resulting_revision integer NOT NULL,
 rationale text NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now()
);
"""

revision = "20261007_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Translator history must be preserved")
