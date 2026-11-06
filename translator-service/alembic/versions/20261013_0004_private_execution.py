"""Record execution attempts inside the owner without changing request bytes."""

import sqlalchemy as sa

from alembic import op

revision = "20261013_0004"
down_revision = "20261010_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "translation_job", sa.Column("execution_ref", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    raise RuntimeError("Translation execution history must be preserved")
