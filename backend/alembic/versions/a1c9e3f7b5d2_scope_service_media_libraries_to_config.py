"""scope service media libraries to the media server config they came from

Adds service_config_id to service_media_libraries and keys uniqueness on
(service_config_id, library_id). Library rows only ever come from the main
media server, but nothing recorded *which* server that was, so nothing could
name it in the UI. It also left a real hole: Jellyfin and Emby derive a
library's VirtualFolders ItemId from its path, so two servers each holding a
library at the same path report the same id. Promoting one over the other
updated the row in place and silently retargeted every rule scoped to it,
while the stale-library notice stayed quiet because the id still existed.

Revision ID: a1c9e3f7b5d2
Revises: d7f3b2a9c604
Create Date: 2026-08-27 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c9e3f7b5d2"
down_revision: str | None = "d7f3b2a9c604"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def _indexes(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA index_list({table})"))}


def upgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS service_media_libraries_new"))

    if "service_config_id" not in _cols("service_media_libraries"):
        with op.batch_alter_table("service_media_libraries", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("service_config_id", sa.Integer(), nullable=True)
            )

    # Backfill: only the main media server has ever contributed library rows, so
    # every existing row belongs to whichever config is main today.
    op.execute(
        sa.text(
            """
            UPDATE service_media_libraries
            SET service_config_id = (
                SELECT id FROM service_configs WHERE is_main = 1 LIMIT 1
            )
            WHERE service_config_id IS NULL
            """
        )
    )

    # Deliberately nullable, and orphans are kept rather than deleted: an install
    # that has not designated a main server yet has no config to point at, and
    # the next library sync adopts or removes the row either way.

    # Rebuild explicitly so SQLite gains the unique constraint (the table had
    # none at all) and the FK.
    op.execute(
        sa.text(
            """
            CREATE TABLE service_media_libraries_new (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                service_config_id INTEGER,
                library_id VARCHAR(50) NOT NULL,
                library_name VARCHAR(255) NOT NULL,
                media_type VARCHAR(6) NOT NULL,
                selected BOOLEAN NOT NULL,
                added_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                CONSTRAINT uq_service_media_library_config
                    UNIQUE (service_config_id, library_id),
                FOREIGN KEY(service_config_id)
                    REFERENCES service_configs (id) ON DELETE CASCADE
            )
            """
        )
    )
    # A pre-existing install cannot hold duplicates - the old sync keyed its diff
    # on library_id alone - but GROUP BY keeps the rebuild safe if one slipped in.
    op.execute(
        sa.text(
            """
            INSERT INTO service_media_libraries_new (
                id, service_config_id, library_id, library_name, media_type,
                selected, added_at, updated_at
            )
            SELECT MIN(id), service_config_id, library_id, library_name,
                media_type, selected, added_at, updated_at
            FROM service_media_libraries
            GROUP BY service_config_id, library_id
            """
        )
    )
    op.execute(sa.text("DROP TABLE service_media_libraries"))
    op.execute(
        sa.text(
            "ALTER TABLE service_media_libraries_new "
            "RENAME TO service_media_libraries"
        )
    )

    if "ix_service_media_libraries_service_config_id" not in _indexes(
        "service_media_libraries"
    ):
        op.create_index(
            "ix_service_media_libraries_service_config_id",
            "service_media_libraries",
            ["service_config_id"],
        )


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS service_media_libraries_old"))

    op.execute(
        sa.text(
            """
            CREATE TABLE service_media_libraries_old (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                library_id VARCHAR(50) NOT NULL,
                library_name VARCHAR(255) NOT NULL,
                media_type VARCHAR(6) NOT NULL,
                selected BOOLEAN NOT NULL,
                added_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL
            )
            """
        )
    )
    # The old shape has no room for a server, so collapse back to one row per
    # library id rather than dropping the losers on a unique violation.
    op.execute(
        sa.text(
            """
            INSERT INTO service_media_libraries_old (
                id, library_id, library_name, media_type, selected,
                added_at, updated_at
            )
            SELECT MIN(id), library_id, library_name, media_type, selected,
                added_at, updated_at
            FROM service_media_libraries
            GROUP BY library_id
            """
        )
    )
    op.execute(sa.text("DROP TABLE service_media_libraries"))
    op.execute(
        sa.text(
            "ALTER TABLE service_media_libraries_old "
            "RENAME TO service_media_libraries"
        )
    )
