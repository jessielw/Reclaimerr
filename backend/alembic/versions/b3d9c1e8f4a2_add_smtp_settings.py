"""Add instance-wide SMTP settings and the notification delivery channel.

Revision ID: b3d9c1e8f4a2
Revises: a1c5f7e2d940
Create Date: 2026-09-20 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d9c1e8f4a2"
down_revision: str | Sequence[str] | None = "a1c5f7e2d940"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name LIMIT 1"
        ),
        {"name": table},
    ).first()
    return row is not None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    if not _table_exists("smtp_settings"):
        op.create_table(
            "smtp_settings",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column(
                "enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column(
                "host",
                sa.String(length=255),
                nullable=False,
                server_default=sa.text("''"),
            ),
            sa.Column(
                "port", sa.Integer(), nullable=False, server_default=sa.text("587")
            ),
            sa.Column(
                "security",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'starttls'"),
            ),
            sa.Column(
                "username",
                sa.String(length=255),
                nullable=False,
                server_default=sa.text("''"),
            ),
            sa.Column(
                "password", sa.Text(), nullable=False, server_default=sa.text("''")
            ),
            sa.Column(
                "from_address",
                sa.String(length=255),
                nullable=False,
                server_default=sa.text("''"),
            ),
            sa.Column(
                "from_name",
                sa.String(length=100),
                nullable=False,
                server_default=sa.text("'Reclaimerr'"),
            ),
            sa.Column("reply_to", sa.String(length=255), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(
                ["updated_by_user_id"], ["users.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    # Existing rows are all Apprise destinations, so the channel backfills to
    # "apprise" and url stays populated; only a system_email row leaves it null.
    notification_cols = _cols("notification_settings")
    with op.batch_alter_table("notification_settings", schema=None) as batch_op:
        if "channel" not in notification_cols:
            batch_op.add_column(
                sa.Column(
                    "channel",
                    sa.String(length=16),
                    nullable=False,
                    server_default=sa.text("'apprise'"),
                )
            )
        if "target_email" not in notification_cols:
            batch_op.add_column(
                sa.Column("target_email", sa.String(length=255), nullable=True)
            )
        batch_op.alter_column("url", existing_type=sa.String(length=500), nullable=True)


def downgrade() -> None:
    if _table_exists("smtp_settings"):
        op.drop_table("smtp_settings")

    # Drop the email-only rows first: they carry no url, so they cannot survive
    # the column going back to NOT NULL.
    op.execute(
        sa.text("DELETE FROM notification_settings WHERE channel = 'system_email'")
    )

    notification_cols = _cols("notification_settings")
    with op.batch_alter_table("notification_settings", schema=None) as batch_op:
        batch_op.alter_column(
            "url",
            existing_type=sa.String(length=500),
            nullable=False,
            server_default=sa.text("''"),
        )
        if "target_email" in notification_cols:
            batch_op.drop_column("target_email")
        if "channel" in notification_cols:
            batch_op.drop_column("channel")
