"""repair orphaned protection scopes and duplicate protections

Revision ID: e7b1d4a9c3f5
Revises: c9a3e5b7d1f4
Create Date: 2026-08-31 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7b1d4a9c3f5"
down_revision: str | Sequence[str] | None = "c9a3e5b7d1f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# tables whose movie_version_id may point at a movie_versions row that sync
# already deleted. ON DELETE SET NULL never fired for them because
# PRAGMA foreign_keys is only enabled on request-scoped sessions, so background
# syncs pruned the version row and left these behind.
_ORPHAN_DELETE_TABLES = ("protected_media", "reclaim_candidates")
_ORPHAN_REQUEST_TABLES = ("protection_requests", "delete_requests")

_SCOPE_COLUMNS = (
    "media_type",
    "movie_id",
    "movie_version_id",
    "series_id",
    "season_id",
    "episode_id",
)


def upgrade() -> None:
    bind = op.get_bind()

    orphaned = (
        "movie_version_id IS NOT NULL "
        "AND movie_version_id NOT IN (SELECT id FROM movie_versions)"
    )

    # a protection or candidate scoped to a file that no longer exists protects
    # or targets nothing, and only shows up as a phantom duplicate beside the
    # replacement file's own row
    for table in _ORPHAN_DELETE_TABLES:
        bind.execute(sa.text(f"DELETE FROM {table} WHERE {orphaned}"))

    # requests still awaiting a decision are equally meaningless; decided ones
    # are history worth keeping, so just detach them
    for table in _ORPHAN_REQUEST_TABLES:
        bind.execute(
            sa.text(f"DELETE FROM {table} WHERE {orphaned} AND status = 'PENDING'")
        )
        bind.execute(
            sa.text(
                f"UPDATE {table} SET movie_version_id = NULL "
                f"WHERE {orphaned} AND status != 'PENDING'"
            )
        )

    # collapse protections that already duplicate each other on the same scope,
    # keeping the one that protects most: permanent beats temporary, a later
    # expiry beats an earlier one, and the oldest row wins the remaining ties.
    # NULL never equals NULL in SQL, so every nullable scope column is coalesced
    # to a sentinel before partitioning.
    partition = ", ".join(
        column if column == "media_type" else f"COALESCE({column}, -1)"
        for column in _SCOPE_COLUMNS
    )
    # rule-managed rows are owned by the cleanup scan, which reconciles and
    # de-duplicates them on every run - leave them out of this
    bind.execute(
        sa.text(
            "DELETE FROM protected_media WHERE source <> 'rule' AND id NOT IN ("
            "SELECT id FROM (SELECT id, ROW_NUMBER() OVER ("
            f"PARTITION BY {partition} "
            "ORDER BY permanent DESC, expires_at DESC, id ASC) AS rn "
            "FROM protected_media WHERE source <> 'rule') WHERE rn = 1)"
        )
    )

    # a version-scoped protection is fully covered by a whole-movie protection of
    # the same movie, which is the second way the same title showed up twice
    bind.execute(
        sa.text(
            "DELETE FROM protected_media WHERE source <> 'rule' "
            "AND movie_version_id IS NOT NULL AND EXISTS ("
            "SELECT 1 FROM protected_media AS whole "
            "WHERE whole.source <> 'rule' "
            "AND whole.movie_id = protected_media.movie_id "
            "AND whole.movie_version_id IS NULL)"
        )
    )


def downgrade() -> None:
    # data repair only - the deleted rows referenced media that no longer exists
    pass
