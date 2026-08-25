"""add watch user aliases

Revision ID: c1d5e9a3f70b
Revises: b4e8f1c2d6a9
Create Date: 2026-08-21 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1d5e9a3f70b"
down_revision: str | Sequence[str] | None = "b4e8f1c2d6a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "watch_user_aliases"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE in inspector.get_table_names():
        return

    service_enum = sa.Enum(
        "SONARR",
        "RADARR",
        "JELLYFIN",
        "EMBY",
        "PLEX",
        "SEERR",
        "TAUTULLI",
        "TRACEARR",
        "MDBLIST",
        "OMDB",
        name="service",
    )

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_service", service_enum, nullable=False),
        sa.Column("source_service_config_id", sa.Integer(), nullable=False),
        sa.Column("observed_service", service_enum, nullable=False),
        sa.Column("provider_user_id", sa.String(length=255), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("alias_normalized", sa.String(length=255), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_service_config_id"], ["service_configs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_service_config_id",
            "provider_user_id",
            "alias_normalized",
            name="uq_watch_user_alias_identity",
        ),
    )
    op.create_index(
        op.f("ix_watch_user_aliases_source_service"),
        TABLE,
        ["source_service"],
    )
    op.create_index(
        op.f("ix_watch_user_aliases_source_service_config_id"),
        TABLE,
        ["source_service_config_id"],
    )
    op.create_index(
        op.f("ix_watch_user_aliases_observed_service"),
        TABLE,
        ["observed_service"],
    )
    op.create_index(
        op.f("ix_watch_user_aliases_provider_user_id"),
        TABLE,
        ["provider_user_id"],
    )
    op.create_index(
        op.f("ix_watch_user_aliases_alias_normalized"),
        TABLE,
        ["alias_normalized"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return
    op.drop_table(TABLE)
