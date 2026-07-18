import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    NativePlaybackAggregate,
    NativePlaybackUser,
    Season,
    Series,
    ServiceConfig,
    SupplementalMediaMatch,
)
from backend.enums import MediaType, Service
from backend.models.media import MediaWatchSnapshot, NativePlaybackSnapshot
from backend.services.media_watch_snapshot_cache import MediaWatchSnapshotCache


def test_legacy_plex_sync_state_requires_full_rebuild() -> None:
    now = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
    recent = (now - timedelta(hours=1)).isoformat()
    legacy_state = {
        "plex_last_viewed_at": recent,
        "plex_last_full_sync_at": recent,
    }

    assert MediaWatchSnapshotCache._plex_sync_state_requires_full_rebuild(
        legacy_state, now
    )


def test_current_plex_sync_state_remains_incremental_until_interval_expires() -> None:
    now = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
    recent = (now - timedelta(hours=1)).isoformat()
    current_state = {
        "format_version": MediaWatchSnapshotCache._PLEX_FORMAT_VERSION,
        "plex_last_viewed_at": recent,
        "plex_last_full_sync_at": recent,
    }

    assert not MediaWatchSnapshotCache._plex_sync_state_requires_full_rebuild(
        current_state, now
    )


def test_current_plex_sync_state_rebuilds_after_interval() -> None:
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    old = (now - MediaWatchSnapshotCache._PLEX_FULL_REBUILD_INTERVAL).isoformat()
    current_state = {
        "format_version": MediaWatchSnapshotCache._PLEX_FORMAT_VERSION,
        "plex_last_viewed_at": old,
        "plex_last_full_sync_at": old,
    }

    assert MediaWatchSnapshotCache._plex_sync_state_requires_full_rebuild(
        current_state, now
    )


def test_watch_snapshot_freshness_uses_persisted_service_state(
    tmp_path, monkeypatch
) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        cache = MediaWatchSnapshotCache()
        now = datetime.now(UTC)
        async with session_factory() as session:
            session.add(
                ServiceConfig(
                    service_type=Service.JELLYFIN,
                    base_url="http://jellyfin",
                    api_key="key",
                    enabled=True,
                    extra_settings={
                        cache._NATIVE_PLAYBACK_SYNC_STATE_KEY: {
                            cache._NATIVE_PLAYBACK_FORMAT_VERSION_KEY: (
                                cache._NATIVE_PLAYBACK_FORMAT_VERSION
                            ),
                            cache._NATIVE_PLAYBACK_AVAILABLE_KEY: True,
                            cache._NATIVE_PLAYBACK_LAST_SUCCESS_AT_KEY: (
                                cache._to_utc_iso(now)
                            ),
                        }
                    },
                )
            )
            await session.commit()

        @asynccontextmanager
        async def db_override():
            async with session_factory() as session:
                yield session

        refresh = AsyncMock(return_value=(True, None))
        monkeypatch.setattr(
            "backend.services.media_watch_snapshot_cache.async_db", db_override
        )
        monkeypatch.setattr(MediaWatchSnapshotCache, "refresh_snapshot", refresh)

        assert await cache.ensure_fresh_snapshot() == (True, None)
        refresh.assert_not_awaited()

        assert await cache.ensure_fresh_snapshot(force=True) == (True, None)
        refresh.assert_awaited_once()
        await engine.dispose()

    asyncio.run(run())


def test_episode_watch_rows_resolve_provider_episode_ids(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            series = Series(title="Example", tmdb_id=5920)
            session.add(series)
            await session.flush()
            season = Season(series_id=series.id, season_number=3)
            session.add(season)
            await session.flush()
            session.add(
                Episode(
                    season_id=season.id,
                    episode_number=2,
                    plex_rating_key="62906",
                )
            )
            await session.flush()

            rows, unmatched = await MediaWatchSnapshotCache._build_episode_watch_rows(
                session=session,
                source_service=Service.PLEX,
                source_service_config_id=4,
                snapshots=[
                    MediaWatchSnapshot(
                        media_type=MediaType.SERIES,
                        tmdb_id=5920,
                        watch_user_key="Alice",
                        last_watched_at=datetime(2026, 7, 4, tzinfo=UTC),
                        source_item_id="62906",
                    )
                ],
            )

        await engine.dispose()
        assert unmatched == 0
        assert len(rows) == 1
        assert rows[0].series_tmdb_id == 5920
        assert rows[0].season_number == 3
        assert rows[0].episode_number == 2
        assert rows[0].watch_user_key_normalized == "alice"

    asyncio.run(run())


def test_native_playback_aggregates_use_exact_movie_and_episode_ids(tmp_path) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'native.db'}")
        session_factory = async_sessionmaker(
            engine, expire_on_commit=False, autoflush=False
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            config = ServiceConfig(
                service_type=Service.JELLYFIN,
                base_url="http://jellyfin",
                api_key="key",
                enabled=True,
            )
            movie = Movie(title="Movie", tmdb_id=1)
            series = Series(title="Series", tmdb_id=2)
            session.add_all([config, movie, series])
            await session.flush()
            season = Season(series_id=series.id, season_number=1)
            session.add(season)
            await session.flush()
            version = MovieVersion(
                movie_id=movie.id,
                service=Service.PLEX,
                service_item_id="plex-movie-item",
                service_media_id="plex-movie-media",
                library_id="movies",
                library_name="Movies",
            )
            episode_one = Episode(
                season_id=season.id,
                episode_number=1,
                jellyfin_episode_id="episode-item-1",
            )
            episode_two = Episode(
                season_id=season.id,
                episode_number=2,
                jellyfin_episode_id="episode-item-2",
            )
            supplemental_match = SupplementalMediaMatch(
                source_service=Service.JELLYFIN,
                source_item_id="movie-item",
                media_type=MediaType.MOVIE,
                movie_id=movie.id,
            )
            session.add_all([version, episode_one, episode_two, supplemental_match])
            await session.flush()

            requester_rows = (
                await MediaWatchSnapshotCache._build_requester_snapshots_from_native(
                    session=session,
                    source_service=Service.JELLYFIN,
                    snapshots=[
                        NativePlaybackSnapshot(
                            source_item_id="movie-item",
                            provider_media_type="movie",
                            source_user_id="u1",
                            source_username="Alice",
                            play_count=2,
                            completed=True,
                            last_activity_at=datetime(2026, 7, 10, tzinfo=UTC),
                        )
                    ],
                )
            )
            assert len(requester_rows) == 1
            assert requester_rows[0].tmdb_id == movie.tmdb_id

            session.add_all(
                [
                    NativePlaybackUser(
                        source_service=Service.JELLYFIN,
                        source_service_config_id=config.id,
                        source_item_id="movie-item",
                        provider_media_type="movie",
                        source_user_id="u1",
                        source_username="Alice",
                        source_username_normalized="alice",
                        play_count=2,
                        completed=True,
                        last_activity_at=datetime(2026, 7, 10, tzinfo=UTC),
                        refreshed_at=datetime(2026, 7, 11, tzinfo=UTC),
                    ),
                    NativePlaybackUser(
                        source_service=Service.JELLYFIN,
                        source_service_config_id=config.id,
                        source_item_id="episode-item-1",
                        provider_media_type="episode",
                        source_user_id="u1",
                        source_username="Alice",
                        source_username_normalized="alice",
                        play_count=1,
                        completed=True,
                        last_activity_at=datetime(2026, 7, 11, tzinfo=UTC),
                        refreshed_at=datetime(2026, 7, 11, tzinfo=UTC),
                    ),
                    NativePlaybackUser(
                        source_service=Service.JELLYFIN,
                        source_service_config_id=config.id,
                        source_item_id="episode-item-2",
                        provider_media_type="episode",
                        source_user_id="u1",
                        source_username="Alice",
                        source_username_normalized="alice",
                        play_count=2,
                        completed=True,
                        last_activity_at=datetime(2026, 7, 12, tzinfo=UTC),
                        refreshed_at=datetime(2026, 7, 12, tzinfo=UTC),
                    ),
                ]
            )
            await MediaWatchSnapshotCache._rebuild_native_playback_aggregates(session)
            await session.flush()
            rows = (
                await session.execute(
                    select(
                        NativePlaybackAggregate.target_scope,
                        NativePlaybackAggregate.target_id,
                        NativePlaybackAggregate.play_count,
                        NativePlaybackAggregate.unique_user_count,
                    )
                )
            ).all()

        await engine.dispose()
        assert set(rows) == {
            ("movie_version", version.id, 2, 1),
            ("series", series.id, 3, 1),
            ("season", season.id, 3, 1),
            ("episode", episode_one.id, 1, 1),
            ("episode", episode_two.id, 2, 1),
        }

    asyncio.run(run())
