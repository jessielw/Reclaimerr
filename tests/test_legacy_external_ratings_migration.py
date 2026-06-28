from __future__ import annotations

import importlib
import json
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, create_engine, text

MIGRATION = importlib.import_module(
    "backend.alembic.versions.5a8c2e7d9f10_repair_legacy_external_rating_tasks"
)


@pytest.fixture
def connection(tmp_path) -> Iterator[Connection]:
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE task_runs ("
                "id INTEGER PRIMARY KEY, task VARCHAR(64) NOT NULL, "
                "task_schedule_id INTEGER)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE task_schedules ("
                "id INTEGER PRIMARY KEY, task VARCHAR(64) NOT NULL, "
                "schedule_type VARCHAR(16) NOT NULL, "
                "schedule_value VARCHAR(100) NOT NULL, "
                "default_schedule_type VARCHAR(16) NOT NULL, "
                "default_schedule_value VARCHAR(100) NOT NULL, "
                "description TEXT, enabled BOOLEAN NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE background_jobs ("
                "id INTEGER PRIMARY KEY, job_type VARCHAR(32) NOT NULL, "
                "payload JSON NOT NULL, dedupe_key VARCHAR(120))"
            )
        )
        yield connection
    engine.dispose()


def _run_upgrade(connection: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MIGRATION.op, "get_bind", lambda: connection)
    MIGRATION.upgrade()


def test_upgrade_repairs_legacy_runs_jobs_and_schedules(
    connection: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection.execute(
        text(
            "INSERT INTO task_runs (id, task, task_schedule_id) "
            "VALUES (1, :task, 1), (2, :valid, 2)"
        ),
        {
            "task": "REFRESH_EXTERNAL_RATINGS",
            "valid": "MDBLIST_RATINGS_REFRESH",
        },
    )
    connection.execute(
        text(
            "INSERT INTO background_jobs (id, job_type, payload, dedupe_key) "
            "VALUES (1, 'TASK_RUN', :payload, :dedupe_key)"
        ),
        {
            "payload": json.dumps({"task": "refresh_external_ratings"}),
            "dedupe_key": "task-run-refresh_external_ratings",
        },
    )
    connection.execute(
        text(
            "INSERT INTO task_schedules "
            "(id, task, schedule_type, schedule_value, default_schedule_type, "
            "default_schedule_value, description, enabled) VALUES "
            "(1, :task, 'CRON', '15 4 * * *', 'CRON', '0 6 * * *', "
            "'Legacy schedule', 0), "
            "(2, :task, 'CRON', '30 4 * * *', 'CRON', '0 6 * * *', "
            "'Duplicate legacy schedule', 1)"
        ),
        {"task": "REFRESH_EXTERNAL_RATINGS"},
    )

    _run_upgrade(connection, monkeypatch)

    assert connection.execute(
        text("SELECT id, task, task_schedule_id FROM task_runs ORDER BY id")
    ).all() == [(2, "MDBLIST_RATINGS_REFRESH", 1)]
    assert connection.execute(
        text(
            "SELECT json_extract(payload, '$.task'), dedupe_key "
            "FROM background_jobs WHERE id = 1"
        )
    ).one() == (
        "mdblist_ratings_refresh",
        "task-run-mdblist_ratings_refresh",
    )
    assert connection.execute(
        text(
            "SELECT id, task, schedule_value, description, enabled "
            "FROM task_schedules ORDER BY id"
        )
    ).all() == [
        (
            1,
            "MDBLIST_RATINGS_REFRESH",
            "15 4 * * *",
            "Refreshes ratings from the configured MDBList provider",
            0,
        )
    ]


def test_upgrade_removes_legacy_schedule_when_replacement_exists(
    connection: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection.execute(
        text(
            "INSERT INTO task_schedules "
            "(id, task, schedule_type, schedule_value, default_schedule_type, "
            "default_schedule_value, description, enabled) VALUES "
            "(1, 'MDBLIST_RATINGS_REFRESH', 'CRON', '10 6 * * *', 'CRON', "
            "'0 6 * * *', 'Current schedule', 0), "
            "(2, 'REFRESH_EXTERNAL_RATINGS', 'CRON', '15 4 * * *', 'CRON', "
            "'0 6 * * *', 'Legacy schedule', 1)"
        )
    )
    connection.execute(
        text(
            "INSERT INTO task_runs (id, task, task_schedule_id) "
            "VALUES (1, 'MDBLIST_RATINGS_REFRESH', 2)"
        )
    )

    _run_upgrade(connection, monkeypatch)

    assert connection.execute(
        text("SELECT id, task, schedule_value, enabled FROM task_schedules")
    ).all() == [(1, "MDBLIST_RATINGS_REFRESH", "10 6 * * *", 0)]
    assert connection.execute(
        text("SELECT task_schedule_id FROM task_runs WHERE id = 1")
    ).scalar_one() == 1
