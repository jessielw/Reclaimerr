from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.rules import _normalize_rule_action
from backend.api.routes.v1 import discover_api
from backend.api.routes.v1 import router as external_api_router
from backend.api.routes.v1.rules import get_rule_matches, list_rules
from backend.core.api_tokens import ApiPrincipal, get_api_principal
from backend.database import Base, get_db
from backend.database.models import (
    Episode,
    Movie,
    MovieArrRef,
    ReclaimRule,
    Season,
    Series,
    SeriesArrRef,
    ServiceConfig,
)
from backend.enums import MediaType, Service
from backend.models.cleanup import (
    MatchedCandidateRecord,
    RulePreviewMatchMetadata,
    RulePreviewMatchResult,
)

PRINCIPAL = ApiPrincipal(token_id=1, name="Script", scopes=frozenset({"rules:read"}))
DEFINITION = {
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
}


def _rule(name: str, outcome: str, *, scope: str = "movie_version") -> ReclaimRule:
    return ReclaimRule(
        name=name,
        media_type=MediaType.MOVIE if scope == "movie_version" else MediaType.SERIES,
        enabled=True,
        target_scope=scope,
        definition=DEFINITION,
        action={"outcome": outcome},
    )


def _record(**kwargs: Any) -> MatchedCandidateRecord:
    return MatchedCandidateRecord(
        matched_rule_ids=[],
        matched_criteria={},
        reason="Matched",
        reason_data=[],
        estimated_space_bytes=None,
        **kwargs,
    )


def test_match_outcome_strips_every_action_field() -> None:
    action = _normalize_rule_action(
        {
            "outcome": "match",
            "tag_enabled": True,
            "arr_tag": "rec-keep",
            "arr_action": "unmonitor",
            "auto_delete_enabled": True,
            "auto_delete_delay_days": 3,
            "move_instead_of_delete": True,
            "radarr_service_config_ids": [1],
        },
        "Match rule",
        "movie_version",
    )
    assert action["outcome"] == "match"
    assert action["candidate"] is False
    assert action["tag_enabled"] is False
    assert action["arr_tag"] is None
    assert action["arr_action"] == "delete"
    assert action["media_server_action"] is None
    assert action["auto_delete_enabled"] is False
    assert action["auto_delete_delay_days"] is None
    assert action["move_instead_of_delete"] is False
    assert action["quality_profile_id"] is None
    assert action["radarr_service_config_ids"] == []
    assert action["sonarr_service_config_ids"] == []


def test_rules_api_lists_rules_and_returns_match_only_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db:
            radarr = ServiceConfig(
                service_type=Service.RADARR,
                base_url="http://radarr",
                api_key="key",
                name="Radarr 4K",
            )
            sonarr = ServiceConfig(
                service_type=Service.SONARR,
                base_url="http://sonarr",
                api_key="key",
                name="",
            )
            movie = Movie(title="Match Movie", tmdb_id=550, imdb_id="tt0137523")
            movie.year = 1999
            series = Series(title="Match Series", tmdb_id=1399, tvdb_id="121361")
            match_rule = _rule("Movies to re-profile", "match")
            episode_rule = _rule("Episodes", "match", scope="episode")
            candidate_rule = _rule("Cleanup", "candidate")
            protect_rule = _rule("Keep", "protect")
            disabled_rule = _rule("Off", "match")
            disabled_rule.enabled = False
            db.add_all(
                [
                    radarr,
                    sonarr,
                    movie,
                    series,
                    match_rule,
                    episode_rule,
                    candidate_rule,
                    protect_rule,
                    disabled_rule,
                ]
            )
            await db.flush()
            season = Season(series_id=series.id, season_number=2)
            db.add(season)
            await db.flush()
            episode = Episode(season_id=season.id, episode_number=5)
            db.add_all(
                [
                    episode,
                    MovieArrRef(
                        movie_id=movie.id, service_config_id=radarr.id, arr_movie_id=42
                    ),
                    SeriesArrRef(
                        series_id=series.id,
                        service_config_id=sonarr.id,
                        arr_series_id=7,
                    ),
                ]
            )
            await db.commit()

            listed = await list_rules(PRINCIPAL, db)
            assert [(item.name, item.outcome) for item in listed.items] == [
                ("Cleanup", "candidate"),
                ("Episodes", "match"),
                ("Keep", "protect"),
                ("Movies to re-profile", "match"),
                ("Off", "match"),
            ]
            assert listed.items[1].target_scope == "episode"

            evaluated: list[ReclaimRule] = []
            metadata = RulePreviewMatchMetadata()
            records = [
                _record(
                    media_type=MediaType.MOVIE,
                    movie_id=movie.id,
                    movie_version_id=9,
                )
            ]

            async def fake_collect(
                _db: AsyncSession, rules: list[ReclaimRule]
            ) -> RulePreviewMatchResult:
                evaluated.extend(rules)
                return RulePreviewMatchResult(matches=records, metadata=metadata)

            monkeypatch.setattr(
                "backend.api.routes.v1.rules.collect_rule_preview_matches_with_metadata",
                fake_collect,
            )

            result = await get_rule_matches(match_rule.id, PRINCIPAL, db)
            assert [rule.id for rule in evaluated] == [match_rule.id]
            assert result.complete is True
            assert result.warnings == []
            assert result.rule.target_scope == "movie_version"
            assert result.total == 1
            item = result.items[0]
            assert (item.movie_id, item.movie_version_id) == (movie.id, 9)
            assert (item.title, item.year) == ("Match Movie", 1999)
            assert (item.tmdb_id, item.imdb_id) == (550, "tt0137523")
            assert [ref.model_dump() for ref in item.arr] == [
                {
                    "service": "radarr",
                    "service_config_id": radarr.id,
                    "service_name": "Radarr 4K",
                    "arr_id": 42,
                }
            ]

            records[:] = [
                _record(
                    media_type=MediaType.SERIES,
                    series_id=series.id,
                    episode_id=episode.id,
                )
            ]
            metadata.playback_unavailable_count = 3
            metadata.playback_error = "Tautulli is unreachable"
            result = await get_rule_matches(episode_rule.id, PRINCIPAL, db)
            assert result.complete is False
            assert result.warnings == [
                "Playback history was unavailable for 3 item(s): "
                "Tautulli is unreachable"
            ]
            item = result.items[0]
            assert (item.series_id, item.season_number, item.episode_number) == (
                series.id,
                2,
                5,
            )
            assert item.tvdb_id == "121361"
            assert [
                (ref.service, ref.arr_id, ref.service_name) for ref in item.arr
            ] == [("sonarr", 7, None)]

            for rule_id, expected in (
                (999, 404),
                (candidate_rule.id, 409),
                (protect_rule.id, 409),
                (disabled_rule.id, 409),
            ):
                with pytest.raises(HTTPException) as error:
                    await get_rule_matches(rule_id, PRINCIPAL, db)
                assert error.value.status_code == expected

        await engine.dispose()

    asyncio.run(run())


def test_rules_api_requires_rules_read_scope() -> None:
    app = FastAPI()
    app.include_router(external_api_router)
    app.dependency_overrides[get_db] = lambda: None
    client = TestClient(app)

    assert client.get("/api/v1/rules").status_code == 401
    assert client.get("/api/v1/rules/1/matches").status_code == 401

    app.dependency_overrides[get_api_principal] = lambda: ApiPrincipal(
        token_id=1, name="Other", scopes=frozenset({"candidates:manage"})
    )
    assert client.get("/api/v1/rules").status_code == 403
    assert client.get("/api/v1/rules/1/matches").status_code == 403


def test_rules_resource_is_discoverable() -> None:
    discovery = asyncio.run(discover_api(PRINCIPAL))
    assert discovery.resources["rules"] == "/api/v1/rules"
    assert discovery.granted_scopes == ["rules:read"]
