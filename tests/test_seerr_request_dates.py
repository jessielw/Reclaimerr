from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.core.rule_engine import (
    TARGET_EPISODE,
    TARGET_MOVIE_VERSION,
    TARGET_SEASON,
    TARGET_SERIES,
    SeerrRequestResolver,
    _build_context,
    validate_rule_definition,
)
from backend.core.service_manager import service_manager
from backend.database.models import Episode, Movie, MovieVersion, Season, Series
from backend.enums import MediaType, SeerrRequestStatus, Service
from backend.models.services.seerr import SeerrRequest, SeerrRequestedSeason, SeerrUser
from backend.services.seerr_cache import SeerrSnapshotCache


def _request(
    *, request_id: int, status: SeerrRequestStatus, created_at: datetime, tmdb_id: int
) -> SeerrRequest:
    return SeerrRequest(
        id=request_id,
        status=status,
        media_id=request_id,
        media_type=MediaType.MOVIE,
        tmdb_id=tmdb_id,
        created_at=created_at,
        requested_by_id=request_id,
        is_4k=False,
    )


def test_seerr_snapshot_uses_latest_pending_or_approved_request() -> None:
    async def run() -> None:
        pending_at = datetime(2026, 1, 1, tzinfo=UTC)
        approved_at = datetime(2026, 2, 1, tzinfo=UTC)
        declined_at = datetime(2026, 3, 1, tzinfo=UTC)
        fake_client = SimpleNamespace(
            get_all_users=AsyncMock(return_value=[]),
            get_all_requests=AsyncMock(
                return_value=[
                    _request(
                        request_id=1,
                        status=SeerrRequestStatus.PENDING,
                        created_at=pending_at,
                        tmdb_id=10,
                    ),
                    _request(
                        request_id=2,
                        status=SeerrRequestStatus.APPROVED,
                        created_at=approved_at,
                        tmdb_id=10,
                    ),
                    _request(
                        request_id=3,
                        status=SeerrRequestStatus.DECLINED,
                        created_at=declined_at,
                        tmdb_id=10,
                    ),
                    _request(
                        request_id=4,
                        status=SeerrRequestStatus.DECLINED,
                        created_at=declined_at,
                        tmdb_id=20,
                    ),
                ]
            ),
        )
        cache = SeerrSnapshotCache()
        with patch.object(service_manager, "_seerr", fake_client):
            snapshot, error = await cache.get_request_snapshot(
                require_fresh=True, allow_stale_on_failure=False
            )

        assert error is None
        assert snapshot is not None
        assert snapshot.latest_active_request_at_by_key == {
            (MediaType.MOVIE, 10): approved_at
        }

    asyncio.run(run())


def test_seerr_snapshot_enriches_requester_identity_from_user_directory() -> None:
    async def run() -> None:
        request = SeerrRequest(
            id=1,
            status=SeerrRequestStatus.COMPLETED,
            media_id=1,
            media_type=MediaType.SERIES,
            tmdb_id=201836,
            created_at=datetime(2025, 10, 21, tzinfo=UTC),
            requested_by_id=16,
            is_4k=False,
            raw={"requestedBy": {"id": 16}},
        )
        fake_client = SimpleNamespace(
            get_all_requests=AsyncMock(return_value=[request]),
            get_all_users=AsyncMock(
                return_value=[
                    SeerrUser(
                        id=16,
                        username="nwilson3000",
                        display_name="N Wilson",
                        email="nwilson@example.com",
                    )
                ]
            ),
        )
        cache = SeerrSnapshotCache()
        with patch.object(service_manager, "_seerr", fake_client):
            snapshot, error = await cache.get_request_snapshot(
                require_fresh=True, allow_stale_on_failure=False
            )

        assert error is None
        assert snapshot is not None
        assert snapshot.requester_identity_keys_by_user_id[16] == {
            "nwilson3000",
            "n wilson",
            "nwilson@example.com",
        }
        assert snapshot.requester_users_by_id[16].display_name == "N Wilson"
        assert snapshot.requester_users_by_id[16].username == "nwilson3000"

    asyncio.run(run())


def test_seerr_snapshot_preserves_requested_seasons_and_excludes_declined() -> None:
    async def run() -> None:
        requested_at = datetime(2026, 1, 1, tzinfo=UTC)
        declined_at = datetime(2026, 2, 1, tzinfo=UTC)
        accepted = SeerrRequest(
            id=1,
            status=SeerrRequestStatus.COMPLETED,
            media_id=1,
            media_type=MediaType.SERIES,
            tmdb_id=5920,
            created_at=requested_at,
            requested_by_id=101,
            is_4k=False,
            requested_seasons=(SeerrRequestedSeason(3, requested_at),),
        )
        declined = SeerrRequest(
            id=2,
            status=SeerrRequestStatus.DECLINED,
            media_id=1,
            media_type=MediaType.SERIES,
            tmdb_id=5920,
            created_at=declined_at,
            requested_by_id=202,
            is_4k=False,
            requested_seasons=(SeerrRequestedSeason(4, declined_at),),
        )
        cache = SeerrSnapshotCache()
        fake_client = SimpleNamespace(
            get_all_requests=AsyncMock(return_value=[accepted, declined]),
            get_all_users=AsyncMock(return_value=[]),
        )
        with patch.object(service_manager, "_seerr", fake_client):
            snapshot, error = await cache.get_request_snapshot(
                require_fresh=True, allow_stale_on_failure=False
            )

        assert error is None
        assert snapshot is not None
        assert snapshot.requester_ids_by_series_season == {(5920, 3): {101}}
        assert snapshot.first_request_at_by_series_season_user == {
            (5920, 3): {101: requested_at}
        }

    asyncio.run(run())


def test_seerr_request_date_fields_are_valid_for_all_media_scopes() -> None:
    definitions = (
        {
            "version": 1,
            "root": {
                "type": "condition",
                "field": "seerr.last_requested_at",
                "operator": "on_or_after",
                "value": "2026-01-01",
            },
        },
        {
            "version": 1,
            "root": {
                "type": "condition",
                "field": "seerr.days_since_last_requested",
                "operator": "greater_than_or_equal",
                "value": 30,
            },
        },
    )

    for scope in (TARGET_MOVIE_VERSION, TARGET_SERIES, TARGET_SEASON, TARGET_EPISODE):
        for definition in definitions:
            validate_rule_definition(definition, target_scope=scope)


def test_rule_context_exposes_latest_active_seerr_request_for_every_scope() -> None:
    requested_at = datetime.now(UTC) - timedelta(days=40)
    movie = Movie(title="Movie", tmdb_id=1)
    version = MovieVersion(
        movie_id=1,
        service=Service.JELLYFIN,
        service_item_id="item",
        service_media_id="media",
        library_id="library",
        library_name="Movies",
    )
    series = Series(title="Series", tmdb_id=2)
    season = Season(series_id=2, season_number=1)
    episode = Episode(season_id=1, episode_number=1)
    SeerrRequestResolver(
        latest_active_request_at_by_key={
            (MediaType.MOVIE, 1): requested_at,
            (MediaType.SERIES, 2): requested_at,
        }
    ).activate()

    contexts = (
        _build_context(
            TARGET_MOVIE_VERSION, movie, version, None, None, compute_disk=False
        ),
        _build_context(TARGET_SERIES, None, None, series, None, compute_disk=False),
        _build_context(TARGET_SEASON, None, None, series, season, compute_disk=False),
        _build_context(
            TARGET_EPISODE,
            None,
            None,
            series,
            season,
            episode,
            compute_disk=False,
        ),
    )

    assert all(
        context["seerr.last_requested_at"] == requested_at for context in contexts
    )
    assert all(context["seerr.days_since_last_requested"] == 40 for context in contexts)


def test_rule_context_propagates_series_requester_watched_to_every_tv_scope() -> None:
    series = Series(title="Series", tmdb_id=5920)
    season = Season(series_id=1, season_number=3)
    episode = Episode(season_id=1, episode_number=23)
    SeerrRequestResolver(
        requester_has_watched_by_target={
            (TARGET_SERIES, 5920, None, None): True,
            (TARGET_SEASON, 5920, 3, None): True,
            (TARGET_EPISODE, 5920, 3, 23): True,
        }
    ).activate()

    contexts = (
        _build_context(TARGET_SERIES, None, None, series, None, compute_disk=False),
        _build_context(TARGET_SEASON, None, None, series, season, compute_disk=False),
        _build_context(
            TARGET_EPISODE,
            None,
            None,
            series,
            season,
            episode,
            compute_disk=False,
        ),
    )

    assert all(context["seerr.requester_has_watched"] is True for context in contexts)


def test_watch_bar_is_the_earliest_request_not_the_latest() -> None:
    """A second request must not invalidate a watch that already happened.

    Seerr writes a separate request row for a 4K copy and for every re-request
    of an airing season. Keeping the newest of those moved the "watched after
    requesting" bar past plays the requester had already finished, so a season
    they demonstrably completed reported as unwatched -- and an `is false`
    cleanup rule deletes on that.
    """

    async def run() -> None:
        first = datetime(2025, 1, 1, tzinfo=UTC)
        reissued = datetime(2026, 6, 1, tzinfo=UTC)
        original = SeerrRequest(
            id=1,
            status=SeerrRequestStatus.COMPLETED,
            media_id=1,
            media_type=MediaType.SERIES,
            tmdb_id=5920,
            created_at=first,
            requested_by_id=101,
            is_4k=False,
            requested_seasons=(SeerrRequestedSeason(6, first),),
        )
        four_k = SeerrRequest(
            id=2,
            status=SeerrRequestStatus.APPROVED,
            media_id=1,
            media_type=MediaType.SERIES,
            tmdb_id=5920,
            created_at=reissued,
            requested_by_id=101,
            is_4k=True,
            requested_seasons=(SeerrRequestedSeason(6, reissued),),
        )
        cache = SeerrSnapshotCache()
        fake_client = SimpleNamespace(
            get_all_requests=AsyncMock(return_value=[original, four_k]),
            get_all_users=AsyncMock(return_value=[]),
        )
        with patch.object(service_manager, "_seerr", fake_client):
            snapshot, error = await cache.get_request_snapshot(
                require_fresh=True, allow_stale_on_failure=False
            )

        assert error is None
        assert snapshot is not None
        media_key = (MediaType.SERIES, 5920)
        assert snapshot.first_request_at_by_key_user[media_key] == {101: first}
        assert snapshot.first_request_at_by_series_season_user[(5920, 6)] == {
            101: first
        }
        # Request *age* is a different question and still tracks the newest.
        assert snapshot.latest_active_request_at_by_key[media_key] == reissued
        assert snapshot.latest_active_request_at_by_series_season[(5920, 6)] == reissued

    asyncio.run(run())
