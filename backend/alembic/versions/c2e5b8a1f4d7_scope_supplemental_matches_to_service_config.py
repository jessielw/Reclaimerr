"""scope supplemental media matches to a specific service config

Adds source_service_config_id to supplemental_media_matches and moves the
uniqueness key from (source_service, source_item_id, media_type) to
(source_service_config_id, source_item_id, media_type). This is required to
support multiple ServiceConfig rows of the same media-server type (e.g. two
Plex servers) contributing supplemental watch-state matches independently -
previously a second same-type linked server's matches would collide with the
first's under the type-only unique constraint.

Revision ID: c2e5b8a1f4d7
Revises: a4c8e1b60f37
Create Date: 2026-08-26 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2e5b8a1f4d7"
down_revision: str | None = "a4c8e1b60f37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS supplemental_media_matches_new"))

    if "source_service_config_id" not in _cols("supplemental_media_matches"):
        with op.batch_alter_table(
            "supplemental_media_matches", schema=None
        ) as batch_op:
            batch_op.add_column(
                sa.Column("source_service_config_id", sa.Integer(), nullable=True)
            )

    # Backfill: pre-existing installs have at most one ServiceConfig row per
    # service_type, so this unambiguously recovers the correct config for
    # every existing match.
    op.execute(
        sa.text(
            """
            UPDATE supplemental_media_matches
            SET source_service_config_id = (
                SELECT id FROM service_configs
                WHERE service_configs.service_type = supplemental_media_matches.source_service
                LIMIT 1
            )
            WHERE source_service_config_id IS NULL
            """
        )
    )

    # Orphaned rows (originating service no longer configured) can't satisfy
    # the new NOT NULL FK - drop them. Harmless: the next sync/linked-sync
    # pass rebuilds supplemental matches from scratch for any enabled server.
    op.execute(
        sa.text(
            "DELETE FROM supplemental_media_matches WHERE source_service_config_id IS NULL"
        )
    )

    # Rebuild explicitly so SQLite swaps the unique constraint from
    # (source_service, source_item_id, media_type) to
    # (source_service_config_id, source_item_id, media_type) and adds the FK.
    op.execute(
        sa.text(
            """
            CREATE TABLE supplemental_media_matches_new (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                source_service VARCHAR(8) NOT NULL,
                source_service_config_id INTEGER NOT NULL,
                source_item_id VARCHAR(100) NOT NULL,
                media_type VARCHAR(6) NOT NULL,
                movie_id INTEGER,
                series_id INTEGER,
                season_id INTEGER,
                source_media_id VARCHAR(100),
                path_tail VARCHAR(1024),
                confidence SMALLINT NOT NULL,
                signals JSON,
                updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                CONSTRAINT uq_supplemental_media_match_source_item
                    UNIQUE (source_service_config_id, source_item_id, media_type),
                FOREIGN KEY(movie_id) REFERENCES movies (id),
                FOREIGN KEY(season_id) REFERENCES seasons (id),
                FOREIGN KEY(series_id) REFERENCES series (id),
                FOREIGN KEY(source_service_config_id)
                    REFERENCES service_configs (id) ON DELETE CASCADE
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO supplemental_media_matches_new (
                id, source_service, source_service_config_id, source_item_id,
                media_type, movie_id, series_id, season_id, source_media_id,
                path_tail, confidence, signals, updated_at
            )
            SELECT id, source_service, source_service_config_id, source_item_id,
                media_type, movie_id, series_id, season_id, source_media_id,
                path_tail, confidence, signals, updated_at
            FROM supplemental_media_matches
            """
        )
    )
    op.execute(sa.text("DROP TABLE supplemental_media_matches"))
    op.execute(
        sa.text(
            "ALTER TABLE supplemental_media_matches_new RENAME TO supplemental_media_matches"
        )
    )

    op.create_index(
        "ix_supplemental_media_matches_source_service",
        "supplemental_media_matches",
        ["source_service"],
    )
    op.create_index(
        "ix_supplemental_media_matches_source_service_config_id",
        "supplemental_media_matches",
        ["source_service_config_id"],
    )
    op.create_index(
        "ix_supplemental_media_matches_media_type",
        "supplemental_media_matches",
        ["media_type"],
    )
    op.create_index(
        "ix_supplemental_media_matches_movie_id",
        "supplemental_media_matches",
        ["movie_id"],
    )
    op.create_index(
        "ix_supplemental_media_matches_series_id",
        "supplemental_media_matches",
        ["series_id"],
    )
    op.create_index(
        "ix_supplemental_media_matches_season_id",
        "supplemental_media_matches",
        ["season_id"],
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS supplemental_media_matches_new"))
    op.execute(
        sa.text(
            """
            CREATE TABLE supplemental_media_matches_new (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                source_service VARCHAR(8) NOT NULL,
                source_item_id VARCHAR(100) NOT NULL,
                media_type VARCHAR(6) NOT NULL,
                movie_id INTEGER,
                series_id INTEGER,
                season_id INTEGER,
                source_media_id VARCHAR(100),
                path_tail VARCHAR(1024),
                confidence SMALLINT NOT NULL,
                signals JSON,
                updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                CONSTRAINT uq_supplemental_media_match_source_item
                    UNIQUE (source_service, source_item_id, media_type),
                FOREIGN KEY(movie_id) REFERENCES movies (id),
                FOREIGN KEY(season_id) REFERENCES seasons (id),
                FOREIGN KEY(series_id) REFERENCES series (id)
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO supplemental_media_matches_new (
                id, source_service, source_item_id, media_type, movie_id,
                series_id, season_id, source_media_id, path_tail, confidence,
                signals, updated_at
            )
            SELECT id, source_service, source_item_id, media_type, movie_id,
                series_id, season_id, source_media_id, path_tail, confidence,
                signals, updated_at
            FROM supplemental_media_matches
            """
        )
    )
    op.execute(sa.text("DROP TABLE supplemental_media_matches"))
    op.execute(
        sa.text(
            "ALTER TABLE supplemental_media_matches_new RENAME TO supplemental_media_matches"
        )
    )
    op.create_index(
        "ix_supplemental_media_matches_source_service",
        "supplemental_media_matches",
        ["source_service"],
    )
    op.create_index(
        "ix_supplemental_media_matches_media_type",
        "supplemental_media_matches",
        ["media_type"],
    )
    op.create_index(
        "ix_supplemental_media_matches_movie_id",
        "supplemental_media_matches",
        ["movie_id"],
    )
    op.create_index(
        "ix_supplemental_media_matches_series_id",
        "supplemental_media_matches",
        ["series_id"],
    )
    op.create_index(
        "ix_supplemental_media_matches_season_id",
        "supplemental_media_matches",
        ["season_id"],
    )
