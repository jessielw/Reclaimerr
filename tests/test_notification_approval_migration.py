from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

MIGRATION = importlib.import_module(
    "backend.alembic.versions.a3c8e1f5b920_add_notification_approved_url_hash"
)


def test_approved_url_hash_migration_keeps_rows_unapproved_and_round_trips(
    monkeypatch, tmp_path
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'notifications.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE notification_settings ("
                "id INTEGER PRIMARY KEY, url VARCHAR(500))"
            )
        )
        connection.execute(
            text("INSERT INTO notification_settings (id, url) VALUES (1, 'json://h/x')")
        )

        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(MIGRATION, "op", operations)

        MIGRATION.upgrade()
        columns = {
            c["name"] for c in inspect(connection).get_columns("notification_settings")
        }
        assert "approved_url_hash" in columns
        # existing rows are not approved, so they need review like any new one
        assert (
            connection.execute(
                text("SELECT approved_url_hash FROM notification_settings")
            ).scalar()
            is None
        )

        MIGRATION.downgrade()
        columns = {
            c["name"] for c in inspect(connection).get_columns("notification_settings")
        }
        assert "approved_url_hash" not in columns
