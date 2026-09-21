from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

CandidateScope = Literal["movie", "version", "series", "season", "episode"]
CandidateOperation = Literal["delete", "move"]


class CalendarItem(BaseModel):
    """One scheduled candidate action on the calendar."""

    candidate_id: int
    media_type: str
    scope: CandidateScope
    title: str
    year: int | None = None
    media_id: int
    movie_version_id: int | None = None
    series_id: int | None = None
    season_id: int | None = None
    episode_id: int | None = None
    estimated_space_bytes: int | None = None
    operation: CandidateOperation
    state: str
    eligible_at: datetime


class CalendarDay(BaseModel):
    """A single day, with the items it will act on.

    ``item_count`` and ``total_bytes`` always describe the whole day; ``items``
    may be truncated to keep a busy day's payload small.
    """

    date: date
    item_count: int
    total_bytes: int
    truncated: bool
    items: list[CalendarItem]


class CalendarResponse(BaseModel):
    start: date
    end: date
    days: list[CalendarDay]
    total_items: int
    total_bytes: int
