"""repair rows orphaned while foreign keys went unenforced

PRAGMA foreign_keys is connection state, and it used to be set per request, so
whether an ON DELETE action fired came down to which pooled connection a
background task happened to borrow. Every hard delete of a season, episode, or
movie version that ran on a connection without it left its children behind:
episodes whose season is gone, candidates and protections scoped to media that
no longer exists, playback events pointing at nothing.

They were mostly inert - nothing joins to a parent that is missing - but
enforcement is now on for every connection, and SQLite re-validates a row's
foreign keys on UPDATE. A dangling row that was merely dead weight becomes a
row the app cannot write to, so it has to be cleaned up before enforcement is
turned on.

Each orphan is resolved the way the runtime cleanup already resolves it when it
gets there first - `detach_movie_version_references` and the season/episode
paths in cleanup.py and sync.py: a candidate or protection covers one piece of
media and dies with it, a request still awaiting a decision goes the same way,
and a decided one is detached because it is history worth keeping. Playback
events are detached rather than dropped: the play happened, only the media it
resolved to is gone.

Deliberately not nulled: a scope column is what tells a candidate, protection,
or request apart from a wider one. `reclaim_candidates.episode_id` empty means
the whole season, `movie_version_id` empty means the whole movie -- so nulling
a dangling scope would quietly widen what the row targets rather than retire it.

Revision ID: b7f0c3e5a294
Revises: f2c8b4d7e0a3
Create Date: 2026-09-04 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7f0c3e5a294"
down_revision: str | Sequence[str] | None = "f2c8b4d7e0a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# scope column -> the table whose ids it must be found in
_SCOPES = (
    ("season_id", "seasons"),
    ("episode_id", "episodes"),
    ("movie_version_id", "movie_versions"),
)

# rows that exist to cover one piece of media, and mean nothing without it
_DROP_TABLES = (
    "reclaim_candidates",
    "protected_media",
    "supplemental_media_matches",
)

# pending is dropped, decided is kept as detached history
_REQUEST_TABLES = ("protection_requests", "delete_requests")

# the event is a real play; only its resolved media ids are stale
_DETACH_TABLES = ("playback_history_events",)


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    bind = op.get_bind()

    # First: an episode whose season is gone orphans its own children in turn,
    # and the episode_id sweep below has to see them.
    bind.execute(
        sa.text("DELETE FROM episodes WHERE season_id NOT IN (SELECT id FROM seasons)")
    )

    for column, parent in _SCOPES:
        orphaned = f"{column} IS NOT NULL AND {column} NOT IN (SELECT id FROM {parent})"

        for table in _DROP_TABLES + _REQUEST_TABLES + _DETACH_TABLES:
            # supplemental matches carry no movie_version_id, playback events no
            # movie_version_id either - skip the pairs that do not exist
            if column not in _columns(table):
                continue

            if table in _DROP_TABLES:
                bind.execute(sa.text(f"DELETE FROM {table} WHERE {orphaned}"))
            elif table in _DETACH_TABLES:
                bind.execute(
                    sa.text(f"UPDATE {table} SET {column} = NULL WHERE {orphaned}")
                )
            else:
                bind.execute(
                    sa.text(
                        f"DELETE FROM {table} WHERE {orphaned} AND status = 'PENDING'"
                    )
                )
                bind.execute(
                    sa.text(
                        f"UPDATE {table} SET {column} = NULL "
                        f"WHERE {orphaned} AND status != 'PENDING'"
                    )
                )


def downgrade() -> None:
    # The rows dropped here referenced media that no longer exists; there is
    # nothing to restore them from and nothing that wants them back.
    pass
