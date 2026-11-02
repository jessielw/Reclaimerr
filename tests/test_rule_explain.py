from __future__ import annotations

import copy
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.routes.rules import router
from backend.core.auth import get_current_user
from backend.core.rule_engine import (
    RULE_VALUE_UNAVAILABLE,
    evaluate_advanced_rule_state,
    explain_advanced_rule,
)
from backend.database import Base, get_db
from backend.database.models import (
    Episode,
    GeneralSettings,
    MediaFavorite,
    Movie,
    MovieVersion,
    ProtectedMedia,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
)
from backend.enums import MediaType, Service, UserRole
from backend.services.rule_explain import explain_rule_target, list_rule_explain_targets
from backend.tasks import cleanup


def condition(field="media.size", operator="greater_than", value=1, **kwargs):
    return {
        "type": "condition",
        "field": field,
        "operator": operator,
        "value": value,
        **kwargs,
    }


def group(op, *children, **kwargs):
    return {"type": "group", "op": op, "children": list(children), **kwargs}


def rule(scope="movie_version", root=None, outcome="candidate"):
    return ReclaimRule(
        name="Unsaved rule",
        enabled=True,
        target_scope=scope,
        media_type=MediaType.MOVIE if scope == "movie_version" else MediaType.SERIES,
        definition={"version": 1, "root": root or group("and", condition())},
        action={"outcome": outcome},
    )


class ExplainEngineTests(unittest.TestCase):
    def explain(self, root, values):
        current = rule(root=root)
        before = copy.deepcopy(current.definition)
        with patch(
            "backend.core.rule_engine._build_context",
            side_effect=lambda *args: dict(values),
        ):
            result = explain_advanced_rule(current, target_scope="movie_version")
            state = evaluate_advanced_rule_state(current, target_scope="movie_version")
        self.assertEqual(current.definition, before)
        self.assertEqual(
            result["state"], "unknown" if state is None else "pass" if state else "fail"
        )
        return result

    def test_visits_conditions_after_failure_and_preserves_nested_groups(self):
        result = self.explain(
            group(
                "and",
                condition(value=100),
                group("or", condition(value=1), condition(value=1000)),
            ),
            {"media.size": 50},
        )
        self.assertEqual(result["state"], "fail")
        self.assertEqual(result["children"][1]["state"], "pass")
        self.assertEqual(
            [child["actual"] for child in result["children"][1]["children"]], [50, 50]
        )

    def test_three_valued_logic_and_disabled_ancestors(self):
        unknown = condition("playback.play_count", value=1)
        for op, known_value, expected in [
            ("and", 50, "unknown"),
            ("and", 0, "fail"),
            ("or", 50, "pass"),
            ("or", 0, "unknown"),
        ]:
            with self.subTest(op=op, known_value=known_value):
                result = self.explain(
                    group(
                        op,
                        unknown,
                        condition(),
                        group("and", condition(), enabled=False),
                    ),
                    {
                        "playback.play_count": RULE_VALUE_UNAVAILABLE,
                        "media.size": known_value,
                    },
                )
                self.assertEqual(result["state"], expected)
                self.assertEqual(result["children"][0]["state"], "unknown")
                self.assertIsNone(result["children"][0]["actual"])
                self.assertEqual(
                    result["children"][2]["children"][0]["state"], "disabled"
                )

    def test_missing_value_is_distinct_from_unavailable_data(self):
        result = self.explain(
            group("and", condition("watch.last_viewed_at", "not_exists", None)),
            {"watch.last_viewed_at": None},
        )
        self.assertEqual(result["state"], "pass")
        self.assertIsNone(result["children"][0]["actual"])

    def test_user_scoped_playback_uses_actual_selected_user_comparison(self):
        result = self.explain(
            group(
                "and",
                condition(
                    "playback.user_watched_duration_minutes",
                    "less_than",
                    {"usernames": ["alice", "bob"], "amount": 5},
                ),
            ),
            {"playback.user_watched_duration_minutes": {"alice": 20, "bob": 2}},
        )
        self.assertEqual(result["state"], "pass")
        self.assertEqual(result["children"][0]["actual"], {"alice": 20, "bob": 2})
        result = self.explain(
            group(
                "and",
                condition(
                    "playback.user_watched_duration_minutes",
                    "less_than",
                    {"usernames": ["alice", "bob"], "amount": 1},
                ),
            ),
            {"playback.user_watched_duration_minutes": {"alice": 20, "bob": 2}},
        )
        self.assertEqual(result["state"], "fail")
        self.assertEqual(result["children"][0]["actual"], {"alice": 20, "bob": 2})


class ExplainIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.patches = [
            patch.object(cleanup, "async_db", self.sessions),
            patch(
                "backend.services.media_watch_snapshot_cache.async_db", self.sessions
            ),
            patch("backend.services.playback_history.async_db", self.sessions),
            patch.object(
                cleanup,
                "_ensure_favorites_snapshot_if_enabled",
                AsyncMock(return_value=(True, None)),
            ),
            patch.object(cleanup, "_load_arr_disk_space", AsyncMock(return_value=[])),
            patch.object(cleanup, "_refresh_arr_tags_for_rules", AsyncMock()),
            patch.object(cleanup, "_refresh_arr_monitoring_for_rules", AsyncMock()),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        async with self.sessions() as db:
            movie = Movie(title="A Movie", tmdb_id=100, size=100)
            series = Series(title="A Show", tmdb_id=200, size=100)
            removed = Movie(
                title="Removed 100%_title",
                tmdb_id=300,
                size=100,
            )
            removed.removed_at = datetime.now(UTC)
            db.add_all([movie, series, removed])
            await db.flush()
            self.movie_id, self.series_id = movie.id, series.id
            version = MovieVersion(
                movie_id=movie.id,
                service=Service.PLEX,
                service_item_id="one",
                service_media_id="one",
                library_id="movies",
                library_name="Movies",
                path="/movies/a.mkv",
                size=100,
            )
            removed_version = MovieVersion(
                movie_id=removed.id,
                service=Service.PLEX,
                service_item_id="removed",
                service_media_id="removed",
                library_id="movies",
                library_name="Movies",
                path="/movies/removed.mkv",
                size=100,
            )
            season = Season(
                series_id=series.id,
                season_number=1,
                path="/shows/a/Season 01",
                size=100,
                episode_count=2,
            )
            db.add_all([version, removed_version, season])
            await db.flush()
            self.version_id, self.removed_id, self.season_id = (
                version.id,
                removed_version.id,
                season.id,
            )
            episode = Episode(
                season_id=season.id, episode_number=1, path="/shows/a/e01.mkv", size=50
            )
            peer = Episode(
                season_id=season.id, episode_number=2, path="/shows/a/e01.mkv", size=50
            )
            db.add_all([episode, peer])
            await db.commit()
            self.episode_id, self.peer_id = episode.id, peer.id

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_all_scopes_match_preview_and_never_persist_rule_or_candidates(self):
        for scope, target_id in [
            ("movie_version", self.version_id),
            ("series", self.series_id),
            ("season", self.season_id),
            ("episode", self.episode_id),
        ]:
            with self.subTest(scope=scope):
                async with self.sessions() as db:
                    current = rule(scope)
                    result = await explain_rule_target(db, current, scope, target_id)
                    self.assertTrue(result.eligible)
                    self.assertEqual(result.condition_state, "pass")
                    self.assertEqual(result.exclusions, [])
                    self.assertEqual(
                        result.root.children[0].actual,
                        100 if scope != "episode" else 50,
                    )
                    self.assertEqual(
                        await db.scalar(
                            select(func.count()).select_from(ReclaimCandidate)
                        ),
                        0,
                    )
                    self.assertEqual(
                        await db.scalar(select(func.count()).select_from(ReclaimRule)),
                        0,
                    )
                    self.assertEqual(
                        await db.scalar(
                            select(func.count()).select_from(ProtectedMedia)
                        ),
                        0,
                    )
                    self.assertNotIn(current, db)

    async def test_version_protection_and_expiry_and_non_candidate_outcomes(self):
        async with self.sessions() as db:
            protection = ProtectedMedia(
                media_type=MediaType.MOVIE,
                movie_id=self.movie_id,
                movie_version_id=self.version_id,
                reason="Keep this copy",
            )
            db.add(protection)
            await db.commit()
        async with self.sessions() as db:
            result = await explain_rule_target(
                db, rule(), "movie_version", self.version_id
            )
            self.assertFalse(result.eligible)
            self.assertEqual(result.condition_state, "pass")
            self.assertIn("Keep this copy", " ".join(result.exclusions))
            for outcome in ["protect", "match"]:
                result = await explain_rule_target(
                    db, rule(outcome=outcome), "movie_version", self.version_id
                )
                self.assertTrue(result.eligible)
                self.assertEqual(result.exclusions, [])
            protection = await db.get(ProtectedMedia, protection.id)
            protection.permanent = False
            protection.expires_at = datetime.now(UTC) - timedelta(days=1)
            await db.commit()
        async with self.sessions() as db:
            self.assertTrue(
                (
                    await explain_rule_target(
                        db, rule(), "movie_version", self.version_id
                    )
                ).eligible
            )

    async def test_favorites_removed_and_missing_paths_are_explained(self):
        async with self.sessions() as db:
            db.add(
                GeneralSettings(
                    favorites_ignore_enabled=True, favorites_protect_all_users=True
                )
            )
            db.add(
                MediaFavorite(
                    media_type=MediaType.MOVIE,
                    tmdb_id=100,
                    source_service=Service.PLEX,
                    source_service_config_id=1,
                    username="alice",
                    username_normalized="alice",
                )
            )
            episode = await db.get(Episode, self.episode_id)
            episode.path = None
            await db.commit()
        async with self.sessions() as db:
            favorite = await explain_rule_target(
                db, rule(), "movie_version", self.version_id
            )
            self.assertFalse(favorite.eligible)
            self.assertIn("favorites", " ".join(favorite.exclusions))
            removed = await explain_rule_target(
                db, rule(), "movie_version", self.removed_id
            )
            self.assertIn("removed", " ".join(removed.exclusions))
            missing = await explain_rule_target(
                db, rule("episode"), "episode", self.episode_id
            )
            self.assertFalse(missing.eligible)
            self.assertIn("no media file path", " ".join(missing.exclusions))

    async def test_episode_file_peer_protection_and_whole_show_protection(self):
        async with self.sessions() as db:
            db.add(
                ProtectedMedia(
                    media_type=MediaType.SERIES,
                    series_id=self.series_id,
                    season_id=self.season_id,
                    episode_id=self.peer_id,
                    reason="Shared file",
                )
            )
            await db.commit()
        async with self.sessions() as db:
            result = await explain_rule_target(
                db, rule("episode"), "episode", self.episode_id
            )
            self.assertFalse(result.eligible)
            self.assertIn("Shared file", " ".join(result.exclusions))
            result = await explain_rule_target(
                db, rule("series"), "series", self.series_id
            )
            self.assertFalse(result.eligible)
            self.assertIn("Shared file", " ".join(result.exclusions))

    async def test_search_pagination_escaping_and_scopes(self):
        async with self.sessions() as db:
            for scope in ["movie_version", "series", "season", "episode"]:
                found = await list_rule_explain_targets(db, scope, "a", 1, 1)
                self.assertEqual(len(found.items), 1)
                self.assertEqual(found.page, 1)
            found = await list_rule_explain_targets(db, "movie_version", "%_", 1, 20)
            self.assertEqual(found.total, 1)
            self.assertEqual(found.items[0].id, self.removed_id)
            found = await list_rule_explain_targets(db, "movie_version", "", 2, 1)
            self.assertEqual(found.total, 2)
            self.assertEqual(len(found.items), 1)
            self.assertEqual(
                (
                    await list_rule_explain_targets(db, "episode", "e01.mkv", 1, 20)
                ).total,
                2,
            )

    async def test_api_unsaved_edits_validation_missing_target_and_admin_guard(self):
        app = FastAPI()
        app.include_router(router)

        async def get_test_db():
            async with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = get_test_db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            role=UserRole.ADMIN
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            payload = {
                "target_scope": "movie_version",
                "target_id": self.version_id,
                "definition": rule(root=group("and", condition(value=999))).definition,
            }
            result = await client.post("/api/rules/explain", json=payload)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["condition_state"], "fail")
            self.assertFalse(result.json()["eligible"])
            payload["target_id"] = 99999
            self.assertEqual(
                (await client.post("/api/rules/explain", json=payload)).status_code, 404
            )
            payload["target_scope"] = "bad"
            self.assertEqual(
                (await client.post("/api/rules/explain", json=payload)).status_code, 422
            )
            payload.update(
                target_scope="movie_version", target_id=self.version_id, definition={}
            )
            self.assertEqual(
                (await client.post("/api/rules/explain", json=payload)).status_code, 422
            )
            self.assertEqual(
                (
                    await client.get("/api/rules/explain/targets?target_scope=season")
                ).status_code,
                200,
            )

            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
                role=UserRole.USER
            )
            self.assertEqual(
                (await client.post("/api/rules/explain", json=payload)).status_code, 403
            )
            self.assertEqual(
                (
                    await client.get("/api/rules/explain/targets?target_scope=season")
                ).status_code,
                403,
            )

    async def test_unavailable_playback_is_unknown_and_serializable(self):
        async with self.sessions() as db:
            current = rule(
                root=group("and", condition("playback.play_count", "greater_than", 1))
            )
            result = await explain_rule_target(
                db, current, "movie_version", self.version_id
            )
            self.assertFalse(result.eligible)
            self.assertEqual(result.condition_state, "unknown")
            self.assertIsNone(result.root.children[0].actual)
            self.assertIn('"condition_state":"unknown"', result.model_dump_json())

    async def test_managed_season_protection_and_empty_inventory(self):
        async with self.sessions() as db:
            db.add(
                ProtectedMedia(
                    media_type=MediaType.SERIES,
                    series_id=self.series_id,
                    season_id=self.season_id,
                    source="rule",
                    reason="Keep recent seasons",
                )
            )
            await db.commit()
        async with self.sessions() as db:
            result = await explain_rule_target(
                db, rule("season"), "season", self.season_id
            )
            self.assertFalse(result.eligible)
            self.assertEqual(result.condition_state, "pass")
            self.assertIn("automated protection rule", " ".join(result.exclusions))
            season = await db.get(Season, self.season_id)
            season.size = 0
            season.episode_count = 0
            await db.commit()
        async with self.sessions() as db:
            result = await explain_rule_target(
                db, rule("series"), "series", self.series_id
            )
            self.assertFalse(result.eligible)
            self.assertIn("no season", " ".join(result.exclusions))
