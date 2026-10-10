from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    GeneralSettings,
    MediaUserIdentity,
    Movie,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
    ServiceConfig,
    TaskSchedule,
    User,
)
from backend.enums import (
    MediaType,
    NotificationType,
    ScheduleType,
    Service,
    Task,
    UserRole,
)
from backend.models.services.seerr import SeerrUser
from backend.services import requester_warnings
from backend.services.notifications import _notification_type_to_field
from backend.services.seerr_cache import SeerrRequestSnapshot

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
# the default movie delay is 14 days, so this deadline is 4 days out
INSIDE_WINDOW = NOW - timedelta(days=10)


def _snapshot(
    requesters: dict[str, SeerrUser],
    by_key: dict[tuple[MediaType, int], set[str]],
    by_season: dict[tuple[int, int], set[str]] | None = None,
) -> SeerrRequestSnapshot:
    return SeerrRequestSnapshot(
        requester_ids_by_key=by_key,
        first_request_at_by_key_user={},
        requester_identity_keys_by_user_id={},
        latest_active_request_at_by_key={},
        requester_users_by_id=requesters,
        requester_ids_by_series_season=by_season or {},
    )


BOB = SeerrUser(id=7, username="bob", display_name="Bob", plex_username="BobPlex")
STRANGER = SeerrUser(id=8, username="ghost", display_name="Bob")


class Harness:
    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, snapshot: SeerrRequestSnapshot | None
    ) -> None:
        self.sent: list[tuple[int, str, dict[str, Any]]] = []

        async def fake_snapshot(**_kwargs: Any) -> tuple[Any, str | None]:
            return snapshot, None if snapshot else "Seerr is down"

        async def fake_notify(
            user_id: int,
            notification_type: NotificationType,
            title: str,
            message: str,
            context: dict[str, Any] | None = None,
            **_kwargs: Any,
        ) -> dict[str, int]:
            assert notification_type is NotificationType.REQUESTER_LEAVING_SOON
            self.sent.append((user_id, message, context or {}))
            return {"sent": 1, "failed": 0}

        monkeypatch.setattr(
            requester_warnings,
            "seerr_snapshot_cache",
            SimpleNamespace(get_request_snapshot=fake_snapshot),
        )
        monkeypatch.setattr(requester_warnings, "notify_user", fake_notify)

    def run(
        self,
        *,
        timer_started_at: datetime = INSIDE_WINDOW,
        task_enabled: bool = True,
        warning_days: int = 7,
        scans: int = 1,
        extra: Any = None,
    ) -> list[requester_warnings.WarningResult]:
        async def go() -> list[requester_warnings.WarningResult]:
            engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            maker = async_sessionmaker(
                engine, expire_on_commit=False, class_=AsyncSession
            )
            async with maker() as db:
                db.add(GeneralSettings(requester_warning_days=warning_days))
                db.add(
                    TaskSchedule(
                        task=Task.DELETE_CLEANUP_CANDIDATES,
                        description="auto delete",
                        schedule_type=ScheduleType.CRON,
                        schedule_value="0 2 * * 0",
                        default_schedule_type=ScheduleType.CRON,
                        default_schedule_value="0 2 * * 0",
                        enabled=task_enabled,
                    )
                )
                plex = ServiceConfig(
                    service_type=Service.PLEX,
                    name="Plex",
                    base_url="http://p",
                    api_key="k",
                )
                user = User(username="bob", password_hash="x", role=UserRole.USER)
                rule = ReclaimRule(
                    name="Old",
                    media_type=MediaType.MOVIE,
                    enabled=True,
                    target_scope="movie",
                    definition=None,
                    action={"auto_delete_enabled": True},
                )
                movie = Movie(title="Dune", tmdb_id=438631, year=2021)
                db.add_all([plex, user, rule, movie])
                await db.flush()
                db.add(
                    MediaUserIdentity(
                        source_service=Service.PLEX,
                        source_service_config_id=plex.id,
                        source_user_id="176142613",
                        username="BobPlex",
                        username_normalized="bobplex",
                        user_id=user.id,
                        last_seen_at=NOW,
                    )
                )
                candidate = ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    matched_rule_ids=[rule.id],
                    matched_criteria={},
                    reason="old",
                    movie_id=movie.id,
                    auto_delete_timer_started_at=timer_started_at,
                )
                db.add(candidate)
                if extra is not None:
                    await extra(db, rule)
                await db.commit()
                results = [
                    await requester_warnings.warn_requesters_before_deletion(
                        db, now=NOW
                    )
                    for _ in range(scans)
                ]
            await engine.dispose()
            return results

        return asyncio.run(go())


def _movie_snapshot(*requesters: SeerrUser) -> SeerrRequestSnapshot:
    keys = {f"1:{r.id}": r for r in requesters}
    return _snapshot(keys, {(MediaType.MOVIE, 438631): set(keys)})


def test_a_mapped_requester_is_warned_once_across_two_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(monkeypatch, _movie_snapshot(BOB))
    first, second = harness.run(scans=2)

    assert len(harness.sent) == 1
    _user_id, message, context = harness.sent[0]
    assert message.startswith("Dune (2021) will be deleted on October 13, 2026.")
    assert context["media_title"] == "Dune (2021)"
    assert (first.warned_candidates, second.warned_candidates) == (1, 0)


def test_an_unmapped_requester_is_skipped_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # same display name as the linked account, which must not be enough
    harness = Harness(monkeypatch, _movie_snapshot(STRANGER))
    (result,) = harness.run()

    assert harness.sent == []
    assert result.unmatched_requesters == 1
    assert result.warned_candidates == 0


def test_a_deadline_outside_the_window_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(monkeypatch, _movie_snapshot(BOB))
    # deadline 14 days out, window 7: not yet, and not marked as warned
    first, _later = harness.run(timer_started_at=NOW, scans=2)

    assert harness.sent == []
    assert first.warned_candidates == 0


@pytest.mark.parametrize(
    "kwargs", [{"task_enabled": False}, {"warning_days": 0}], ids=["task-off", "zero"]
)
def test_nothing_is_sent_when_warnings_or_auto_delete_are_off(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
) -> None:
    harness = Harness(monkeypatch, _movie_snapshot(BOB))
    harness.run(**kwargs)
    assert harness.sent == []


def test_missing_seerr_data_leaves_the_candidate_for_the_next_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(monkeypatch, None)
    (result,) = harness.run()
    assert harness.sent == []
    assert result.warned_candidates == 0


def test_several_titles_for_one_user_arrive_as_one_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def add_series_season(db: AsyncSession, rule: ReclaimRule) -> None:
        series = Series(title="Severance", tmdb_id=95396, year=2022)
        db.add(series)
        await db.flush()
        season = Season(series_id=series.id, season_number=1)
        db.add(season)
        await db.flush()
        rule.media_type = MediaType.SERIES
        db.add(
            ReclaimCandidate(
                media_type=MediaType.SERIES,
                matched_rule_ids=[rule.id],
                matched_criteria={},
                reason="old",
                series_id=series.id,
                season_id=season.id,
                auto_delete_timer_started_at=NOW - timedelta(days=30),
            )
        )

    snapshot = _snapshot(
        {"1:7": BOB},
        {(MediaType.MOVIE, 438631): {"1:7"}},
        by_season={(95396, 1): {"1:7"}},
    )
    harness = Harness(monkeypatch, snapshot)
    harness.run(extra=add_series_season)

    assert len(harness.sent) == 1
    _user_id, message, context = harness.sent[0]
    assert message.startswith("2 titles you requested")
    # soonest first; the season is already past its deadline
    assert [t["label"] for t in context["titles"]] == [
        "Severance (2022) Season 1",
        "Dune (2021)",
    ]
    assert context["titles"][0]["when"] == "at the next cleanup run"


def test_the_type_maps_to_its_own_setting_column() -> None:
    field = _notification_type_to_field(NotificationType.REQUESTER_LEAVING_SOON)
    assert field == "requester_leaving_soon"
