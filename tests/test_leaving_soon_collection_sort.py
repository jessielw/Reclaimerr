from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from backend.enums import LeavingSoonCollectionSort
from backend.models.settings import GeneralSettingsResponse
from backend.utils.helpers import (
    normalize_leaving_soon_collection_sort,
    order_leaving_soon_item_ids,
)

MIGRATION = importlib.import_module(
    "backend.alembic.versions.c4a7e91d3b58_add_leaving_soon_collection_sort"
)

_NOW = datetime(2026, 9, 7, tzinfo=UTC)


class TestCollectionSortNormalization:
    @pytest.mark.parametrize("value", ["", "   ", None, "nonsense", 7])
    def test_unknown_values_fall_back_to_default(self, value: object) -> None:
        assert (
            normalize_leaving_soon_collection_sort(value)
            is LeavingSoonCollectionSort.DEFAULT
        )

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("default", LeavingSoonCollectionSort.DEFAULT),
            ("alpha", LeavingSoonCollectionSort.ALPHA),
            ("leaving_soonest", LeavingSoonCollectionSort.LEAVING_SOONEST),
            ("  ALPHA  ", LeavingSoonCollectionSort.ALPHA),
            (LeavingSoonCollectionSort.ALPHA, LeavingSoonCollectionSort.ALPHA),
        ],
    )
    def test_known_values_round_trip(
        self, value: object, expected: LeavingSoonCollectionSort
    ) -> None:
        assert normalize_leaving_soon_collection_sort(value) is expected

    def test_settings_model_defaults_to_leaving_the_server_alone(self) -> None:
        settings = GeneralSettingsResponse()

        assert (
            settings.leaving_soon_collection_sort
            is LeavingSoonCollectionSort.DEFAULT
        )

    @pytest.mark.parametrize("value", ["from-a-newer-build", "", None])
    def test_settings_model_coerces_rather_than_rejecting(self, value: object) -> None:
        # a row this build cannot parse must not fail the whole settings read,
        # which is what reaching the enum unparsed would do
        settings = GeneralSettingsResponse(
            leaving_soon_collection_sort=value,  # type: ignore[arg-type]
        )

        assert (
            settings.leaving_soon_collection_sort
            is LeavingSoonCollectionSort.DEFAULT
        )

    def test_settings_model_accepts_a_known_sort(self) -> None:
        settings = GeneralSettingsResponse(
            leaving_soon_collection_sort="leaving_soonest",  # type: ignore[arg-type]
        )

        assert (
            settings.leaving_soon_collection_sort
            is LeavingSoonCollectionSort.LEAVING_SOONEST
        )


class TestItemOrdering:
    def test_non_custom_sorts_keep_the_lexicographic_order(self) -> None:
        item_ids = {"30", "4", "200"}
        deadlines = {"30": _NOW, "4": _NOW + timedelta(days=5)}

        for sort in (
            LeavingSoonCollectionSort.DEFAULT,
            LeavingSoonCollectionSort.ALPHA,
        ):
            assert order_leaving_soon_item_ids(
                item_ids, collection_sort=sort, item_deadlines=deadlines
            ) == ["200", "30", "4"]

    def test_leaving_soonest_orders_by_deadline(self) -> None:
        ordered = order_leaving_soon_item_ids(
            {"a", "b", "c"},
            collection_sort=LeavingSoonCollectionSort.LEAVING_SOONEST,
            item_deadlines={
                "a": _NOW + timedelta(days=10),
                "b": _NOW + timedelta(days=1),
                "c": _NOW + timedelta(days=5),
            },
        )

        assert ordered == ["b", "c", "a"]

    def test_items_without_a_deadline_sort_last_by_id(self) -> None:
        ordered = order_leaving_soon_item_ids(
            {"known", "zzz", "aaa"},
            collection_sort=LeavingSoonCollectionSort.LEAVING_SOONEST,
            item_deadlines={"known": _NOW + timedelta(days=3)},
        )

        assert ordered == ["known", "aaa", "zzz"]

    def test_ties_break_on_item_id_so_runs_are_repeatable(self) -> None:
        deadlines = {"b": _NOW, "a": _NOW, "c": _NOW}

        assert order_leaving_soon_item_ids(
            {"c", "a", "b"},
            collection_sort=LeavingSoonCollectionSort.LEAVING_SOONEST,
            item_deadlines=deadlines,
        ) == ["a", "b", "c"]

    def test_blank_and_duplicate_ids_are_dropped(self) -> None:
        assert order_leaving_soon_item_ids(
            ["  7 ", "7", "", "   ", "3"],
            collection_sort=LeavingSoonCollectionSort.LEAVING_SOONEST,
            item_deadlines={},
        ) == ["3", "7"]

    def test_missing_deadline_map_degrades_to_id_order(self) -> None:
        assert order_leaving_soon_item_ids(
            {"b", "a"},
            collection_sort=LeavingSoonCollectionSort.LEAVING_SOONEST,
            item_deadlines=None,
        ) == ["a", "b"]


def test_migration_adds_the_column_defaulted_to_untouched(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'settings.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE general_settings ("
                "id INTEGER PRIMARY KEY, "
                "leaving_soon_enabled BOOLEAN NOT NULL DEFAULT 0)"
            )
        )
        connection.execute(
            text("INSERT INTO general_settings (id, leaving_soon_enabled) VALUES (1, 1)")
        )
        operations = Operations(MigrationContext.configure(connection))
        import unittest.mock as mock

        with mock.patch.object(MIGRATION, "op", operations):
            MIGRATION.upgrade()

            columns = {
                column["name"]
                for column in inspect(connection).get_columns("general_settings")
            }
            assert "leaving_soon_collection_sort" in columns
            # an existing install must not have its collections reordered by
            # the upgrade itself
            row = connection.execute(
                text("SELECT leaving_soon_collection_sort FROM general_settings")
            ).scalar_one()
            assert row == "default"

            # re-running is a no-op rather than a duplicate-column error
            MIGRATION.upgrade()

            MIGRATION.downgrade()
            columns = {
                column["name"]
                for column in inspect(connection).get_columns("general_settings")
            }
            assert "leaving_soon_collection_sort" not in columns
