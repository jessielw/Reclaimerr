"""record a linked server's episode ids on supplemental media matches

There is one episode id column per service *type* (``plex_rating_key``,
``jellyfin_episode_id``, ``emby_episode_id``), and ``sync_linked_data`` only
lets a linked server write them when its type differs from the main server's --
otherwise it would clobber the ids media-server delete operations rely on. That
left a second Plex server's episode ids recorded nowhere, so every episode play
it reported had nothing to resolve through and was dropped.

Supplemental matches already carry a linked server's movie, series, and season
ids keyed to its own config; this adds the episode level.

Revision ID: c9a3e5b7d1f4
Revises: b8f4c2d6a9e3
Create Date: 2026-08-28 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9a3e5b7d1f4"
down_revision: str | None = "b8f4c2d6a9e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def _indexes(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA index_list({table})"))}


def upgrade() -> None:
    # Added with a raw ALTER rather than a batch rebuild: SQLite accepts a
    # REFERENCES clause on ADD COLUMN as long as the default is NULL.
    if "episode_id" not in _cols("supplemental_media_matches"):
        op.execute(
            sa.text(
                """
                ALTER TABLE supplemental_media_matches
                ADD COLUMN episode_id INTEGER REFERENCES episodes (id)
                """
            )
        )

    if "ix_supplemental_media_matches_episode_id" not in _indexes(
        "supplemental_media_matches"
    ):
        op.create_index(
            "ix_supplemental_media_matches_episode_id",
            "supplemental_media_matches",
            ["episode_id"],
        )

    # No backfill: episode matches are only known to the linked server that
    # reports them, and the next linked sync replaces that config's matches
    # wholesale (see _replace_supplemental_matches).


def downgrade() -> None:
    if "ix_supplemental_media_matches_episode_id" in _indexes(
        "supplemental_media_matches"
    ):
        op.drop_index(
            "ix_supplemental_media_matches_episode_id",
            table_name="supplemental_media_matches",
        )
    with op.batch_alter_table("supplemental_media_matches", schema=None) as batch_op:
        batch_op.drop_column("episode_id")
