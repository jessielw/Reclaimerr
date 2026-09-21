from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import require_page_access
from backend.core.auto_delete import resolve_auto_delete_policy
from backend.core.utils.datetime_utils import ensure_utc
from backend.database import get_db
from backend.database.models import (
    Episode,
    GeneralSettings,
    Movie,
    MovieVersion,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
    User,
)
from backend.enums import MediaType, PageAccess
from backend.models.calendar import (
    CalendarDay,
    CalendarItem,
    CalendarResponse,
    CandidateOperation,
    CandidateScope,
)

router = APIRouter(prefix="/api", tags=["calendar"])

# A year of days is more than any view needs, and it bounds the work a single
# request can ask for.
MAX_WINDOW_DAYS = 366

# Only these states have a meaningful date to plot: a disabled or cancelled
# candidate is never going to be acted on.
PLOTTED_STATES = frozenset({"scheduled", "eligible", "postponed"})


def _scope_of(candidate: ReclaimCandidate) -> CandidateScope:
    if candidate.media_type is MediaType.MOVIE:
        return "version" if candidate.movie_version_id is not None else "movie"
    if candidate.episode_id is not None:
        return "episode"
    if candidate.season_id is not None:
        return "season"
    return "series"


def _title_for(row: Any, candidate: ReclaimCandidate) -> str | None:
    """Return the label shown on the calendar, or None when media is missing."""
    if candidate.media_type is MediaType.MOVIE:
        return row.movie_title
    title = row.series_title
    if title is None:
        return None
    season_number = row.season_number
    episode_number = row.episode_number
    if episode_number is not None and season_number is not None:
        return f"{title} S{season_number:02d}E{episode_number:02d}"
    if season_number is not None:
        return f"{title} S{season_number:02d}"
    return title


def _estimated_bytes(row: Any, candidate: ReclaimCandidate) -> int | None:
    """Fall back through the scope's own size when the candidate has none."""
    if candidate.estimated_space_bytes is not None:
        return candidate.estimated_space_bytes
    for value in (
        row.version_size,
        row.episode_size,
        row.season_size,
        row.movie_size if candidate.media_type is MediaType.MOVIE else row.series_size,
    ):
        if value is not None:
            return value
    return None


@router.get("/calendar", response_model=CalendarResponse)
async def get_calendar(
    _user: Annotated[User, Depends(require_page_access(PageAccess.CALENDAR))],
    start: Annotated[
        date, Query(description="First day to include, in the viewer's zone")
    ],
    end: Annotated[date, Query(description="Last day to include, inclusive")],
    db: AsyncSession = Depends(get_db),
    tz_offset_minutes: Annotated[
        int,
        Query(
            ge=-840,
            le=840,
            description=(
                "Minutes to add to UTC to reach the viewer's local time, i.e. "
                "the negation of JavaScript's Date.getTimezoneOffset()."
            ),
        ),
    ] = 0,
    per_day_limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> CalendarResponse:
    """Return the scheduled deletions and moves falling inside a date window.

    Deadlines are not stored: they are derived from each candidate's timer and
    the delay its matched rules ask for, so this resolves them the same way the
    candidates list and the auto-delete task do.
    """
    if end < start:
        raise HTTPException(status_code=400, detail="end must not precede start")
    if (end - start).days + 1 > MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Window must cover at most {MAX_WINDOW_DAYS} days",
        )

    offset = timedelta(minutes=tz_offset_minutes)

    settings_row = (await db.execute(select(GeneralSettings))).scalars().first()
    movie_delay_days = (
        settings_row.auto_delete_movie_delay_days if settings_row is not None else 14
    )
    series_delay_days = (
        settings_row.auto_delete_series_delay_days if settings_row is not None else 7
    )

    rows = (
        await db.execute(
            select(
                ReclaimCandidate,
                Movie.title.label("movie_title"),
                Movie.year.label("movie_year"),
                Movie.size.label("movie_size"),
                MovieVersion.size.label("version_size"),
                Series.title.label("series_title"),
                Series.year.label("series_year"),
                Series.size.label("series_size"),
                Season.season_number.label("season_number"),
                Season.size.label("season_size"),
                Episode.episode_number.label("episode_number"),
                Episode.size.label("episode_size"),
            )
            .outerjoin(Movie, ReclaimCandidate.movie_id == Movie.id)
            .outerjoin(
                MovieVersion, ReclaimCandidate.movie_version_id == MovieVersion.id
            )
            .outerjoin(Series, ReclaimCandidate.series_id == Series.id)
            .outerjoin(Season, ReclaimCandidate.season_id == Season.id)
            .outerjoin(Episode, ReclaimCandidate.episode_id == Episode.id)
        )
    ).all()

    rule_ids = {
        rule_id
        for row in rows
        for rule_id in (row.ReclaimCandidate.matched_rule_ids or [])
    }
    rule_actions_by_id: dict[int, dict[str, Any] | None] = {}
    if rule_ids:
        rule_actions_by_id = {
            rule.id: rule.action
            for rule in (
                (
                    await db.execute(
                        select(ReclaimRule).where(ReclaimRule.id.in_(rule_ids))
                    )
                )
                .scalars()
                .all()
            )
        }

    now = datetime.now(UTC)
    buckets: dict[date, list[CalendarItem]] = {}
    for row in rows:
        candidate = row.ReclaimCandidate
        policy = resolve_auto_delete_policy(
            media_type=cast(MediaType, candidate.media_type),
            matched_rule_ids=cast(list[int], candidate.matched_rule_ids),
            created_at=cast(datetime, candidate.created_at),
            timer_started_at=candidate.auto_delete_timer_started_at,
            postponed_until=candidate.auto_delete_postponed_until,
            cancelled_at=candidate.auto_delete_cancelled_at,
            rule_actions_by_id=rule_actions_by_id,
            movie_delay_days=movie_delay_days,
            series_delay_days=series_delay_days,
            now=now,
        )
        if not policy.is_enabled or policy.state not in PLOTTED_STATES:
            continue

        eligible_at = ensure_utc(policy.eligible_at)
        day = (eligible_at + offset).date()
        if day < start or day > end:
            continue

        title = _title_for(row, candidate)
        is_movie = candidate.media_type is MediaType.MOVIE
        media_id = candidate.movie_id if is_movie else candidate.series_id
        if title is None or media_id is None:
            # the media row is gone; the candidate is cleaned up on the next scan
            continue

        operation: CandidateOperation = (
            "move"
            if any(
                (rule_actions_by_id.get(rule_id) or {}).get("move_instead_of_delete")
                is True
                for rule_id in (candidate.matched_rule_ids or [])
            )
            else "delete"
        )

        buckets.setdefault(day, []).append(
            CalendarItem(
                candidate_id=candidate.id,
                media_type=candidate.media_type.value,
                scope=_scope_of(candidate),
                title=title,
                year=row.movie_year if is_movie else row.series_year,
                media_id=media_id,
                movie_version_id=candidate.movie_version_id,
                series_id=candidate.series_id,
                season_id=candidate.season_id,
                episode_id=candidate.episode_id,
                estimated_space_bytes=_estimated_bytes(row, candidate),
                operation=operation,
                state=policy.state,
                eligible_at=eligible_at,
            )
        )

    days: list[CalendarDay] = []
    total_items = 0
    total_bytes = 0
    for day in sorted(buckets):
        items = sorted(buckets[day], key=lambda item: (item.eligible_at, item.title))
        day_bytes = sum(item.estimated_space_bytes or 0 for item in items)
        total_items += len(items)
        total_bytes += day_bytes
        days.append(
            CalendarDay(
                date=day,
                item_count=len(items),
                total_bytes=day_bytes,
                truncated=len(items) > per_day_limit,
                items=items[:per_day_limit],
            )
        )

    return CalendarResponse(
        start=start,
        end=end,
        days=days,
        total_items=total_items,
        total_bytes=total_bytes,
    )
