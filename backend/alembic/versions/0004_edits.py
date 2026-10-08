"""auto edits

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08 10:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "edits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("style", sa.String(length=24), nullable=False),
        sa.Column("style_auto", sa.Boolean(), nullable=False),
        sa.Column("duration", sa.Float(), nullable=False),
        sa.Column("music", sa.Boolean(), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_video_ids", JSON, nullable=False),
        sa.Column("plan", JSON, nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("render_key", sa.String(length=500), nullable=True),
        sa.Column("thumbnail_key", sa.String(length=500), nullable=True),
        sa.Column("render_meta", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_edits_status"), "edits", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_edits_status"), table_name="edits")
    op.drop_table("edits")
