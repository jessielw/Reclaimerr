from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

MIGRATION = importlib.import_module(
    "backend.alembic.versions.e7b1d4a9c3f5_repair_orphaned_protection_scopes"
)

_SCHEMA = (
    "CREATE TABLE movie_versions (id INTEGER PRIMARY KEY, movie_id INTEGER NOT NULL)",
    "CREATE TABLE protected_media ("
    "id INTEGER PRIMARY KEY, media_type VARCHAR(6) NOT NULL, movie_id INTEGER, "
    "movie_version_id INTEGER, series_id INTEGER, season_id INTEGER, "
    "episode_id INTEGER, source VARCHAR(16) NOT NULL DEFAULT 'manual', "
    "permanent BOOLEAN NOT NULL DEFAULT 1, expires_at DATETIME)",
    "CREATE TABLE reclaim_candidates (id INTEGER PRIMARY KEY, movie_version_id INTEGER)",
    "CREATE TABLE protection_requests ("
    "id INTEGER PRIMARY KEY, movie_version_id INTEGER, status VARCHAR(16) NOT NULL)",
    "CREATE TABLE delete_requests ("
    "id INTEGER PRIMARY KEY, movie_version_id INTEGER, status VARCHAR(16) NOT NULL)",
)


def _seed(connection) -> None:
    for statement in _SCHEMA:
        connection.execute(text(statement))
    # version 7002 is deliberately absent: sync pruned it when the file was replaced
    connection.execute(
        text("INSERT INTO movie_versions (id, movie_id) VALUES (7001, 9001), (7003, 9002)")
    )
    rows = (
        # (id, movie_id, movie_version_id, source, permanent, expires_at)
        (5001, 9001, 7002, "manual", 1, None),  # dangling: the replaced file
        (5002, 9001, 7001, "manual", 0, "2020-01-01"),  # temporary duplicate
        (5003, 9001, 7001, "manual", 1, None),  # permanent duplicate - should win
        (5005, 9002, 7003, "manual", 1, None),  # healthy, unrelated
        (5006, 9002, 7002, "rule", 1, None),  # dangling rule row
    )
    for row in rows:
        connection.execute(
            text(
                "INSERT INTO protected_media "
                "(id, media_type, movie_id, movie_version_id, source, permanent, expires_at) "
                "VALUES (:id, 'MOVIE', :movie_id, :version, :source, :permanent, :expires)"
            ),
            {
                "id": row[0],
                "movie_id": row[1],
                "version": row[2],
                "source": row[3],
                "permanent": row[4],
                "expires": row[5],
            },
        )
    connection.execute(
        text("INSERT INTO reclaim_candidates (id, movie_version_id) VALUES (1, 7002), (2, 7001)")
    )
    for table in ("protection_requests", "delete_requests"):
        connection.execute(
            text(
                f"INSERT INTO {table} (id, movie_version_id, status) "
                "VALUES (1, 7002, 'PENDING'), (2, 7002, 'APPROVED'), (3, 7001, 'PENDING')"
            )
        )


def _run_upgrade(connection, monkeypatch) -> None:
    operations = Operations(MigrationContext.configure(connection))
    monkeypatch.setattr(MIGRATION, "op", operations)
    MIGRATION.upgrade()


def test_migration_drops_orphans_and_collapses_duplicates(monkeypatch, tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'repair.db'}")
    with engine.begin() as connection:
        _seed(connection)
        _run_upgrade(connection, monkeypatch)

        surviving = [
            row[0]
            for row in connection.execute(
                text("SELECT id FROM protected_media ORDER BY id")
            )
        ]
        # 5001 and 5006 pointed at a file that no longer exists; 5002 lost the
        # collapse to 5003, which protects more
        assert surviving == [5003, 5005]

        dangling = connection.execute(
            text(
                "SELECT COUNT(*) FROM protected_media WHERE movie_version_id IS NOT NULL "
                "AND movie_version_id NOT IN (SELECT id FROM movie_versions)"
            )
        ).scalar_one()
        assert dangling == 0

        candidates = [
            row[0]
            for row in connection.execute(
                text("SELECT id FROM reclaim_candidates ORDER BY id")
            )
        ]
        assert candidates == [2]

        for table in ("protection_requests", "delete_requests"):
            remaining = list(
                connection.execute(
                    text(f"SELECT id, movie_version_id FROM {table} ORDER BY id")
                )
            )
            # the pending request for the pruned file is gone, the decided one is
            # kept as history but detached, and the unrelated one is untouched
            assert remaining == [(2, None), (3, 7001)]

    engine.dispose()


def test_migration_subsumes_version_rows_under_a_whole_movie_protection(
    monkeypatch, tmp_path
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'subsume.db'}")
    with engine.begin() as connection:
        for statement in _SCHEMA:
            connection.execute(text(statement))
        connection.execute(
            text("INSERT INTO movie_versions (id, movie_id) VALUES (7001, 9001)")
        )
        connection.execute(
            text(
                "INSERT INTO protected_media "
                "(id, media_type, movie_id, movie_version_id, source, permanent) VALUES "
                "(1, 'MOVIE', 9001, 7001, 'manual', 1), "
                "(2, 'MOVIE', 9001, NULL, 'manual', 1)"
            )
        )
        _run_upgrade(connection, monkeypatch)

        surviving = [
            row[0]
            for row in connection.execute(
                text("SELECT id FROM protected_media ORDER BY id")
            )
        ]
        # the whole-movie row already covers the version-scoped one
        assert surviving == [2]

    engine.dispose()
