from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.routes.v1 import router
from backend.core.api_tokens import generate_api_token
from backend.database import Base, get_db
from backend.database.models import (
    ApiToken,
    BackgroundJob,
    DeleteRequest,
    Episode,
    GeneralSettings,
    Movie,
    MovieVersion,
    ReclaimCandidate,
    ReclaimRule,
    ScheduledDeletion,
    Season,
    Series,
    SeriesServiceRef,
    ServiceConfig,
    SupplementalMediaMatch,
    User,
)
from backend.enums import (
    BackgroundJobStatus,
    BackgroundJobType,
    MediaType,
    ProtectionRequestStatus,
    Service,
)

pytestmark = pytest.mark.anyio
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            plaintext, prefix, digest = generate_api_token()
            db.add(
                ApiToken(
                    name="Reader",
                    token_prefix=prefix,
                    token_hash=digest,
                    scopes=["candidates:read"],
                )
            )
            await db.commit()

            async def override_db():
                yield db

            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[get_db] = override_db
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": f"Bearer {plaintext}"},
            ) as client:
                yield client, db
    finally:
        await engine.dispose()


async def add_candidate(db, *, movie=None, rule=None, **kwargs):
    if movie is None:
        count = await db.scalar(select(func.count(Movie.id)))
        movie = Movie(title="Cleanup", tmdb_id=100 + count, size=1000)
        db.add(movie)
    if rule is None:
        rule = ReclaimRule(
            name="Cleanup",
            media_type=MediaType.MOVIE,
            action={"auto_delete_enabled": True, "auto_delete_delay_days": 2},
        )
        db.add(rule)
    await db.flush()
    candidate = ReclaimCandidate(
        media_type=MediaType.MOVIE,
        movie_id=movie.id,
        matched_rule_ids=[rule.id],
        matched_criteria={},
        reason="test",
        **kwargs,
    )
    candidate.created_at = NOW
    db.add(candidate)
    await db.flush()
    return candidate


async def add_schedule(
    db, *, media_id=500, state="scheduled", delete_request_id=None, **target_fields
):
    job = BackgroundJob(
        job_type=BackgroundJobType.CANDIDATE_FILE_OP,
        scheduled_at=NOW + timedelta(days=3),
    )
    db.add(job)
    await db.flush()
    schedule = ScheduledDeletion(
        scheduled_at=job.scheduled_at,
        job_id=job.id,
        state=state,
        delete_request_id=delete_request_id,
        created_by_username="private username",
        target={
            "media_type": "movie",
            "media_id": media_id,
            "scope": "movie",
            "title": "Scheduled",
            "year": 2026,
            "tmdb_id": 200,
            "operation": "delete",
            "estimated_space_bytes": 2000,
            "inventory": {"files": {"/private/media.mkv": 2000}},
            "version_path": "/private/media.mkv",
            "routing_settings": {"api_key": "secret"},
            **target_fields,
        },
    )
    db.add(schedule)
    await db.flush()
    return schedule, job


async def test_combines_sources_and_exposes_only_public_fields(client_db):
    client, db = client_db
    candidate = await add_candidate(db)
    manual, _ = await add_schedule(db, estimated_space_bytes=0)
    requested_movie = Movie(title="Requested", tmdb_id=300)
    requester = User(username="requester", password_hash="unused")
    db.add_all([requested_movie, requester])
    await db.flush()
    request = DeleteRequest(
        media_type=MediaType.MOVIE,
        movie_id=requested_movie.id,
        requested_by_user_id=requester.id,
        status=ProtectionRequestStatus.APPROVED,
    )
    db.add(request)
    await db.flush()
    approval, _ = await add_schedule(
        db, media_id=requested_movie.id, delete_request_id=request.id
    )
    approval.scheduled_at = NOW + timedelta(days=1)
    await db.commit()

    response = await client.get("/api/v1/leaving-soon")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    items = payload["items"]
    assert [item["source"] for item in items] == [
        "delete_request",
        "candidate",
        "manual",
    ]
    assert items[0]["delete_request_id"] == request.id
    assert items[0]["scheduled_deletion_id"] == approval.id
    assert items[0]["candidate_id"] is None
    assert items[1]["candidate_id"] == candidate.id
    assert items[1]["size_bytes"] == 1000
    assert items[2]["scheduled_deletion_id"] == manual.id
    assert items[2]["size_bytes"] == 0
    assert all(item["delete_at"].endswith("Z") for item in items)
    assert "/private" not in response.text
    assert "secret" not in response.text
    assert "private username" not in response.text


async def test_schedule_replaces_overlapping_candidate_but_preserves_other_versions(
    client_db,
):
    client, db = client_db
    first = await add_candidate(db, movie_version_id=10)
    other = await add_candidate(
        db, movie=await db.get(Movie, first.movie_id), movie_version_id=11
    )
    schedule, _ = await add_schedule(
        db,
        media_id=first.movie_id,
        scope="version",
        movie_version_id=10,
        candidate_id=first.id,
    )
    await db.commit()
    payload = (await client.get("/api/v1/leaving-soon")).json()
    assert payload["total"] == 2
    assert [item["candidate_id"] for item in payload["items"]] == [other.id, first.id]
    assert payload["items"][1]["scheduled_deletion_id"] == schedule.id


async def test_policy_deadlines_and_terminal_schedules(client_db):
    client, db = client_db
    canceled = await add_candidate(db, auto_delete_cancelled_at=NOW)
    disabled = await add_candidate(db)
    disabled.matched_rule_ids = []
    postponed = await add_candidate(
        db, auto_delete_postponed_until=NOW + timedelta(days=10)
    )
    reset = await add_candidate(
        db, auto_delete_timer_started_at=NOW + timedelta(days=1)
    )
    eligible = await add_candidate(db)
    eligible.created_at = NOW - timedelta(days=10)
    for state in ("canceled", "completed"):
        await add_schedule(db, state=state)
    await db.commit()

    items = (await client.get("/api/v1/leaving-soon")).json()["items"]
    assert [item["candidate_id"] for item in items] == [
        eligible.id,
        reset.id,
        postponed.id,
    ]
    assert [item["state"] for item in items] == ["eligible", "scheduled", "postponed"]
    assert datetime.fromisoformat(items[1]["delete_at"]) == NOW + timedelta(days=3)
    assert datetime.fromisoformat(items[2]["delete_at"]) == NOW + timedelta(days=10)
    assert canceled.id not in [item["candidate_id"] for item in items]


@pytest.mark.parametrize("state", ["scheduled", "running", "blocked", "failed"])
async def test_active_schedule_state_is_preserved_and_holds_cleanup(client_db, state):
    client, db = client_db
    candidate = await add_candidate(db)
    schedule, job = await add_schedule(db, media_id=candidate.movie_id, state=state)
    if state == "running":
        job.status = BackgroundJobStatus.RUNNING
    await db.commit()
    payload = (await client.get("/api/v1/leaving-soon")).json()
    assert payload["total"] == 1
    assert payload["items"][0]["state"] == state
    assert payload["items"][0]["scheduled_deletion_id"] == schedule.id


async def test_missing_execution_job_is_reported_as_failed(client_db):
    client, db = client_db
    schedule, _ = await add_schedule(db)
    schedule.job_id = None
    await db.commit()
    payload = (await client.get("/api/v1/leaving-soon")).json()
    assert payload["items"][0]["state"] == "failed"


async def test_profile_changes_do_not_claim_media_is_leaving(client_db):
    client, db = client_db
    profile = {"arr_action": "change_quality_profile", "quality_profile_id": 2}
    candidate = await add_candidate(db)
    rule = await db.get(ReclaimRule, candidate.matched_rule_ids[0])
    rule.action = {**rule.action, **profile}
    await add_schedule(db, rule_actions={str(rule.id): profile})
    await db.commit()
    payload = (await client.get("/api/v1/leaving-soon")).json()
    assert payload["total"] == 0


async def test_tv_schedule_keeps_snapshot_scope_without_a_candidate(client_db):
    client, db = client_db
    schedule, _ = await add_schedule(
        db,
        media_type="series",
        scope="episode",
        season_id=10,
        season_number=2,
        episode_id=11,
        episode_number=7,
        estimated_space_bytes=None,
        operation="move",
    )
    await db.commit()
    item = (await client.get("/api/v1/leaving-soon")).json()["items"][0]
    assert item["scheduled_deletion_id"] == schedule.id
    assert item["series_id"] == schedule.target["media_id"]
    assert item["scope"] == "episode"
    assert item["season_id"] == 10
    assert item["season_number"] == 2
    assert item["episode_id"] == 11
    assert item["episode_number"] == 7
    assert item["size_bytes"] is None
    assert item["delete_operation"] == "move"


async def test_tv_server_items_respect_series_and_child_scopes(client_db):
    client, db = client_db
    series = Series(title="Show", tmdb_id=0)
    main = ServiceConfig(
        service_type=Service.JELLYFIN,
        name="Jellyfin",
        base_url="http://private",
        api_key="secret",
        is_main=True,
    )
    linked = ServiceConfig(
        service_type=Service.EMBY,
        name="Emby",
        base_url="http://private",
        api_key="secret",
    )
    rule = ReclaimRule(
        name="Cleanup",
        media_type=MediaType.SERIES,
        action={"auto_delete_enabled": True},
    )
    db.add_all([series, main, linked, rule])
    await db.flush()
    season = Season(
        series_id=series.id, season_number=1, jellyfin_season_id="main-season"
    )
    db.add_all(
        [
            season,
            SeriesServiceRef(
                series_id=series.id,
                service=Service.JELLYFIN,
                service_id="main-series",
                library_id="tv",
                library_name="TV",
            ),
        ]
    )
    await db.flush()
    episode = Episode(
        season_id=season.id, episode_number=2, jellyfin_episode_id="main-episode"
    )
    db.add(episode)
    await db.flush()
    for scope, season_id, episode_id in [
        ("series", None, None),
        ("season", season.id, None),
        ("episode", season.id, episode.id),
    ]:
        db.add(
            SupplementalMediaMatch(
                source_service=Service.EMBY,
                source_service_config_id=linked.id,
                source_item_id=f"linked-{scope}",
                media_type=MediaType.SERIES,
                series_id=series.id,
                season_id=season_id,
                episode_id=episode_id,
            )
        )
        db.add(
            ReclaimCandidate(
                media_type=MediaType.SERIES,
                series_id=series.id,
                season_id=season_id,
                episode_id=episode_id,
                matched_rule_ids=[rule.id],
                matched_criteria={},
                reason="test",
            )
        )
    await db.commit()

    response = await client.get("/api/v1/leaving-soon")
    assert response.status_code == 200
    by_scope = {
        item["scope"]: item["media_server_items"] for item in response.json()["items"]
    }
    assert set(by_scope) == {"series", "season", "episode"}
    for scope, refs in by_scope.items():
        assert [
            (
                ref["source_service_config_id"],
                ref["source_item_id"],
                ref["source_media_id"],
            )
            for ref in refs
        ] == [(main.id, f"main-{scope}", None), (linked.id, f"linked-{scope}", None)]


@pytest.mark.parametrize("scope", ["movie", "version", "series", "season", "episode"])
@pytest.mark.parametrize("size_source", ["estimate", "fallback", "zero", "unknown"])
async def test_scoped_metadata_and_sizes(client_db, scope, size_source):
    client, db = client_db
    movie = Movie(title="Film", tmdb_id=400, size=10000)
    series = Series(title="Show", tmdb_id=401, size=20000)
    rule = ReclaimRule(
        name="Move",
        media_type=MediaType.MOVIE,
        action={"auto_delete_enabled": True, "move_instead_of_delete": True},
    )
    db.add_all(
        [
            movie,
            series,
            rule,
            GeneralSettings(
                auto_delete_movie_delay_days=4, auto_delete_series_delay_days=5
            ),
        ]
    )
    await db.flush()
    version = MovieVersion(
        movie_id=movie.id,
        service=Service.PLEX,
        service_item_id="film",
        service_media_id="file",
        library_id="1",
        library_name="Movies",
        size=3000,
    )
    season = Season(series_id=series.id, season_number=2, size=4000)
    db.add_all([version, season])
    await db.flush()
    episode = Episode(season_id=season.id, episode_number=7, size=5000)
    db.add(episode)
    await db.flush()
    scoped = {
        "movie": movie,
        "version": version,
        "series": series,
        "season": season,
        "episode": episode,
    }[scope]
    expected = scoped.size
    if size_source == "zero":
        scoped.size = expected = 0
    elif size_source == "unknown":
        expected = None
        if scope != "version":
            scoped.size = None
    is_movie = scope in ("movie", "version")
    candidate = ReclaimCandidate(
        media_type=MediaType.MOVIE if is_movie else MediaType.SERIES,
        movie_id=movie.id if is_movie else None,
        movie_version_id=(version.id if size_source != "unknown" else version.id + 1000)
        if scope == "version"
        else None,
        series_id=series.id if not is_movie else None,
        season_id=season.id if scope in ("season", "episode") else None,
        episode_id=episode.id if scope == "episode" else None,
        matched_rule_ids=[rule.id],
        matched_criteria={},
        reason="test",
        estimated_space_bytes=123 if size_source == "estimate" else None,
    )
    candidate.created_at = NOW
    db.add(candidate)
    await db.commit()

    response = await client.get("/api/v1/leaving-soon")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["scope"] == scope
    assert item["size_bytes"] == (123 if size_source == "estimate" else expected)
    assert item["delete_operation"] == "move"
    assert item["tmdb_id"] == (400 if is_movie else 401)
    assert datetime.fromisoformat(item["delete_at"]) == NOW + timedelta(
        days=4 if is_movie else 5
    )
    assert item["movie_version_id"] == candidate.movie_version_id
    assert item["season_number"] == (2 if scope in ("season", "episode") else None)
    assert item["episode_number"] == (7 if scope == "episode" else None)


async def test_filters_precede_pagination_and_ties_are_stable(client_db):
    client, db = client_db
    for _ in range(3):
        await add_candidate(db)
    await add_schedule(db)
    await db.commit()
    first = (await client.get("/api/v1/leaving-soon", params={"per_page": 2})).json()
    second = (
        await client.get("/api/v1/leaving-soon", params={"per_page": 2, "page": 2})
    ).json()
    assert first["total"] == second["total"] == 4
    assert first["total_pages"] == 2
    assert (
        len({item["candidate_id"] for item in first["items"] + second["items"][:1]})
        == 3
    )
    deadline = (NOW + timedelta(days=2)).isoformat()
    filtered = (
        await client.get(
            "/api/v1/leaving-soon",
            params={
                "media_type": "movie",
                "delete_after": deadline,
                "delete_before": deadline,
                "per_page": 2,
            },
        )
    ).json()
    assert filtered["total"] == 3
    assert len(filtered["items"]) == 2
    assert filtered["items"] == first["items"]
    assert (
        await client.get("/api/v1/leaving-soon", params={"media_type": "series"})
    ).json()["total"] == 0
    assert (await client.get("/api/v1/leaving-soon", params={"page": 100})).json()[
        "items"
    ] == []


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"per_page": 201},
        {"media_type": "invalid"},
        {"delete_before": "2026-12-01T12:00:00"},
        {
            "delete_after": "2026-12-02T12:00:00Z",
            "delete_before": "2026-12-01T12:00:00Z",
        },
    ],
)
async def test_invalid_queries(client_db, params):
    client, _ = client_db
    assert (await client.get("/api/v1/leaving-soon", params=params)).status_code == 422


async def test_authentication_scope_discovery_and_schema(client_db):
    client, db = client_db
    assert (
        await client.get("/api/v1/leaving-soon", headers={"Authorization": ""})
    ).status_code == 401
    plaintext, prefix, digest = generate_api_token()
    token = ApiToken(
        name="Other", token_prefix=prefix, token_hash=digest, scopes=["media:read"]
    )
    db.add(token)
    await db.commit()
    headers = {"Authorization": f"Bearer {plaintext}"}
    assert (
        await client.get("/api/v1/leaving-soon", headers=headers)
    ).status_code == 403
    token.scopes = ["candidates:manage"]
    await db.commit()
    empty = await client.get("/api/v1/leaving-soon", headers=headers)
    assert empty.status_code == 200
    assert empty.json()["total"] == 0
    assert (await client.get("/api/v1")).json()["resources"][
        "leaving_soon"
    ] == "/api/v1/leaving-soon"
    schema = (await client.get("/api/v1/openapi.json")).json()
    assert schema["paths"]["/api/v1/leaving-soon"]["get"]["security"] == [
        {"HTTPBearer": []}
    ]
