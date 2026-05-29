"""add media user identities table

Revision ID: b7c8d9e0f1a2
Revises: 9f7e6d5c4b3a
Create Date: 2026-05-28 15:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = "9f7e6d5c4b3a"
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


def _index_exists(index: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = :name LIMIT 1"
        ),
        {"name": index},
    ).first()
    return row is not None


def upgrade() -> None:
    table_name = "media_user_identities"
    if not _table_exists(table_name):
        op.create_table(
            table_name,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column(
                "source_service",
                sa.Enum(
                    "SONARR",
                    "RADARR",
                    "JELLYFIN",
                    "EMBY",
                    "PLEX",
                    "SEERR",
                    "TAUTULLI",
                    name="service",
                ),
                nullable=False,
            ),
            sa.Column("source_service_config_id", sa.Integer(), nullable=False),
            sa.Column("source_user_id", sa.String(length=255), nullable=False),
            sa.Column("username", sa.String(length=255), nullable=False),
            sa.Column("username_normalized", sa.String(length=255), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("email", sa.String(length=255), nullable=True),
            sa.Column("display_name", sa.String(length=255), nullable=True),
            sa.Column("raw", sa.JSON(), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
            sa.Column("linked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(
                ["source_service_config_id"],
                ["service_configs.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source_service",
                "source_service_config_id",
                "source_user_id",
                name="uq_media_user_identities_source_user",
            ),
        )

    indexes = {
        "ix_media_user_identities_source_service": ["source_service"],
        "ix_media_user_identities_source_service_config_id": [
            "source_service_config_id"
        ],
        "ix_media_user_identities_username_normalized": ["username_normalized"],
        "ix_media_user_identities_user_id": ["user_id"],
        "ix_media_user_identities_email": ["email"],
        "ix_media_user_identities_last_seen_at": ["last_seen_at"],
    }
    for index_name, columns in indexes.items():
        if not _index_exists(index_name):
            op.create_index(index_name, table_name, columns)


def downgrade() -> None:
    table_name = "media_user_identities"
    for index_name in (
        "ix_media_user_identities_last_seen_at",
        "ix_media_user_identities_email",
        "ix_media_user_identities_user_id",
        "ix_media_user_identities_username_normalized",
        "ix_media_user_identities_source_service_config_id",
        "ix_media_user_identities_source_service",
    ):
        if _index_exists(index_name):
            op.drop_index(index_name, table_name=table_name)

    if _table_exists(table_name):
        op.drop_table(table_name)
