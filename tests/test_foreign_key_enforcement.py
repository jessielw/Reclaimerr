"""Foreign key enforcement is uniform, and the data it now enforces is clean.

`PRAGMA foreign_keys` is connection state. It used to be set per request, so a
background task ran with enforcement on or off depending on which pooled
connection it borrowed - ON DELETE actions fired or silently did not. It is now
set on every connection the pool opens, with migrations the one exception.

Turning it on retroactively matters for rows already orphaned while it was off:
SQLite re-validates a row's foreign keys on UPDATE, so a dangling row goes from
dead weight to a row the app cannot write to.
"""

from __future__ import annotations

import importlib
import sqlite3

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

import backend.database as database

MIGRATION = importlib.import_module(
    "backend.alembic.versions.b7f0c3e5a294_repair_orphans_before_fk_enforcement"
)


def test_pragma_hook_enforces_foreign_keys_outside_migrations() -> None:
    calls: list[str] = []

    class _Cursor:
        def execute(self, statement: str) -> None:
            calls.append(statement)

        def close(self) -> None:
            pass

    class _Conn:
        def cursor(self) -> _Cursor:
            return _Cursor()

    database.set_sqlite_pragma(_Conn(), None)
    assert "PRAGMA foreign_keys=ON" in calls


def test_pragma_hook_leaves_foreign_keys_off_for_migrations(monkeypatch) -> None:
    calls: list[str] = []

    class _Cursor:
        def execute(self, statement: str) -> None:
            calls.append(statement)

        def close(self) -> None:
            pass

    class _Conn:
        def cursor(self) -> _Cursor:
            return _Cursor()

    # a batch rebuild drops and renames tables other tables reference, which
    # enforcement refuses - and the pragma cannot be turned off once the
    # migration transaction is open
    monkeypatch.setattr(database, "_migrating", True)
    database.set_sqlite_pragma(_Conn(), None)
    assert "PRAGMA foreign_keys=ON" not in calls
    assert "PRAGMA journal_mode=WAL" in calls


_SCHEMA = (
    "CREATE TABLE seasons (id INTEGER PRIMARY KEY)",
    "CREATE TABLE movie_versions (id INTEGER PRIMARY KEY)",
    "CREATE TABLE episodes (id INTEGER PRIMARY KEY, season_id INTEGER NOT NULL)",
    "CREATE TABLE reclaim_candidates ("
    "id INTEGER PRIMARY KEY, season_id INTEGER, episode_id INTEGER, "
    "movie_version_id INTEGER)",
    "CREATE TABLE protected_media ("
    "id INTEGER PRIMARY KEY, season_id INTEGER, episode_id INTEGER, "
    "movie_version_id INTEGER)",
    "CREATE TABLE supplemental_media_matches ("
    "id INTEGER PRIMARY KEY, season_id INTEGER, episode_id INTEGER)",
    "CREATE TABLE protection_requests ("
    "id INTEGER PRIMARY KEY, season_id INTEGER, episode_id INTEGER, "
    "movie_version_id INTEGER, status VARCHAR(16) NOT NULL)",
    "CREATE TABLE delete_requests ("
    "id INTEGER PRIMARY KEY, season_id INTEGER, episode_id INTEGER, "
    "movie_version_id INTEGER, status VARCHAR(16) NOT NULL)",
    "CREATE TABLE playback_history_events ("
    "id INTEGER PRIMARY KEY, season_id INTEGER, episode_id INTEGER)",
)


def _seed(connection) -> None:
    for statement in _SCHEMA:
        connection.execute(text(statement))
    # season 20 and movie version 40 were hard-deleted with foreign keys off
    connection.execute(text("INSERT INTO seasons (id) VALUES (21)"))
    connection.execute(
        text("INSERT INTO episodes (id, season_id) VALUES (30, 20), (31, 21)")
    )
    connection.execute(
        text(
            "INSERT INTO reclaim_candidates "
            "(id, season_id, episode_id, movie_version_id) VALUES "
            "(1, 20, NULL, NULL), "  # season is gone
            "(2, 21, 30, NULL), "  # episode dies with its orphaned season
            "(3, NULL, NULL, 40), "  # version is gone
            "(4, 21, NULL, NULL)"  # healthy
        )
    )
    connection.execute(
        text("INSERT INTO protected_media (id, season_id) VALUES (1, 20), (2, 21)")
    )
    connection.execute(
        text("INSERT INTO supplemental_media_matches (id, season_id) VALUES (1, 20)")
    )
    for table in ("protection_requests", "delete_requests"):
        connection.execute(
            text(
                f"INSERT INTO {table} (id, season_id, status) VALUES "
                "(1, 20, 'PENDING'), (2, 20, 'APPROVED'), (3, 21, 'PENDING')"
            )
        )
    connection.execute(
        text(
            "INSERT INTO playback_history_events (id, season_id, episode_id) "
            "VALUES (1, 20, 30)"
        )
    )


def test_migration_retires_orphans_without_widening_their_scope(
    monkeypatch, tmp_path
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'orphans.db'}")
    with engine.begin() as connection:
        _seed(connection)

        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(MIGRATION, "op", operations)
        MIGRATION.upgrade()

        def rows(statement: str) -> list[tuple]:
            return [tuple(row) for row in connection.execute(text(statement))]

        # the episode whose season is gone goes too, so its own children resolve
        assert rows("SELECT id, season_id FROM episodes ORDER BY id") == [(31, 21)]

        # candidates 1-3 all lost their scope; nulling instead of dropping would
        # have promoted 2 to the whole season and 3 to the whole movie
        assert rows(
            "SELECT id, season_id, episode_id, movie_version_id "
            "FROM reclaim_candidates ORDER BY id"
        ) == [(4, 21, None, None)]

        assert rows("SELECT id FROM protected_media ORDER BY id") == [(2,)]
        assert rows("SELECT id FROM supplemental_media_matches") == []

        for table in ("protection_requests", "delete_requests"):
            # pending is dropped, decided is kept detached, healthy is untouched
            assert rows(f"SELECT id, season_id FROM {table} ORDER BY id") == [
                (2, None),
                (3, 21),
            ]

        # the play happened - only the media it resolved to is gone
        assert rows(
            "SELECT id, season_id, episode_id FROM playback_history_events"
        ) == [(1, None, None)]

    engine.dispose()

    connection = sqlite3.connect(tmp_path / "orphans.db")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
    finally:
        connection.close()
