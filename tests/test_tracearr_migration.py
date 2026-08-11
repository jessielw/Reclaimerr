from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

MIGRATION = importlib.import_module(
    "backend.alembic.versions.e4a7c9d2f6b1_add_tracearr_playback_provider"
)


def test_tracearr_migration_backfills_observed_service_and_round_trips(
    monkeypatch,
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'tracearr.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE service_configs ("
                "id INTEGER PRIMARY KEY, service_type VARCHAR(16) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE playback_history_events ("
                "id INTEGER PRIMARY KEY, source_service VARCHAR(16) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE playback_history_user_aggregates ("
                "id INTEGER PRIMARY KEY, observed_service VARCHAR(16) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO playback_history_events (id, source_service) VALUES "
                "(1, 'TAUTULLI'), (2, 'JELLYFIN'), (3, 'EMBY')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO playback_history_user_aggregates "
                "(id, observed_service) VALUES (1, 'PLEX')"
            )
        )

        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(MIGRATION, "op", operations)

        MIGRATION.upgrade()

        assert "observed_service" in {
            column["name"]
            for column in inspect(connection).get_columns("playback_history_events")
        }
        observed_by_id = {
            int(row["id"]): str(row["observed_service"])
            for row in connection.execute(
                text("SELECT id, observed_service FROM playback_history_events")
            ).mappings()
        }
        assert observed_by_id == {1: "PLEX", 2: "JELLYFIN", 3: "EMBY"}
        assert "ix_playback_history_events_observed_service" in {
            index["name"]
            for index in inspect(connection).get_indexes("playback_history_events")
        }

        connection.execute(
            text(
                "INSERT INTO service_configs (id, service_type) VALUES (1, 'TRACEARR')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO playback_history_events "
                "(id, source_service, observed_service) "
                "VALUES (4, 'TRACEARR', 'PLEX')"
            )
        )

        MIGRATION.downgrade()

        assert "observed_service" not in {
            column["name"]
            for column in inspect(connection).get_columns("playback_history_events")
        }
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM service_configs")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM playback_history_events")
            ).scalar_one()
            == 3
        )

    engine.dispose()
