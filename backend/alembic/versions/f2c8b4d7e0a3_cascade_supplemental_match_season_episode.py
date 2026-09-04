"""cascade supplemental media matches when a season or episode is deleted

supplemental_media_matches.season_id and .episode_id were declared with no
ON DELETE action, so every path that hard-deletes a season (candidate delete,
candidate move, sync pruning a season the media server no longer reports) hit

    (sqlite3.IntegrityError) FOREIGN KEY constraint failed
    [SQL: DELETE FROM seasons WHERE seasons.id = ?]

on any install with a linked media server, since only linked servers write
supplemental matches. The season delete also cascades to episodes, so a match
row holding just episode_id blocked it the same way.

None of the hand-rolled cleanup in cleanup.py / sync.py touched this table, so
the fix belongs on the constraint: CASCADE, not SET NULL, because a row is keyed
to a single linked-server item and a match whose local season/episode is gone
maps nothing.

Also purges rows already orphaned by deletes that ran while PRAGMA foreign_keys
was off for that pooled connection - the new table cannot be populated from
them. Harmless: the next linked sync rebuilds a config's matches wholesale
(see _replace_supplemental_matches).

Revision ID: f2c8b4d7e0a3
Revises: e7b1d4a9c3f5
Create Date: 2026-09-04 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2c8b4d7e0a3"
down_revision: str | Sequence[str] | None = "e7b1d4a9c3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    ("ix_supplemental_media_matches_source_service", "source_service"),
    (
        "ix_supplemental_media_matches_source_service_config_id",
        "source_service_config_id",
    ),
    ("ix_supplemental_media_matches_media_type", "media_type"),
    ("ix_supplemental_media_matches_movie_id", "movie_id"),
    ("ix_supplemental_media_matches_series_id", "series_id"),
    ("ix_supplemental_media_matches_season_id", "season_id"),
    ("ix_supplemental_media_matches_episode_id", "episode_id"),
)

_COLUMNS = """
                id, source_service, source_service_config_id, source_item_id,
                media_type, movie_id, series_id, season_id, episode_id,
                source_media_id, path_tail, confidence, signals, updated_at
"""


def _rebuild(scope_ondelete: str) -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS supplemental_media_matches_new"))

    # Rows whose parent is already gone cannot be copied into a table that
    # enforces the FK on rebuild.
    op.execute(
        sa.text(
            """
            DELETE FROM supplemental_media_matches
            WHERE (season_id IS NOT NULL
                   AND season_id NOT IN (SELECT id FROM seasons))
               OR (episode_id IS NOT NULL
                   AND episode_id NOT IN (SELECT id FROM episodes))
            """
        )
    )

    op.execute(
        sa.text(
            f"""
            CREATE TABLE supplemental_media_matches_new (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                source_service VARCHAR(8) NOT NULL,
                source_service_config_id INTEGER NOT NULL,
                source_item_id VARCHAR(100) NOT NULL,
                media_type VARCHAR(6) NOT NULL,
                movie_id INTEGER,
                series_id INTEGER,
                season_id INTEGER,
                episode_id INTEGER,
                source_media_id VARCHAR(100),
                path_tail VARCHAR(1024),
                confidence SMALLINT NOT NULL,
                signals JSON,
                updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                CONSTRAINT uq_supplemental_media_match_source_item
                    UNIQUE (source_service_config_id, source_item_id, media_type),
                FOREIGN KEY(movie_id) REFERENCES movies (id),
                FOREIGN KEY(series_id) REFERENCES series (id),
                FOREIGN KEY(season_id) REFERENCES seasons (id){scope_ondelete},
                FOREIGN KEY(episode_id) REFERENCES episodes (id){scope_ondelete},
                FOREIGN KEY(source_service_config_id)
                    REFERENCES service_configs (id) ON DELETE CASCADE
            )
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO supplemental_media_matches_new ({_COLUMNS})
            SELECT {_COLUMNS} FROM supplemental_media_matches
            """
        )
    )
    op.execute(sa.text("DROP TABLE supplemental_media_matches"))
    op.execute(
        sa.text(
            "ALTER TABLE supplemental_media_matches_new "
            "RENAME TO supplemental_media_matches"
        )
    )

    for name, column in _INDEXES:
        op.create_index(name, "supplemental_media_matches", [column])


def upgrade() -> None:
    _rebuild(" ON DELETE CASCADE")


def downgrade() -> None:
    _rebuild("")
