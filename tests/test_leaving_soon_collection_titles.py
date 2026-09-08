from __future__ import annotations

import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text

from backend.models.settings import GeneralSettingsResponse
from backend.utils.helpers import (
    DEFAULT_LEAVING_SOON_MOVIE_TITLE,
    DEFAULT_LEAVING_SOON_SERIES_TITLE,
    LeavingSoonTitles,
    normalize_leaving_soon_titles,
)

MIGRATION = importlib.import_module(
    "backend.alembic.versions.d2b6f4a8c135_split_leaving_soon_collection_titles"
)


class TestCollectionTitleValidation:
    def test_blank_titles_fall_back_to_defaults(self) -> None:
        settings = GeneralSettingsResponse(
            leaving_soon_movie_collection_title="   ",
            leaving_soon_series_collection_title="",
        )

        assert (
            settings.leaving_soon_movie_collection_title
            == DEFAULT_LEAVING_SOON_MOVIE_TITLE
        )
        assert (
            settings.leaving_soon_series_collection_title
            == DEFAULT_LEAVING_SOON_SERIES_TITLE
        )

    def test_custom_titles_are_kept_verbatim_after_stripping(self) -> None:
        settings = GeneralSettingsResponse(
            leaving_soon_movie_collection_title="  Expiring Films  ",
            leaving_soon_series_collection_title="Last Chance TV",
        )

        assert settings.leaving_soon_movie_collection_title == "Expiring Films"
        assert settings.leaving_soon_series_collection_title == "Last Chance TV"

    @pytest.mark.parametrize(
        "movie_title,series_title",
        [
            ("Last Chance", "Last Chance"),
            ("Last Chance", "last chance"),
            ("  Last Chance ", "LAST CHANCE"),
        ],
    )
    def test_identical_titles_are_rejected(
        self, movie_title: str, series_title: str
    ) -> None:
        """Jellyfin and Emby collections are global, so a shared name would
        resolve to one BoxSet and each sync half would strip the other's items.
        """
        with pytest.raises(ValidationError) as excinfo:
            GeneralSettingsResponse(
                leaving_soon_movie_collection_title=movie_title,
                leaving_soon_series_collection_title=series_title,
            )

        assert "must be different" in str(excinfo.value)

    def test_titles_over_the_column_limit_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GeneralSettingsResponse(
                leaving_soon_movie_collection_title="x" * 256,
            )


class TestLastSuccessTitleNormalization:
    def test_legacy_base_title_string_expands_to_the_suffixed_pair(self) -> None:
        assert normalize_leaving_soon_titles("Old Soon") == LeavingSoonTitles(
            movies="Old Soon [Movies]",
            series="Old Soon [Series]",
        )

    def test_current_mapping_shape_round_trips(self) -> None:
        titles = LeavingSoonTitles(movies="Expiring Films", series="Last Chance TV")

        assert normalize_leaving_soon_titles(titles.as_dict()) == titles

    def test_missing_halves_fall_back_to_defaults(self) -> None:
        assert normalize_leaving_soon_titles({}) == LeavingSoonTitles(
            movies=DEFAULT_LEAVING_SOON_MOVIE_TITLE,
            series=DEFAULT_LEAVING_SOON_SERIES_TITLE,
        )


def test_migration_seeds_split_titles_from_the_base_title(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'settings.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE general_settings ("
                "id INTEGER PRIMARY KEY, "
                "leaving_soon_enabled BOOLEAN NOT NULL DEFAULT 0, "
                "leaving_soon_collection_title VARCHAR(255) "
                "  NOT NULL DEFAULT 'Leaving Soon')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO general_settings "
                "(id, leaving_soon_enabled, leaving_soon_collection_title) "
                "VALUES (1, 1, 'Old Soon'), (2, 0, '  ')"
            )
        )
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(MIGRATION, "op", operations)

        MIGRATION.upgrade()

        rows = connection.execute(
            text(
                "SELECT id, leaving_soon_movie_collection_title, "
                "leaving_soon_series_collection_title "
                "FROM general_settings ORDER BY id"
            )
        ).all()
        # an existing rename keeps the names the clients used to build
        assert rows[0] == (1, "Old Soon [Movies]", "Old Soon [Series]")
        # a blank base title falls back to the default rather than " [Movies]"
        assert rows[1] == (2, "Leaving Soon [Movies]", "Leaving Soon [Series]")

        columns = {
            column["name"] for column in inspect(connection).get_columns(
                "general_settings"
            )
        }
        assert "leaving_soon_collection_title" not in columns

        MIGRATION.downgrade()

        assert (
            connection.execute(
                text(
                    "SELECT leaving_soon_collection_title "
                    "FROM general_settings WHERE id = 1"
                )
            ).scalar_one()
            == "Old Soon"
        )
        columns = {
            column["name"] for column in inspect(connection).get_columns(
                "general_settings"
            )
        }
        assert "leaving_soon_movie_collection_title" not in columns
    engine.dispose()
