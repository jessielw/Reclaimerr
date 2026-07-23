from __future__ import annotations

import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.rule_engine import TARGET_EPISODE, TARGET_SEASON, TARGET_SERIES
from backend.database import Base
from backend.database.models import (
    Episode,
    ProtectedMedia,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
)
from backend.enums import MediaType, Service
from backend.tasks.cleanup import (
    _collect_episode_candidate_records,
    _collect_season_candidate_records,
    _collect_series_candidate_records,
)
from backend.tasks.sync import _sync_seasons


def _size_rule(target_scope: str) -> ReclaimRule:
    return ReclaimRule(
        name=f"{target_scope} physical inventory",
        media_type=MediaType.SERIES,
        enabled=True,
        target_scope=target_scope,
        definition={
            "version": 1,
            "root": {
                "type": "group",
                "op": "and",
                "children": [
                    {
                        "type": "condition",
                        "field": "media.size",
                        "operator": "greater_than",
                        "value": 0,
                    }
                ],
            },
        },
        action={"candidate": True, "media_server_action": "delete"},
    )


class PhysicalTvInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessionmaker = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            autoflush=False,
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_rule_collectors_only_return_path_backed_tv_targets(self) -> None:
        async with self.sessionmaker() as session:
            series_rule = _size_rule(TARGET_SERIES)
            season_rule = _size_rule(TARGET_SEASON)
            episode_rule = _size_rule(TARGET_EPISODE)
            physical_series = Series(title="Physical", tmdb_id=81001, size=100)
            pathless_series = Series(title="Pathless", tmdb_id=81002, size=100)
            session.add_all(
                [
                    series_rule,
                    season_rule,
                    episode_rule,
                    physical_series,
                    pathless_series,
                ]
            )
            await session.flush()

            physical_season = Season(
                series_id=physical_series.id,
                season_number=1,
                size=100,
                path="/media/Physical/Season 01",
            )
            pathless_season = Season(
                series_id=pathless_series.id,
                season_number=1,
                size=100,
            )
            extra_pathless_season = Season(
                series_id=physical_series.id,
                season_number=2,
                size=100,
            )
            session.add_all([physical_season, pathless_season, extra_pathless_season])
            await session.flush()

            physical_episode = Episode(
                season_id=physical_season.id,
                episode_number=1,
                size=100,
                path="/media/Physical/Season 01/S01E01.mkv",
            )
            pathless_episode = Episode(
                season_id=physical_season.id,
                episode_number=2,
                size=100,
            )
            session.add_all([physical_episode, pathless_episode])
            await session.commit()
            physical_series_id = physical_series.id
            physical_season_id = physical_season.id
            physical_episode_id = physical_episode.id

        async with self.sessionmaker() as session:
            rules = (await session.execute(select(ReclaimRule))).scalars().all()
            rules_by_scope = {rule.target_scope: rule for rule in rules}
            series_records = await _collect_series_candidate_records(
                session,
                [rules_by_scope[TARGET_SERIES]],
                exclude_favorites=False,
                exclude_protected=False,
            )
            season_records = await _collect_season_candidate_records(
                session,
                [rules_by_scope[TARGET_SEASON]],
                exclude_favorites=False,
                exclude_protected=False,
            )
            episode_records = await _collect_episode_candidate_records(
                session,
                [rules_by_scope[TARGET_EPISODE]],
                exclude_favorites=False,
                exclude_protected=False,
            )

        self.assertEqual(
            [record.series_id for record in series_records],
            [physical_series_id],
        )
        self.assertEqual(
            [record.season_id for record in season_records],
            [physical_season_id],
        )
        self.assertEqual(
            [record.episode_id for record in episode_records],
            [physical_episode_id],
        )

    async def test_empty_authoritative_inventory_removes_stale_tv_children(
        self,
    ) -> None:
        async with self.sessionmaker() as session:
            series = Series(title="No Files", tmdb_id=82001, size=0)
            session.add(series)
            await session.flush()
            season = Season(
                series_id=series.id,
                season_number=1,
                size=100,
                path="/media/No Files/Season 01",
            )
            session.add(season)
            await session.flush()
            episode = Episode(
                season_id=season.id,
                episode_number=1,
                size=100,
                path="/media/No Files/Season 01/S01E01.mkv",
            )
            session.add(episode)
            await session.flush()

            session.add_all(
                [
                    ReclaimCandidate(
                        media_type=MediaType.SERIES,
                        series_id=series.id,
                        matched_rule_ids=[1],
                        matched_criteria={},
                        reason="series",
                    ),
                    ReclaimCandidate(
                        media_type=MediaType.SERIES,
                        series_id=series.id,
                        season_id=season.id,
                        matched_rule_ids=[1],
                        matched_criteria={},
                        reason="season",
                    ),
                    ReclaimCandidate(
                        media_type=MediaType.SERIES,
                        series_id=series.id,
                        season_id=season.id,
                        episode_id=episode.id,
                        matched_rule_ids=[1],
                        matched_criteria={},
                        reason="episode",
                    ),
                    ProtectedMedia(
                        media_type=MediaType.SERIES,
                        series_id=series.id,
                        reason="keep the catalog item protected",
                    ),
                ]
            )
            await session.commit()

            await _sync_seasons(session, series.id, [], Service.JELLYFIN)
            await session.commit()

            remaining_series = (
                await session.execute(select(Series).where(Series.id == series.id))
            ).scalar_one_or_none()
            remaining_seasons = (await session.execute(select(Season))).scalars().all()
            remaining_episodes = (
                (await session.execute(select(Episode))).scalars().all()
            )
            remaining_candidates = (
                (await session.execute(select(ReclaimCandidate))).scalars().all()
            )
            remaining_protections = (
                (await session.execute(select(ProtectedMedia))).scalars().all()
            )

        self.assertIsNotNone(remaining_series)
        self.assertEqual(remaining_seasons, [])
        self.assertEqual(remaining_episodes, [])
        self.assertEqual(remaining_candidates, [])
        self.assertEqual(len(remaining_protections), 1)
        self.assertEqual(remaining_protections[0].series_id, series.id)
