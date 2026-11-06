"""Persist checkpoints for long text translations."""

import sqlalchemy as sa

from alembic import op

revision = "20261014_0005"
down_revision = "20261013_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "translation_segment",
        sa.Column(
            "request_id",
            sa.Text(),
            sa.ForeignKey("translation_job.request_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("execution_ref", sa.Text(), nullable=False),
        sa.Column("source_digest", sa.Text(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("translated_text", sa.Text(), nullable=True),
        sa.Column("warnings", sa.JSON(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("segment_index >= 0"),
        sa.PrimaryKeyConstraint("request_id", "segment_index"),
    )


def downgrade() -> None:
    raise RuntimeError("Translation checkpoints must be preserved")
