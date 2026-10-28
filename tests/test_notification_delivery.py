from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    Movie,
    NotificationSetting,
    ReclaimCandidate,
    Series,
    User,
)
from backend.enums import MediaType, NotificationType, UserRole
from backend.services import notifications

_SHARED_CHAT = "tgram://123456:ABCdef/987654"


@pytest.fixture
async def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Two admins whose destinations both point at one Telegram chat."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as session:
        for index, url in enumerate((_SHARED_CHAT, f"{_SHARED_CHAT}/", _SHARED_CHAT)):
            user = User(
                username=f"admin{index}",
                password_hash="x",
                role=UserRole.ADMIN,
                is_active=True,
            )
            session.add(user)
            await session.flush()
            session.add(
                NotificationSetting(
                    user_id=user.id,
                    name=f"telegram {index}",
                    url=url,
                    enabled=True,
                    admin_message=True,
                    new_cleanup_candidates=True,
                )
            )
        # someone on a different chat must still hear about it
        other = User(
            username="other",
            password_hash="x",
            role=UserRole.ADMIN,
            is_active=True,
        )
        session.add(other)
        await session.flush()
        session.add(
            NotificationSetting(
                user_id=other.id,
                name="discord",
                url="discord://webhook_id/webhook_token",
                enabled=True,
                admin_message=True,
                new_cleanup_candidates=True,
            )
        )
        await session.commit()

    calls: list[dict[str, Any]] = []

    async def fake_send(**kwargs: Any) -> bool:
        calls.append(kwargs)
        return True

    monkeypatch.setattr(notifications, "async_db", session_maker)
    monkeypatch.setattr(notifications, "send_notification", fake_send)
    return calls


@pytest.mark.anyio
async def test_admins_sharing_a_chat_get_one_message(
    sent: list[dict[str, Any]],
) -> None:
    results = await notifications.notify_admins(
        NotificationType.ADMIN_MESSAGE, "Heads up", "Something happened"
    )

    assert sorted(call["url"].rstrip("/") for call in sent) == sorted(
        [_SHARED_CHAT, "discord://webhook_id/webhook_token"]
    )
    assert results == {"sent": 2, "failed": 0}


@pytest.mark.anyio
async def test_all_users_fan_out_is_deduplicated_too(
    sent: list[dict[str, Any]],
) -> None:
    await notifications.notify_all_users(
        NotificationType.NEW_CLEANUP_CANDIDATES,
        "New Cleanup Candidates Found",
        "There are 2 new cleanup candidates",
        context={"created_count": 2, "total_reclaimable_bytes": 0, "candidates": []},
    )

    assert len(sent) == 2


@pytest.mark.anyio
async def test_cleanup_context_carries_episode_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.database.models import Episode, Season

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(notifications, "async_db", session_maker)

    started = datetime.now(UTC)
    async with session_maker() as session:
        series = Series(title="Carrie", tmdb_id=1, year=2026)
        session.add(series)
        await session.flush()
        season = Season(series_id=series.id, season_number=1)
        session.add(season)
        await session.flush()
        for number in (5, 6):
            episode = Episode(season_id=season.id, episode_number=number)
            session.add(episode)
            await session.flush()
            session.add(
                ReclaimCandidate(
                    media_type=MediaType.SERIES,
                    series_id=series.id,
                    season_id=season.id,
                    episode_id=episode.id,
                    estimated_space_bytes=500_000_000,
                    matched_rule_ids=[],
                    matched_criteria={},
                    reason="Old episodes",
                    reason_data=[
                        {
                            "rule_name": "Old episodes",
                            "conditions": [
                                {
                                    "field": "library.id",
                                    "operator": "contains_any",
                                    "expected": ["b539bda9"],
                                    "display": "Library matches any b539bda9",
                                }
                            ],
                        }
                    ],
                )
            )
        await session.commit()

    context = await notifications.build_cleanup_notification_context(
        created_count=2, created_since=started
    )

    numbers = sorted(c["episode_number"] for c in context["candidates"])
    assert numbers == [5, 6]
    assert {c["media_id"] for c in context["candidates"]} == {series.id}
    assert len(notifications._group_cleanup_candidates(context["candidates"])) == 1
    # a rule whose only condition is library scoping falls back to its name
    assert context["candidates"][0]["reasons"] == [
        {"rule": "Old episodes", "full": [], "short": []}
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("media_type", [MediaType.MOVIE, MediaType.SERIES])
async def test_cleanup_keeps_same_named_media_separate(
    monkeypatch: pytest.MonkeyPatch, media_type: MediaType
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(notifications, "async_db", session_maker)
        started = datetime.now(UTC)
        async with session_maker() as session:
            for tmdb_id in (100, 200):
                model = Movie if media_type == MediaType.MOVIE else Series
                media = model(title="Same Name", year=2020, tmdb_id=tmdb_id)
                session.add(media)
                await session.flush()
                session.add(
                    ReclaimCandidate(
                        media_type=media_type,
                        movie_id=media.id if media_type == MediaType.MOVIE else None,
                        series_id=media.id if media_type == MediaType.SERIES else None,
                        estimated_space_bytes=1024**3,
                        matched_rule_ids=[],
                        matched_criteria={},
                        reason="Test",
                        reason_data=[],
                    )
                )
            await session.commit()

        context = await notifications.build_cleanup_notification_context(
            created_count=2, created_since=started
        )
        assert len({c["media_id"] for c in context["candidates"]}) == 2
        _, body, _ = notifications._compose_body(
            notification_type=NotificationType.NEW_CLEANUP_CANDIDATES,
            setting=NotificationSetting(user_id=1, enabled=True),
            fallback_title="New candidates",
            fallback_message="",
            context=context,
        )
        assert body.count("**Same Name (2020)**") == 2
        assert "versions" not in body
    finally:
        await engine.dispose()
