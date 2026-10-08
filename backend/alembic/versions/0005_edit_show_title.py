"""auto edit: show the name as text (at the end)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-08 18:00:00
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("edits", schema=None) as batch_op:
        batch_op.add_column(sa.Column("show_title", sa.Boolean(), server_default=sa.true(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("edits", schema=None) as batch_op:
        batch_op.drop_column("show_title")
