from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

MIGRATION = importlib.import_module(
    "backend.alembic.versions.b2d4f6a8c0e1_add_background_job_priority"
)


def test_priority_migration_backfills_existing_jobs_and_creates_index(
    monkeypatch,
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE background_jobs ("
                "id INTEGER PRIMARY KEY, "
                "job_type VARCHAR(32) NOT NULL, "
                "payload JSON NOT NULL, "
                "status VARCHAR(16) NOT NULL DEFAULT 'PENDING', "
                "scheduled_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO background_jobs (id, job_type, payload) "
                "VALUES (1, 'TASK_RUN', '{}')"
            )
        )
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(MIGRATION, "op", operations)

        MIGRATION.upgrade()

        assert (
            connection.execute(
                text("SELECT priority FROM background_jobs WHERE id = 1")
            ).scalar_one()
            == "NORMAL"
        )
        assert "ix_background_jobs_claimable" in {
            index["name"]
            for index in inspect(connection).get_indexes("background_jobs")
        }

        MIGRATION.downgrade()

        assert "priority" not in {
            column["name"]
            for column in inspect(connection).get_columns("background_jobs")
        }
    engine.dispose()
