from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

MIGRATION = importlib.import_module(
    "backend.alembic.versions.c3f7b1d09a24_clear_unrated_tmdb_ratings"
)

_CREATE = (
    "CREATE TABLE {table} ("
    "id INTEGER PRIMARY KEY, "
    "vote_average FLOAT, "
    "vote_count INTEGER)"
)

# id 1 is the sentinel, id 2 is a real rating, id 3 has an unknown vote count,
# id 4 is already clear
_ROWS = (
    (1, 0.0, 0),
    (2, 7.5, 1200),
    (3, 0.0, None),
    (4, None, 0),
)


def test_migration_clears_only_the_zero_vote_sentinel(monkeypatch, tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'ratings.db'}")
    with engine.begin() as connection:
        for table in ("movies", "series"):
            connection.execute(text(_CREATE.format(table=table)))
            for row_id, rating, votes in _ROWS:
                connection.execute(
                    text(
                        f"INSERT INTO {table} (id, vote_average, vote_count) "
                        "VALUES (:id, :rating, :votes)"
                    ),
                    {"id": row_id, "rating": rating, "votes": votes},
                )

        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(MIGRATION, "op", operations)

        MIGRATION.upgrade()

        for table in ("movies", "series"):
            ratings = dict(
                connection.execute(
                    text(f"SELECT id, vote_average FROM {table}")
                ).all()
            )
            assert ratings[1] is None, f"{table}: sentinel row was not cleared"
            assert ratings[2] == 7.5, f"{table}: real rating was modified"
            assert ratings[3] == 0.0, f"{table}: unknown vote count was modified"
            assert ratings[4] is None, f"{table}: already-clear row changed"

        MIGRATION.downgrade()

        for table in ("movies", "series"):
            assert (
                connection.execute(
                    text(f"SELECT vote_average FROM {table} WHERE id = 1")
                ).scalar_one()
                is None
            ), f"{table}: downgrade should not restore the sentinel"

    engine.dispose()
