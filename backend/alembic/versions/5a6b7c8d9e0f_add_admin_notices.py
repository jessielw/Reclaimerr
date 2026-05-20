"""add admin notices table

Revision ID: 5a6b7c8d9e0f
Revises: 4f1a2b3c4d5e
Create Date: 2026-05-19 14:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5a6b7c8d9e0f"
down_revision: str | Sequence[str] | None = "4f1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_notices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=80), nullable=False),
        sa.Column(
            "severity",
            sa.String(length=16),
            nullable=False,
            server_default="warning",
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("action_label", sa.String(length=100), nullable=True),
        sa.Column("action_href", sa.String(length=500), nullable=True),
        sa.Column("context_json", sa.JSON(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "is_read",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("read_by_user_id", sa.Integer(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=120), nullable=True),
        sa.Column(
            "last_occurred_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["read_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_admin_notices_dedupe_key"),
    )
    op.create_index(
        "ix_admin_notices_is_active", "admin_notices", ["is_active"], unique=False
    )
    op.create_index(
        "ix_admin_notices_is_read", "admin_notices", ["is_read"], unique=False
    )
    op.create_index("ix_admin_notices_kind", "admin_notices", ["kind"], unique=False)
    op.create_index(
        "ix_admin_notices_dedupe_key", "admin_notices", ["dedupe_key"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_admin_notices_dedupe_key", table_name="admin_notices")
    op.drop_index("ix_admin_notices_kind", table_name="admin_notices")
    op.drop_index("ix_admin_notices_is_read", table_name="admin_notices")
    op.drop_index("ix_admin_notices_is_active", table_name="admin_notices")
    op.drop_table("admin_notices")
