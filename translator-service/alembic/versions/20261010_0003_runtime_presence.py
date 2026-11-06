"""Rebuildable technical process presence; never create DDL in a worker."""

from alembic import op

revision = "20261010_0003"
down_revision = "20261009_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE translation_runtime_presence (
        component text NOT NULL, instance text NOT NULL,
        observed_at timestamptz NOT NULL,
        state text NOT NULL CHECK (state IN ('ONLINE','OFFLINE','UNKNOWN')),
        PRIMARY KEY(component, instance))"""
    )


def downgrade() -> None:
    raise RuntimeError("Translator history must be preserved")
