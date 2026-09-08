from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from niquests.exceptions import HTTPError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    GeneralSettings,
    Movie,
    MovieVersion,
    ReclaimCandidate,
    Series,
    SeriesServiceRef,
    ServiceConfig,
)
from backend.enums import LeavingSoonCollectionSort, MediaType, Service
from backend.services.emby_base import EmbyServiceBase
from backend.services.plex import PlexService
from backend.tasks import cleanup


class LeavingSoonAdapterPruneTests(unittest.IsolatedAsyncioTestCase):
    async def test_emby_prune_removes_only_matching_collection_items(self) -> None:
        class FakeEmby:
            def __init__(self) -> None:
                self.removals: list[tuple[str, set[str]]] = []

            async def _prune_leaving_soon_collection(self, **kwargs: Any) -> None:
                await EmbyServiceBase._prune_leaving_soon_collection(
                    self,  # type: ignore[arg-type]
                    **kwargs,
                )

            async def _find_collection_ids_by_title(self, title: str) -> list[str]:
                return ["collection-1"] if title.endswith("[Movies]") else []

            async def _get_collection_item_ids(
                self, *, collection_id: str, include_item_types: str
            ) -> set[str]:
                self.assert_request = (collection_id, include_item_types)
                return {"keep", "remove"}

            async def _remove_items_from_collection(
                self, *, collection_id: str, item_ids: set[str]
            ) -> None:
                self.removals.append((collection_id, set(item_ids)))

        fake = FakeEmby()
        await EmbyServiceBase.prune_leaving_soon_items(
            fake,  # type: ignore[arg-type]
            movie_title="Leaving Soon [Movies]",
            series_title="Leaving Soon [Series]",
            movie_item_ids={"remove", "not-present"},
            series_item_ids=set(),
        )

        self.assertEqual(fake.assert_request, ("collection-1", "Movie"))
        self.assertEqual(fake.removals, [("collection-1", {"remove"})])

    async def test_plex_prune_rebuilds_collection_without_removed_items(self) -> None:
        class FakePlex:
            def __init__(self) -> None:
                self.deleted: list[tuple[str, str]] = []
                self.created: list[dict[str, Any]] = []

            async def _prune_leaving_soon_collection_for_type(
                self, **kwargs: Any
            ) -> None:
                await PlexService._prune_leaving_soon_collection_for_type(
                    self,  # type: ignore[arg-type]
                    **kwargs,
                )

            async def _get_section_ids_by_type(self, section_type: str) -> list[str]:
                return ["movies"] if section_type == "movie" else []

            async def _find_collection_ids_by_title(
                self, *, section_id: str, collection_title: str
            ) -> list[str]:
                return ["collection-1"]

            async def _get_collection_child_ids_ordered(
                self, collection_id: str
            ) -> list[str]:
                return ["keep-b", "remove", "keep-a"]

            async def _delete_collection(
                self, *, section_id: str, collection_id: str
            ) -> None:
                self.deleted.append((section_id, collection_id))

            async def _create_collection(self, **kwargs: Any) -> str:
                self.created.append(kwargs)
                return "collection-2"

            async def _apply_collection_sort(self, **kwargs: Any) -> None:
                self.sorted_calls.append(kwargs)

        fake = FakePlex()
        fake.sorted_calls = []  # type: ignore[attr-defined]
        await PlexService.prune_leaving_soon_items(
            fake,  # type: ignore[arg-type]
            movie_title="Leaving Soon [Movies]",
            series_title="Leaving Soon [Series]",
            movie_item_ids={"remove"},
            series_item_ids=set(),
        )

        self.assertEqual(fake.deleted, [("movies", "collection-1")])
        # survivors keep the order Plex was already showing them in
        self.assertEqual(
            fake.created,
            [
                {
                    "section_id": "movies",
                    "section_type": "movie",
                    "collection_title": "Leaving Soon [Movies]",
                    "item_ids": ["keep-b", "keep-a"],
                }
            ],
        )
        self.assertEqual(
            fake.sorted_calls,  # type: ignore[attr-defined]
            [
                {
                    "collection_id": "collection-2",
                    "section_id": "movies",
                    "collection_title": "Leaving Soon [Movies]",
                    "collection_sort": LeavingSoonCollectionSort.DEFAULT,
                    "ordered_item_ids": ["keep-b", "keep-a"],
                }
            ],
        )


class LeavingSoonEmbyCollectionSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_collection_membership_lookup_paginates_past_1000_items(
        self,
    ) -> None:
        class FakeService:
            def __init__(self) -> None:
                self.start_indexes: list[int] = []

            async def _get_paginated_items(self, **kwargs: Any) -> list[dict[str, Any]]:
                return await EmbyServiceBase._get_paginated_items(
                    self,  # type: ignore[arg-type]
                    **kwargs,
                )

            async def _make_request(
                self,
                endpoint: str,
                params: dict[str, Any] | None = None,
                **_kwargs: Any,
            ) -> dict[str, Any]:
                self.assert_endpoint = endpoint
                assert params is not None
                start_index = int(params["StartIndex"])
                self.start_indexes.append(start_index)
                page_size = 1000 if start_index == 0 else 2
                return {
                    "Items": [
                        {"Id": f"item-{start_index + offset}"}
                        for offset in range(page_size)
                    ],
                    "TotalRecordCount": 1002,
                }

        fake = FakeService()
        item_ids = await EmbyServiceBase._get_collection_item_ids(
            fake,  # type: ignore[arg-type]
            collection_id="collection-1",
            include_item_types="Movie",
        )

        self.assertEqual(fake.assert_endpoint, "Items")
        self.assertEqual(fake.start_indexes, [0, 1000])
        self.assertEqual(len(item_ids), 1002)
        self.assertIn("item-1001", item_ids)

    async def test_create_and_add_collection_items_in_bounded_batches(self) -> None:
        class FakeResponse:
            def __init__(self, payload: dict[str, Any] | None = None) -> None:
                self.payload = payload or {}

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return self.payload

        class FakeSession:
            def __init__(self) -> None:
                self.posts: list[tuple[str, dict[str, Any]]] = []

            async def post(
                self,
                endpoint: str,
                *,
                params: dict[str, Any],
                **_kwargs: Any,
            ) -> FakeResponse:
                self.posts.append((endpoint, params))
                if endpoint.endswith("/Collections"):
                    return FakeResponse({"Id": "collection-1"})
                return FakeResponse()

        class FakeService:
            service_url = "http://jellyfin"
            service_type = Service.JELLYFIN

            def __init__(self) -> None:
                self.session = FakeSession()

            async def _add_items_to_collection(self, **kwargs: Any) -> None:
                await EmbyServiceBase._add_items_to_collection(
                    self,  # type: ignore[arg-type]
                    **kwargs,
                )

        fake = FakeService()
        await EmbyServiceBase._create_collection(
            fake,  # type: ignore[arg-type]
            collection_title="Leaving Soon [Movies]",
            item_ids={f"{item_id:032d}" for item_id in range(205)},
        )

        batch_sizes = [
            len(params["Ids"].split(",")) for _, params in fake.session.posts
        ]
        self.assertEqual(batch_sizes, [100, 100, 5])
        self.assertTrue(
            all(size <= 100 for size in batch_sizes),
        )

    async def test_sync_skips_missing_jellyfin_ids_and_adds_valid_items(self) -> None:
        class FakeService:
            service_type = Service.JELLYFIN

            def __init__(self) -> None:
                self.added: set[str] = set()

            async def _find_collection_ids_by_title(self, _title: str) -> list[str]:
                return ["collection-1"]

            async def _get_collection_item_ids(self, **_kwargs: Any) -> set[str]:
                return {"keep"}

            async def _filter_existing_collection_item_ids(
                self, **kwargs: Any
            ) -> set[str]:
                return await EmbyServiceBase._filter_existing_collection_item_ids(
                    self,  # type: ignore[arg-type]
                    **kwargs,
                )

            async def _make_request(
                self,
                endpoint: str,
                params: dict[str, Any] | None = None,
                **_kwargs: Any,
            ) -> dict[str, Any]:
                assert endpoint == "Items"
                assert params is not None
                requested_ids = set(str(params["Ids"]).split(","))
                return {
                    "Items": [
                        {"Id": item_id}
                        for item_id in requested_ids
                        if item_id != "missing"
                    ]
                }

            async def _add_items_to_collection(
                self, *, collection_id: str, item_ids: set[str]
            ) -> None:
                self.assert_collection_id = collection_id
                self.added.update(item_ids)

            async def _remove_items_from_collection(self, **_kwargs: Any) -> None:
                raise AssertionError("No collection items should be removed")

            async def _delete_collection(self, _collection_id: str) -> None:
                raise AssertionError("No collection should be deleted")

        fake = FakeService()
        with self.assertLogs("reclaimerr", level="WARNING") as logs:
            await EmbyServiceBase._sync_leaving_soon_collection(
                fake,  # type: ignore[arg-type]
                collection_title="Leaving Soon [Movies]",
                expected_item_ids={"keep", "new", "missing"},
                include_item_types="Movie",
            )

        self.assertEqual(fake.assert_collection_id, "collection-1")
        self.assertEqual(fake.added, {"new"})
        self.assertTrue(
            any("Skipping 1 missing jellyfin item(s)" in line for line in logs.output)
        )

    async def test_remove_batches_items_and_uses_404_compatibility_fallback(
        self,
    ) -> None:
        class FakeResponse:
            def __init__(self, *, status_code: int = 204) -> None:
                self.status_code = status_code
                self.text = ""
                self.request = None

            def __bool__(self) -> bool:
                return False

            def raise_for_status(self) -> None:
                if self.status_code == 404:
                    error = HTTPError("not found")
                    error.response = self
                    raise error

        class FakeSession:
            def __init__(self) -> None:
                self.deletes: list[dict[str, Any]] = []
                self.posts: list[dict[str, Any]] = []

            async def delete(
                self,
                _endpoint: str,
                *,
                params: dict[str, Any],
                **_kwargs: Any,
            ) -> FakeResponse:
                self.deletes.append(params)
                return FakeResponse(status_code=404)

            async def post(
                self,
                _endpoint: str,
                *,
                params: dict[str, Any],
                **_kwargs: Any,
            ) -> FakeResponse:
                self.posts.append(params)
                return FakeResponse()

        class FakeService:
            service_url = "http://jellyfin"
            service_type = Service.JELLYFIN

            def __init__(self) -> None:
                self.session = FakeSession()

        fake = FakeService()
        await EmbyServiceBase._remove_items_from_collection(
            fake,  # type: ignore[arg-type]
            collection_id="collection-1",
            item_ids={f"{item_id:032d}" for item_id in range(205)},
        )

        delete_batch_sizes = [
            len(params["Ids"].split(",")) for params in fake.session.deletes
        ]
        fallback_batch_sizes = [
            len(params["Ids"].split(",")) for params in fake.session.posts
        ]
        self.assertEqual(delete_batch_sizes, [100, 100, 5])
        self.assertEqual(fallback_batch_sizes, [100, 100, 5])

    async def test_add_failure_includes_http_context_instead_of_none(self) -> None:
        class FakeRequest:
            method = "POST"
            url = "http://jellyfin/Collections/collection-1/Items"

        class FakeResponse:
            status_code = 413
            text = '{"error":"request too large"}'
            request = FakeRequest()

            def __bool__(self) -> bool:
                return False

            def raise_for_status(self) -> None:
                raise RuntimeError("None")

        class FakeSession:
            async def post(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
                return FakeResponse()

        class FakeService:
            service_url = "http://jellyfin"
            service_type = Service.JELLYFIN
            session = FakeSession()

        with self.assertRaisesRegex(
            ValueError,
            "status=413.*method=POST.*Collections/collection-1/Items",
        ):
            await EmbyServiceBase._add_items_to_collection(
                FakeService(),  # type: ignore[arg-type]
                collection_id="collection-1",
                item_ids={"item-1"},
            )


class LeavingSoonActionGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_blocks_when_pre_prune_fails(self) -> None:
        prune = AsyncMock(side_effect=RuntimeError("jellyfin unavailable"))
        implementation = AsyncMock(return_value=(1, 0))
        reconcile = AsyncMock()

        with (
            patch.object(
                cleanup,
                "_prune_leaving_soon_before_candidate_actions",
                prune,
            ),
            patch.object(cleanup, "_delete_specific_candidates_impl", implementation),
            patch.object(
                cleanup,
                "_mark_candidate_delete_failures",
                AsyncMock(),
            ) as mark_failures,
            patch.object(
                cleanup,
                "_reconcile_leaving_soon_after_candidate_actions",
                reconcile,
            ),
        ):
            result = await cleanup.delete_specific_candidates([4, 4, 7])

        self.assertEqual(result, (0, 2))
        implementation.assert_not_awaited()
        mark_failures.assert_awaited_once()
        reconcile.assert_awaited_once()

    async def test_delete_prunes_before_action_and_reconciles_afterward(self) -> None:
        events: list[str] = []

        async def prune(_candidate_ids: list[int]) -> None:
            events.append("prune")

        async def implementation(
            _candidate_ids: list[int], approved_by: str
        ) -> tuple[int, int]:
            self.assertEqual(approved_by, "tester")
            events.append("delete")
            return 1, 0

        async def reconcile() -> None:
            events.append("reconcile")

        with (
            patch.object(
                cleanup,
                "_prune_leaving_soon_before_candidate_actions",
                prune,
            ),
            patch.object(cleanup, "_delete_specific_candidates_impl", implementation),
            patch.object(
                cleanup,
                "_reconcile_leaving_soon_after_candidate_actions",
                reconcile,
            ),
        ):
            result = await cleanup.delete_specific_candidates(
                [9],
                approved_by="tester",
            )

        self.assertEqual(result, (1, 0))
        self.assertEqual(events, ["prune", "delete", "reconcile"])

    async def test_move_uses_the_same_guard(self) -> None:
        prune = AsyncMock()
        implementation = AsyncMock(return_value=(1, 0))
        reconcile = AsyncMock()

        with (
            patch.object(
                cleanup,
                "_prune_leaving_soon_before_candidate_actions",
                prune,
            ),
            patch.object(cleanup, "_move_specific_candidates_impl", implementation),
            patch.object(
                cleanup,
                "_reconcile_leaving_soon_after_candidate_actions",
                reconcile,
            ),
        ):
            result = await cleanup.move_specific_candidates([11])

        self.assertEqual(result, (1, 0))
        prune.assert_awaited_once_with([11])
        implementation.assert_awaited_once()
        reconcile.assert_awaited_once()


class LeavingSoonPruneResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp_root = Path("tests/.tmp")
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.db_path = tmp_root / f"leaving_soon_guard_{uuid4().hex}.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")
        self.sessionmaker = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        if self.db_path.exists():
            self.db_path.unlink()

    async def test_movie_candidate_prunes_all_service_versions_before_action(
        self,
    ) -> None:
        async with self.sessionmaker() as db:
            # last_success_titles intentionally uses the legacy
            # {service_type: <base title>} shape here to also exercise the
            # backward-compat migration in
            # normalize_leaving_soon_last_success_titles.
            db.add(
                GeneralSettings(
                    leaving_soon_enabled=True,
                    leaving_soon_movie_collection_title="Leaving Soon [Movies]",
                    leaving_soon_series_collection_title="Leaving Soon [Series]",
                    leaving_soon_last_success_titles={
                        Service.PLEX.value: "Old Soon",
                        Service.JELLYFIN.value: "Leaving Soon",
                    },
                )
            )
            plex_config = ServiceConfig(
                service_type=Service.PLEX,
                name="Plex",
                base_url="http://plex",
                api_key="key",
                enabled=True,
                is_main=True,
            )
            jellyfin_config = ServiceConfig(
                service_type=Service.JELLYFIN,
                name="Jellyfin",
                base_url="http://jellyfin",
                api_key="key",
                enabled=True,
            )
            db.add_all([plex_config, jellyfin_config])
            movie = Movie(title="Movie", tmdb_id=1234)
            db.add(movie)
            await db.flush()
            plex_config_id = plex_config.id
            jellyfin_config_id = jellyfin_config.id
            plex_version = MovieVersion(
                movie_id=movie.id,
                service=Service.PLEX,
                service_item_id="plex-item",
                service_media_id="plex-media",
                library_id="plex-library",
                library_name="Movies",
            )
            jellyfin_version = MovieVersion(
                movie_id=movie.id,
                service=Service.JELLYFIN,
                service_item_id="jellyfin-item",
                service_media_id="jellyfin-media",
                library_id="jellyfin-library",
                library_name="Movies",
            )
            db.add_all([plex_version, jellyfin_version])
            await db.flush()
            candidate = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                movie_id=movie.id,
                movie_version_id=plex_version.id,
                matched_rule_ids=[],
                matched_criteria={},
                reason="test",
            )
            db.add(candidate)
            await db.commit()
            candidate_id = candidate.id

        class FakeService:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def prune_leaving_soon_items(self, **kwargs: Any) -> None:
                self.calls.append(kwargs)

        fake_plex = FakeService()
        fake_jellyfin = FakeService()
        previous_plex = cleanup.service_manager._plex
        previous_jellyfin = cleanup.service_manager._jellyfin
        previous_emby = cleanup.service_manager._emby
        previous_plex_clients = cleanup.service_manager._plex_clients
        previous_jellyfin_clients = cleanup.service_manager._jellyfin_clients
        cleanup.service_manager._plex = fake_plex  # type: ignore[assignment]
        cleanup.service_manager._jellyfin = fake_jellyfin  # type: ignore[assignment]
        cleanup.service_manager._emby = None
        cleanup.service_manager._plex_clients = {plex_config_id: fake_plex}  # type: ignore[dict-item]
        cleanup.service_manager._jellyfin_clients = {jellyfin_config_id: fake_jellyfin}  # type: ignore[dict-item]
        try:
            with patch.object(cleanup, "async_db", self.sessionmaker):
                await cleanup._prune_leaving_soon_before_candidate_actions(
                    [candidate_id]
                )
        finally:
            cleanup.service_manager._plex = previous_plex
            cleanup.service_manager._jellyfin = previous_jellyfin
            cleanup.service_manager._emby = previous_emby
            cleanup.service_manager._plex_clients = previous_plex_clients
            cleanup.service_manager._jellyfin_clients = previous_jellyfin_clients

        # the legacy base titles expand into the suffixed pair the clients
        # used to build themselves, so a pre-upgrade rename stays prunable
        self.assertEqual(
            {
                (call["movie_title"], call["series_title"])
                for call in fake_plex.calls
            },
            {
                ("Leaving Soon [Movies]", "Leaving Soon [Series]"),
                ("Old Soon [Movies]", "Old Soon [Series]"),
            },
        )
        self.assertTrue(
            all(call["movie_item_ids"] == {"plex-item"} for call in fake_plex.calls)
        )
        self.assertEqual(len(fake_jellyfin.calls), 1)
        self.assertEqual(
            fake_jellyfin.calls[0]["movie_item_ids"],
            {"jellyfin-item"},
        )

class LeavingSoonPlexCollectionSortTests(unittest.IsolatedAsyncioTestCase):
    """Adapter-level coverage for the Plex-only collection sort."""

    @staticmethod
    def _fake_plex(*, children: list[str] | None = None) -> Any:
        class FakeResponse:
            def __init__(self, payload: dict[str, Any] | None = None) -> None:
                self._payload = payload or {}

            def json(self) -> dict[str, Any]:
                return self._payload

            def raise_for_status(self) -> None:
                return None

        class FakeSession:
            def __init__(self) -> None:
                self.posts: list[dict[str, Any]] = []
                self.puts: list[tuple[str, dict[str, Any] | None]] = []
                self.put_error: Exception | None = None

            async def post(self, url: str, **kwargs: Any) -> FakeResponse:
                self.posts.append({"url": url, **kwargs})
                return FakeResponse(
                    {"MediaContainer": {"Metadata": [{"ratingKey": "col-new"}]}}
                )

            async def put(self, url: str, **kwargs: Any) -> FakeResponse:
                self.puts.append((url, kwargs.get("params")))
                if self.put_error is not None:
                    raise self.put_error
                return FakeResponse()

        class FakePlex:
            plex_url = "http://plex"

            def __init__(self) -> None:
                self.session = FakeSession()
                self.children = children if children is not None else []

            async def _make_request(self, endpoint: str, **kwargs: Any) -> Any:
                return {"MediaContainer": {"machineIdentifier": "machine-1"}}, 200

            async def _sync_leaving_soon_collection_for_type(
                self, **kwargs: Any
            ) -> None:
                await PlexService._sync_leaving_soon_collection_for_type(
                    self,  # type: ignore[arg-type]
                    **kwargs,
                )

            async def _get_section_ids_by_type(self, section_type: str) -> list[str]:
                return ["1"] if section_type == "movie" else []

            async def _find_collection_ids_by_title(self, **kwargs: Any) -> list[str]:
                return []

            async def _resolve_item_section_ids(
                self, item_ids: set[str]
            ) -> dict[str, str]:
                return {item_id: "1" for item_id in item_ids}

            async def _get_collection_child_ids_ordered(
                self, collection_id: str
            ) -> list[str]:
                return list(self.children)

            @staticmethod
            def _extract_metadata_items(payload: Any) -> list[dict[str, Any]]:
                return PlexService._extract_metadata_items(payload)

            async def _build_item_uri(self, item_ids: Any) -> str:
                return await PlexService._build_item_uri(
                    self,  # type: ignore[arg-type]
                    item_ids,
                )

            async def _create_collection(self, **kwargs: Any) -> str | None:
                return await PlexService._create_collection(
                    self,  # type: ignore[arg-type]
                    **kwargs,
                )

            async def _apply_collection_sort(self, **kwargs: Any) -> None:
                await PlexService._apply_collection_sort(
                    self,  # type: ignore[arg-type]
                    **kwargs,
                )

            async def _reorder_collection_items(self, **kwargs: Any) -> None:
                await PlexService._reorder_collection_items(
                    self,  # type: ignore[arg-type]
                    **kwargs,
                )

        return FakePlex()

    async def _sync(self, fake: Any, **kwargs: Any) -> None:
        await PlexService.sync_leaving_soon_collections(
            fake,  # type: ignore[arg-type]
            movie_title="Leaving Soon [Movies]",
            series_title=None,
            movie_item_ids={"10", "20", "30"},
            series_item_ids=set(),
            **kwargs,
        )

    async def test_default_sort_issues_no_prefs_put_and_keeps_id_order(self) -> None:
        fake = self._fake_plex()

        await self._sync(fake, collection_sort=LeavingSoonCollectionSort.DEFAULT)

        self.assertEqual(fake.session.puts, [])
        self.assertTrue(
            fake.session.posts[0]["params"]["uri"].endswith("/10,20,30"),
            fake.session.posts[0]["params"]["uri"],
        )

    async def test_alpha_sets_the_pref_and_never_moves_items(self) -> None:
        fake = self._fake_plex()

        await self._sync(fake, collection_sort=LeavingSoonCollectionSort.ALPHA)

        self.assertEqual(
            fake.session.puts,
            [("http://plex/library/metadata/col-new/prefs", {"collectionSort": "1"})],
        )

    async def test_leaving_soonest_builds_the_uri_in_deadline_order(self) -> None:
        now = datetime(2026, 9, 7, tzinfo=UTC)
        fake = self._fake_plex(children=["30", "20", "10"])

        await self._sync(
            fake,
            collection_sort=LeavingSoonCollectionSort.LEAVING_SOONEST,
            item_deadlines={
                "10": now + timedelta(days=9),
                "20": now + timedelta(days=5),
                "30": now + timedelta(days=1),
            },
        )

        self.assertTrue(
            fake.session.posts[0]["params"]["uri"].endswith("/30,20,10"),
            fake.session.posts[0]["params"]["uri"],
        )
        # Plex honored the create order, so no move calls are needed
        self.assertEqual(
            fake.session.puts,
            [("http://plex/library/metadata/col-new/prefs", {"collectionSort": "2"})],
        )

    async def test_leaving_soonest_moves_items_when_plex_ignores_create_order(
        self,
    ) -> None:
        now = datetime(2026, 9, 7, tzinfo=UTC)
        fake = self._fake_plex(children=["10", "20", "30"])

        await self._sync(
            fake,
            collection_sort=LeavingSoonCollectionSort.LEAVING_SOONEST,
            item_deadlines={
                "10": now + timedelta(days=9),
                "20": now + timedelta(days=5),
                "30": now + timedelta(days=1),
            },
        )

        self.assertEqual(
            fake.session.puts,
            [
                (
                    "http://plex/library/metadata/col-new/prefs",
                    {"collectionSort": "2"},
                ),
                ("http://plex/library/collections/col-new/items/30/move", None),
                (
                    "http://plex/library/collections/col-new/items/20/move",
                    {"after": "30"},
                ),
                (
                    "http://plex/library/collections/col-new/items/10/move",
                    {"after": "20"},
                ),
            ],
        )

    async def test_a_failed_prefs_put_does_not_fail_the_sync(self) -> None:
        fake = self._fake_plex()
        fake.session.put_error = HTTPError("nope")

        await self._sync(fake, collection_sort=LeavingSoonCollectionSort.ALPHA)

        # the collection itself still exists; only the ordering was skipped
        self.assertEqual(len(fake.session.posts), 1)


class LeavingSoonEmbySortIsIgnoredTests(unittest.IsolatedAsyncioTestCase):
    async def test_emby_accepts_the_sort_kwargs_without_extra_requests(self) -> None:
        class FakeService:
            def __init__(self) -> None:
                self.synced: list[dict[str, Any]] = []

            async def _sync_leaving_soon_collection(self, **kwargs: Any) -> None:
                self.synced.append(kwargs)

        fake = FakeService()
        await EmbyServiceBase.sync_leaving_soon_collections(
            fake,  # type: ignore[arg-type]
            movie_title="Leaving Soon [Movies]",
            series_title=None,
            movie_item_ids={"b", "a"},
            series_item_ids=set(),
            collection_sort=LeavingSoonCollectionSort.LEAVING_SOONEST,
            item_deadlines={"a": datetime(2026, 9, 7, tzinfo=UTC)},
        )

        # the sort never reaches the BoxSet calls - Emby has no ordering API
        self.assertEqual(
            fake.synced,
            [
                {
                    "collection_title": "Leaving Soon [Movies]",
                    "expected_item_ids": {"b", "a"},
                    "include_item_types": "Movie",
                }
            ],
        )

class LeavingSoonDeadlineResolutionTests(unittest.IsolatedAsyncioTestCase):
    """The deadline map that feeds the `leaving_soonest` collection sort."""

    async def asyncSetUp(self) -> None:
        tmp_root = Path("tests/.tmp")
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.db_path = tmp_root / f"leaving_soon_deadlines_{uuid4().hex}.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        if self.db_path.exists():
            self.db_path.unlink()

    async def test_series_takes_the_soonest_of_its_candidates(self) -> None:
        async with self.sessionmaker() as db:
            db.add(
                GeneralSettings(
                    leaving_soon_enabled=True,
                    auto_delete_movie_delay_days=14,
                    auto_delete_series_delay_days=7,
                )
            )
            plex_config = ServiceConfig(
                service_type=Service.PLEX,
                name="Plex",
                base_url="http://plex",
                api_key="key",
                enabled=True,
                is_main=True,
            )
            db.add(plex_config)
            series = Series(title="Series", tmdb_id=99)
            db.add(series)
            await db.flush()
            config_id = plex_config.id
            db.add(
                SeriesServiceRef(
                    series_id=series.id,
                    service=Service.PLEX,
                    service_id="plex-series",
                    library_id="plex-library",
                    library_name="Shows",
                )
            )
            # two candidates on the same series resolve to one rating key; the
            # earlier deadline has to win or the collection sorts it too late
            later = ReclaimCandidate(
                media_type=MediaType.SERIES,
                series_id=series.id,
                matched_rule_ids=[],
                matched_criteria={},
                reason="later",
            )
            sooner = ReclaimCandidate(
                media_type=MediaType.SERIES,
                series_id=series.id,
                matched_rule_ids=[],
                matched_criteria={},
                reason="sooner",
            )
            db.add_all([later, sooner])
            await db.flush()
            later.auto_delete_timer_started_at = datetime(2026, 9, 20)
            sooner.auto_delete_timer_started_at = datetime(2026, 9, 1)
            await db.commit()

        async with self.sessionmaker() as db:
            configs = await cleanup._get_enabled_leaving_soon_configs(db)
            (
                _movies,
                series_by_config,
                deadlines_by_config,
            ) = await cleanup._build_leaving_soon_expected_item_ids(db, configs)

        self.assertEqual(series_by_config, {config_id: {"plex-series"}})
        # 2026-09-01 + the 7 day series delay, not 2026-09-20 + 7
        self.assertEqual(
            deadlines_by_config[config_id]["plex-series"],
            datetime(2026, 9, 8, tzinfo=UTC),
        )

    async def test_movie_versions_inherit_their_movie_deadline(self) -> None:
        async with self.sessionmaker() as db:
            db.add(
                GeneralSettings(
                    leaving_soon_enabled=True,
                    auto_delete_movie_delay_days=14,
                    auto_delete_series_delay_days=7,
                )
            )
            plex_config = ServiceConfig(
                service_type=Service.PLEX,
                name="Plex",
                base_url="http://plex",
                api_key="key",
                enabled=True,
                is_main=True,
            )
            db.add(plex_config)
            movie = Movie(title="Movie", tmdb_id=1234)
            db.add(movie)
            await db.flush()
            config_id = plex_config.id
            version = MovieVersion(
                movie_id=movie.id,
                service=Service.PLEX,
                service_item_id="plex-item",
                service_media_id="plex-media",
                library_id="plex-library",
                library_name="Movies",
            )
            db.add(version)
            await db.flush()
            candidate = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                movie_id=movie.id,
                movie_version_id=version.id,
                matched_rule_ids=[],
                matched_criteria={},
                reason="test",
            )
            db.add(candidate)
            await db.flush()
            candidate.auto_delete_timer_started_at = datetime(2026, 9, 1)
            await db.commit()

        async with self.sessionmaker() as db:
            configs = await cleanup._get_enabled_leaving_soon_configs(db)
            (
                movies_by_config,
                _series,
                deadlines_by_config,
            ) = await cleanup._build_leaving_soon_expected_item_ids(db, configs)

        self.assertEqual(movies_by_config, {config_id: {"plex-item"}})
        # 2026-09-01 + the 14 day movie delay
        self.assertEqual(
            deadlines_by_config[config_id]["plex-item"],
            datetime(2026, 9, 15, tzinfo=UTC),
        )

    async def test_no_candidates_yields_an_empty_deadline_map(self) -> None:
        async with self.sessionmaker() as db:
            db.add(GeneralSettings(leaving_soon_enabled=True))
            db.add(
                ServiceConfig(
                    service_type=Service.PLEX,
                    name="Plex",
                    base_url="http://plex",
                    api_key="key",
                    enabled=True,
                    is_main=True,
                )
            )
            await db.commit()

        async with self.sessionmaker() as db:
            configs = await cleanup._get_enabled_leaving_soon_configs(db)
            result = await cleanup._build_leaving_soon_expected_item_ids(db, configs)

        self.assertEqual(result, ({}, {}, {}))
