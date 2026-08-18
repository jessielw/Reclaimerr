"""add Tracearr playback provider

Revision ID: e4a7c9d2f6b1
Revises: b6d8e1f3a5c7
Create Date: 2026-08-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4a7c9d2f6b1"
down_revision: str | Sequence[str] | None = "b6d8e1f3a5c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_SERVICE = sa.Enum(
    "SONARR",
    "RADARR",
    "JELLYFIN",
    "EMBY",
    "PLEX",
    "SEERR",
    "TAUTULLI",
    "MDBLIST",
    "OMDB",
    name="service",
)
_NEW_SERVICE = sa.Enum(
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

_SERVICE_COLUMNS = (
    ("service_configs", "service_type"),
    ("media_user_identities", "source_service"),
    ("movie_versions", "service"),
    ("series_service_refs", "service"),
    ("supplemental_media_matches", "source_service"),
    ("media_favorites", "source_service"),
    ("media_watch_users", "source_service"),
    ("media_watch_user_episodes", "source_service"),
    ("native_playback_users", "source_service"),
    ("native_playback_aggregates", "source_service"),
    ("playback_history_events", "source_service"),
    ("playback_history_user_aggregates", "observed_service"),
)


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=:name"
            ),
            {"name": table},
        ).first()
        is not None
    )


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def _alter_service_enum(existing_type: sa.Enum, new_type: sa.Enum) -> None:
    for table, column in _SERVICE_COLUMNS:
        if not _table_exists(table) or column not in _columns(table):
            continue
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                column,
                existing_type=existing_type,
                type_=new_type,
                existing_nullable=False,
            )


def upgrade() -> None:
    _alter_service_enum(_OLD_SERVICE, _NEW_SERVICE)

    with op.batch_alter_table("playback_history_events", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("observed_service", _NEW_SERVICE, nullable=True)
        )

    op.execute(
        sa.text(
            """
            UPDATE playback_history_events
            SET observed_service = CASE
                WHEN source_service = 'TAUTULLI' THEN 'PLEX'
                WHEN source_service IN ('JELLYFIN', 'EMBY', 'PLEX')
                    THEN source_service
                ELSE 'PLEX'
            END
            """
        )
    )

    with op.batch_alter_table("playback_history_events", schema=None) as batch_op:
        batch_op.alter_column(
            "observed_service",
            existing_type=_NEW_SERVICE,
            nullable=False,
        )
        batch_op.create_index(
            "ix_playback_history_events_observed_service",
            ["observed_service"],
            unique=False,
        )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM service_configs WHERE service_type = 'TRACEARR'")
    )
    op.execute(
        sa.text("DELETE FROM playback_history_events WHERE source_service = 'TRACEARR'")
    )

    with op.batch_alter_table("playback_history_events", schema=None) as batch_op:
        batch_op.drop_index("ix_playback_history_events_observed_service")
        batch_op.drop_column("observed_service")

    _alter_service_enum(_NEW_SERVICE, _OLD_SERVICE)
