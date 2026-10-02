"""spoken-language report per transcript

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03 10:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("transcripts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("language_info", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("transcripts", schema=None) as batch_op:
        batch_op.drop_column("language_info")
