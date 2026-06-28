"""split external ratings into provider tasks

Revision ID: 1d7c9e4a6b2f
Revises: f0a1b2c3d4e5
Create Date: 2026-06-27 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "1d7c9e4a6b2f"
down_revision: str | Sequence[str] | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TASK = sa.Enum(
    "SYNC_MEDIA",
    "RESYNC_MEDIA",
    "SYNC_MEDIA_LIBRARIES",
    "SYNC_LINKED_DATA",
    "SCAN_CLEANUP_CANDIDATES",
    "TAG_CLEANUP_CANDIDATES",
    "DELETE_CLEANUP_CANDIDATES",
    "WEEKLY_HOUSE_KEEPING",
    "CHECK_APP_UPDATES",
    "IMDB_RATINGS_REFRESH",
    "ANILIST_RATINGS_REFRESH",
    "REFRESH_EXTERNAL_RATINGS",
    "REFRESH_PLAYBACK_HISTORY",
    name="task",
)
_NEW_TASK = sa.Enum(
    "SYNC_MEDIA",
    "RESYNC_MEDIA",
    "SYNC_MEDIA_LIBRARIES",
    "SYNC_LINKED_DATA",
    "SCAN_CLEANUP_CANDIDATES",
    "TAG_CLEANUP_CANDIDATES",
    "DELETE_CLEANUP_CANDIDATES",
    "WEEKLY_HOUSE_KEEPING",
    "CHECK_APP_UPDATES",
    "IMDB_RATINGS_REFRESH",
    "ANILIST_RATINGS_REFRESH",
    "MDBLIST_RATINGS_REFRESH",
    "OMDB_RATINGS_REFRESH",
    "REFRESH_PLAYBACK_HISTORY",
    name="task",
)


def _add_provider_cache_columns(table: str) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("mdblist_ratings_cache", sa.JSON(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("mdblist_ratings_refreshed_at", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(sa.Column("omdb_ratings_cache", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("omdb_ratings_refreshed_at", sa.DateTime(), nullable=True)
        )


def upgrade() -> None:
    _add_provider_cache_columns("movies")
    _add_provider_cache_columns("series")

    bind = op.get_bind()
    legacy = bind.execute(
        sa.text(
            "SELECT id, enabled FROM task_schedules "
            "WHERE task = 'REFRESH_EXTERNAL_RATINGS' ORDER BY id LIMIT 1"
        )
    ).mappings().first()
    bind.execute(
        sa.text("DELETE FROM task_runs WHERE task = 'REFRESH_EXTERNAL_RATINGS'")
    )
    bind.execute(
        sa.text(
            "UPDATE background_jobs SET "
            "payload = json_set(payload, '$.task', 'mdblist_ratings_refresh'), "
            "dedupe_key = CASE WHEN dedupe_key = 'task-run-refresh_external_ratings' "
            "THEN 'task-run-mdblist_ratings_refresh' ELSE dedupe_key END "
            "WHERE job_type = 'TASK_RUN' "
            "AND json_extract(payload, '$.task') = 'refresh_external_ratings'"
        )
    )
    if legacy is not None:
        bind.execute(
            sa.text(
                "UPDATE task_schedules SET task = 'MDBLIST_RATINGS_REFRESH', "
                "description = :description WHERE id = :schedule_id"
            ),
            {
                "description": "Refreshes ratings from the configured MDBList provider",
                "schedule_id": legacy["id"],
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO task_schedules "
                "(task, schedule_type, schedule_value, default_schedule_type, "
                "default_schedule_value, description, enabled) "
                "VALUES ('OMDB_RATINGS_REFRESH', 'CRON', '0 7 * * *', 'CRON', "
                "'0 7 * * *', :description, :enabled)"
            ),
            {
                "description": (
                    "Refreshes OMDb fallback ratings for missing Tomatometer and "
                    "Metacritic values"
                ),
                "enabled": legacy["enabled"],
            },
        )

    with op.batch_alter_table("task_schedules", schema=None) as batch_op:
        batch_op.alter_column(
            "task",
            existing_type=_OLD_TASK,
            type_=_NEW_TASK,
            existing_nullable=False,
        )
    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.alter_column(
            "task",
            existing_type=_OLD_TASK,
            type_=_NEW_TASK,
            existing_nullable=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM task_runs "
            "WHERE task IN ('MDBLIST_RATINGS_REFRESH', 'OMDB_RATINGS_REFRESH')"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE background_jobs SET "
            "payload = json_set(payload, '$.task', 'refresh_external_ratings'), "
            "dedupe_key = CASE WHEN dedupe_key = 'task-run-mdblist_ratings_refresh' "
            "THEN 'task-run-refresh_external_ratings' ELSE dedupe_key END "
            "WHERE job_type = 'TASK_RUN' "
            "AND json_extract(payload, '$.task') = 'mdblist_ratings_refresh'"
        )
    )
    bind.execute(
        sa.text("DELETE FROM task_schedules WHERE task = 'OMDB_RATINGS_REFRESH'")
    )
    bind.execute(
        sa.text(
            "UPDATE task_schedules SET task = 'REFRESH_EXTERNAL_RATINGS', "
            "description = :description WHERE task = 'MDBLIST_RATINGS_REFRESH'"
        ),
        {
            "description": (
                "Refreshes external ratings from configured MDBList and OMDb providers"
            )
        },
    )

    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.alter_column(
            "task",
            existing_type=_NEW_TASK,
            type_=_OLD_TASK,
            existing_nullable=False,
        )
    with op.batch_alter_table("task_schedules", schema=None) as batch_op:
        batch_op.alter_column(
            "task",
            existing_type=_NEW_TASK,
            type_=_OLD_TASK,
            existing_nullable=False,
        )

    for table in ("series", "movies"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("omdb_ratings_refreshed_at")
            batch_op.drop_column("omdb_ratings_cache")
            batch_op.drop_column("mdblist_ratings_refreshed_at")
            batch_op.drop_column("mdblist_ratings_cache")
