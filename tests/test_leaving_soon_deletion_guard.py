from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    GeneralSettings,
    Movie,
    MovieVersion,
    ReclaimCandidate,
)
from backend.enums import MediaType, Service
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
            base_title="Leaving Soon",
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

            async def _get_collection_child_ids(self, collection_id: str) -> set[str]:
                return {"keep", "remove"}

            async def _delete_collection(
                self, *, section_id: str, collection_id: str
            ) -> None:
                self.deleted.append((section_id, collection_id))

            async def _create_collection(self, **kwargs: Any) -> None:
                self.created.append(kwargs)

        fake = FakePlex()
        await PlexService.prune_leaving_soon_items(
            fake,  # type: ignore[arg-type]
            base_title="Leaving Soon",
            movie_item_ids={"remove"},
            series_item_ids=set(),
        )

        self.assertEqual(fake.deleted, [("movies", "collection-1")])
        self.assertEqual(
            fake.created,
            [
                {
                    "section_id": "movies",
                    "section_type": "movie",
                    "collection_title": "Leaving Soon [Movies]",
                    "item_ids": {"keep"},
                }
            ],
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
            db.add(
                GeneralSettings(
                    leaving_soon_enabled=True,
                    leaving_soon_collection_title="Leaving Soon",
                    leaving_soon_last_success_titles={
                        Service.PLEX.value: "Old Soon",
                        Service.JELLYFIN.value: "Leaving Soon",
                    },
                )
            )
            movie = Movie(title="Movie", tmdb_id=1234)
            db.add(movie)
            await db.flush()
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
        cleanup.service_manager._plex = fake_plex  # type: ignore[assignment]
        cleanup.service_manager._jellyfin = fake_jellyfin  # type: ignore[assignment]
        cleanup.service_manager._emby = None
        try:
            with patch.object(cleanup, "async_db", self.sessionmaker):
                await cleanup._prune_leaving_soon_before_candidate_actions(
                    [candidate_id]
                )
        finally:
            cleanup.service_manager._plex = previous_plex
            cleanup.service_manager._jellyfin = previous_jellyfin
            cleanup.service_manager._emby = previous_emby

        self.assertEqual(
            {call["base_title"] for call in fake_plex.calls},
            {"Leaving Soon", "Old Soon"},
        )
        self.assertTrue(
            all(call["movie_item_ids"] == {"plex-item"} for call in fake_plex.calls)
        )
        self.assertEqual(len(fake_jellyfin.calls), 1)
        self.assertEqual(
            fake_jellyfin.calls[0]["movie_item_ids"],
            {"jellyfin-item"},
        )
