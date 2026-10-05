from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.routes.rules import (
    create_rule,
    get_rule_presets,
    import_rules,
    update_rule,
)
from backend.core.rule_engine import (
    FavoritesRuleDataResolver,
    PlaybackHistoryResolver,
    SeerrRequestResolver,
    SonarrRuleDataResolver,
    WatchCompletionResolver,
    evaluate_advanced_rule,
    validate_rule_definition,
)
from backend.database import Base
from backend.database.models import (
    BackgroundJob,
    Movie,
    MovieVersion,
    ReclaimRule,
    Season,
    Series,
    SeriesServiceRef,
    User,
)
from backend.enums import MediaType, Service, UserRole
from backend.models.cleanup import CleanupRuleUpdate, RuleImportPayload
from backend.services.rule_presets import rule_presets


def _draft(preset_id):
    preset = next(p for p in rule_presets() if p.id == preset_id)
    for node in preset.rule.definition["root"]["children"]:
        if node["field"] == "library.id":
            node["value"] = ["library"]
        elif node["field"] == "playback.fully_watched_usernames":
            node["value"] = ["alice", "bob"]
    return preset.rule


def _rule(preset_id):
    return ReclaimRule(**_draft(preset_id).model_dump())


@pytest.mark.parametrize("preset", rule_presets(), ids=lambda p: p.id)
def test_presets_require_binding_then_pass_normal_validation(preset):
    with pytest.raises(ValueError):
        validate_rule_definition(
            preset.rule.definition, target_scope=preset.rule.target_scope
        )
    draft = _draft(preset.id)
    for scope in preset.target_scopes:
        validate_rule_definition(draft.definition, target_scope=scope)
    assert not draft.enabled
    assert draft.action is not None
    for key in (
        "auto_delete_enabled",
        "move_instead_of_delete",
        "tag_enabled",
        "trigger_search",
    ):
        assert draft.action[key] is False
    for key in ("radarr_service_config_ids", "sonarr_service_config_ids"):
        assert draft.action[key] == []


def _movie_match(
    preset_id,
    *,
    age=200,
    watched_days=None,
    views=0,
    size=41 * 1024**3,
    library="library",
):
    now = datetime.now(UTC)
    movie = Movie(title="Example", tmdb_id=1, size=size, view_count=views)
    movie.added_at = now - timedelta(days=age)
    movie.last_viewed_at = (
        now - timedelta(days=watched_days) if watched_days is not None else None
    )
    version = MovieVersion(
        movie_id=1,
        service=Service.PLEX,
        service_item_id="item",
        service_media_id="version",
        library_id=library,
        library_name="Movies",
        size=size,
        added_at=movie.added_at,
    )
    version.id = 1
    return evaluate_advanced_rule(
        _rule(preset_id), target_scope="movie_version", movie=movie, version=version
    )[0]


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, True),
        ({"age": 179}, False),
        ({"views": 1}, False),
        ({"watched_days": 2}, False),
        ({"library": "other"}, False),
    ],
)
def test_old_unwatched_movies(kwargs, expected):
    assert _movie_match("old-unwatched-movies", **kwargs) is expected


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"views": 1, "watched_days": 100}, True),
        ({"views": 0, "watched_days": 100}, True),
        ({"views": 1, "watched_days": 89}, False),
        ({"views": 1}, False),
        ({}, False),
        ({"age": 10, "views": 1, "watched_days": 100}, False),
    ],
)
def test_watched_movies_require_a_current_copy_watch_date(kwargs, expected):
    assert _movie_match("watched-movies-not-revisited", **kwargs) is expected


def test_large_movies_threshold_and_missing_size():
    assert _movie_match("large-movies-for-review")
    assert not _movie_match("large-movies-for-review", size=40 * 1024**3)
    assert not _movie_match("large-movies-for-review", size=0)


def _series():
    series = Series(title="Show", tmdb_id=900)
    series.added_at = datetime.now(UTC) - timedelta(days=400)
    series.service_refs = [
        SeriesServiceRef(
            series_id=1,
            service=Service.PLEX,
            service_id="series",
            library_id="library",
            library_name="TV",
        )
    ]
    series.seasons = [
        Season(series_id=1, season_number=n, size=100) for n in (0, 1, 3, 7)
    ]
    for season in series.seasons:
        season.id = season.season_number + 1
        season.added_at = series.added_at
        season.episodes = []
    return series


def test_newest_two_counts_known_regular_seasons_including_gaps_and_no_files():
    series = _series()
    series.seasons[-1].size = 0
    rule = _rule("keep-newest-two-seasons")
    matches = [
        season.season_number
        for season in series.seasons
        if evaluate_advanced_rule(
            rule, target_scope="season", series=series, season=season
        )[0]
    ]
    assert matches == [1]
    series.seasons = series.seasons[:2]
    assert not evaluate_advanced_rule(
        rule, target_scope="season", series=series, season=series.seasons[1]
    )[0]


@pytest.mark.parametrize(
    "completed,activity,special,expected",
    [
        (["alice", "bob"], 31, False, True),
        (["alice"], 31, False, False),
        ([], 31, False, False),
        (None, 31, False, False),
        (["alice", "bob"], None, False, False),
        (["alice", "bob"], 30, False, False),
        (["alice", "bob"], 31, True, False),
    ],
)
def test_fully_watched_requires_every_user_known_activity_and_regular_season(
    completed, activity, special, expected
):
    series = _series()
    season = series.seasons[0 if special else 1]
    completion_token = WatchCompletionResolver._ctx.set(
        WatchCompletionResolver(
            {}
            if completed is None
            else {("season", 900, season.season_number, None): completed}
        )
    )
    playback_token = PlaybackHistoryResolver._ctx.set(
        PlaybackHistoryResolver(
            {}
            if activity is None
            else {
                ("season", season.id): {"playback.days_since_last_activity": activity}
            }
        )
    )
    try:
        assert (
            evaluate_advanced_rule(
                _rule("fully-watched-seasons"),
                target_scope="season",
                series=series,
                season=season,
            )[0]
            is expected
        )
    finally:
        WatchCompletionResolver._ctx.reset(completion_token)
        PlaybackHistoryResolver._ctx.reset(playback_token)


def test_catalog_does_not_save_and_saved_rules_use_normal_edit_and_import():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        admin = User(
            username="admin", password_hash="hash", role=UserRole.ADMIN, permissions=[]
        )
        async with sessions() as db:
            catalog = await get_rule_presets(admin)
            assert len({p.id for p in catalog}) == 8
            assert all(p.version == 1 for p in catalog)
            # Catalog browsing and preparing/canceling a draft have no writes.
            draft = _draft("old-unwatched-movies")
            assert await db.scalar(select(func.count()).select_from(ReclaimRule)) == 0
            created = await create_rule(draft, admin, db)
            assert not created.enabled
            assert created.action and not created.action["auto_delete_enabled"]
            changed_definition = draft.definition
            changed_definition["root"]["children"][1]["value"] = 365
            updated = await update_rule(
                created.id,
                CleanupRuleUpdate(name="My edited rule", definition=changed_definition),
                admin,
                db,
            )
            assert updated.name == "My edited rule"
            # A fresh catalog still has its default; exporting/importing needs no preset metadata.
            assert (
                _draft("old-unwatched-movies").definition["root"]["children"][1][
                    "value"
                ]
                == 180
            )
            exported = updated.model_dump(exclude={"id", "created_at", "updated_at"})
            exported["name"] = "Imported copy"
            result = await import_rules(RuleImportPayload(rules=[exported]), admin, db)
            assert result.imported == 1
            assert not result.errors
            assert await db.scalar(select(func.count()).select_from(ReclaimRule)) == 2
            assert await db.scalar(select(func.count()).select_from(BackgroundJob)) == 0
        await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "names,expected", [(["alice"], True), ([], False), (None, False)]
)
@pytest.mark.parametrize("scope", ["movie_version", "series"])
def test_favorites_protection_matches_movies_and_shows(scope, names, expected):
    data = (
        {(MediaType.MOVIE, 1): names, (MediaType.SERIES, 900): names}
        if names is not None
        else None
    )
    token = FavoritesRuleDataResolver._ctx.set(
        FavoritesRuleDataResolver(data) if data is not None else None
    )
    try:
        if scope == "movie_version":
            assert _movie_match("protect-favorites-watchlists") is expected
        else:
            rule = _rule("protect-favorites-watchlists")
            rule.target_scope = "series"
            rule.media_type = MediaType.SERIES
            assert (
                evaluate_advanced_rule(rule, target_scope="series", series=_series())[0]
                is expected
            )
        action = _draft("protect-favorites-watchlists").action
        assert action["outcome"] == "protect"
        assert action["candidate"] is False
        assert action["media_server_action"] is None
    finally:
        FavoritesRuleDataResolver._ctx.reset(token)


@pytest.mark.parametrize(
    "status,completed,activity,expected",
    [
        ("ended", ["alice", "bob"], 91, True),
        ("continuing", ["alice", "bob"], 91, False),
        (None, ["alice", "bob"], 91, False),
        ("ended", ["alice"], 91, False),
        ("ended", None, 91, False),
        ("ended", ["alice", "bob"], 90, False),
        ("ended", ["alice", "bob"], None, False),
    ],
)
def test_finished_shows_require_ended_status_all_users_and_old_activity(
    status, completed, activity, expected
):
    series = _series()
    series.id = 1
    sonarr_token = SonarrRuleDataResolver._ctx.set(
        SonarrRuleDataResolver({1: {"sonarr.series_status": status}} if status else {})
    )
    completion_token = WatchCompletionResolver._ctx.set(
        WatchCompletionResolver(
            {("series", 900, None, None): completed} if completed is not None else {}
        )
    )
    playback_token = PlaybackHistoryResolver._ctx.set(
        PlaybackHistoryResolver(
            {("series", 1): {"playback.days_since_last_activity": activity}}
            if activity is not None
            else {}
        )
    )
    try:
        assert (
            evaluate_advanced_rule(
                _rule("finished-ended-shows"), target_scope="series", series=series
            )[0]
            is expected
        )
    finally:
        SonarrRuleDataResolver._ctx.reset(sonarr_token)
        WatchCompletionResolver._ctx.reset(completion_token)
        PlaybackHistoryResolver._ctx.reset(playback_token)


@pytest.mark.parametrize(
    "requested,watched,activity,expected",
    [
        (True, True, 31, True),
        (False, True, 31, False),
        (None, True, 31, False),
        (True, False, 31, False),
        (True, None, 31, False),
        (True, True, 30, False),
        (True, True, None, False),
    ],
)
def test_watched_requests_require_requester_completion_and_old_activity(
    requested, watched, activity, expected
):
    key = (MediaType.MOVIE, 1)
    seerr_token = SeerrRequestResolver._ctx.set(
        SeerrRequestResolver(
            requester_ids_by_key={key: ["1:7"] if requested else []}
            if requested is not None
            else {},
            requester_has_watched_by_key={key: watched} if watched is not None else {},
        )
    )
    playback_token = PlaybackHistoryResolver._ctx.set(
        PlaybackHistoryResolver(
            {("movie_version", 1): {"playback.days_since_last_activity": activity}}
            if activity is not None
            else {}
        )
    )
    try:
        assert _movie_match("requested-movies-watched") is expected
    finally:
        SeerrRequestResolver._ctx.reset(seerr_token)
        PlaybackHistoryResolver._ctx.reset(playback_token)


def test_saved_favorites_preset_retains_protection_outcome():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        admin = User(
            username="admin", password_hash="hash", role=UserRole.ADMIN, permissions=[]
        )
        async with sessions() as db:
            created = await create_rule(
                _draft("protect-favorites-watchlists"), admin, db
            )
            assert not created.enabled
            assert created.action["outcome"] == "protect"
            assert created.action["candidate"] is False
            assert created.action["media_server_action"] is None
            assert not created.action["auto_delete_enabled"]
            assert await db.scalar(select(func.count()).select_from(BackgroundJob)) == 0
        await engine.dispose()

    asyncio.run(run())
