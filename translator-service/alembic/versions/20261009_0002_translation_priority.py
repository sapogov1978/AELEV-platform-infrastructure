"""Add service-owned priority scheduling without trusting request payloads."""

from alembic import op

revision = "20261009_0002"
down_revision = "20261007_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE translation_job ADD COLUMN priority smallint NOT NULL DEFAULT 100"
    )
    op.execute(
        "ALTER TABLE translation_job ADD CONSTRAINT translation_job_priority "
        "CHECK (priority IN (0,100))"
    )
    op.execute(
        "ALTER TABLE translation_control ADD COLUMN priority_streak integer "
        "NOT NULL DEFAULT 0 CHECK (priority_streak >= 0)"
    )
    op.execute("DROP INDEX translation_job_queue")
    op.execute(
        "CREATE INDEX translation_job_queue ON translation_job"
        "(state,priority,available_at,created_at)"
    )


def downgrade() -> None:
    raise RuntimeError("Translator history must be preserved")
