from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from backend.enums import LeavingSoonCollectionSort

__all__ = [
    "DEFAULT_LEAVING_SOON_BASE_TITLE",
    "DEFAULT_LEAVING_SOON_MOVIE_TITLE",
    "DEFAULT_LEAVING_SOON_SERIES_TITLE",
    "MAX_LEAVING_SOON_TITLE_LENGTH",
    "LeavingSoonPosters",
    "LeavingSoonTitles",
    "leaving_soon_titles_from_base_title",
    "normalize_leaving_soon_collection_sort",
    "normalize_leaving_soon_movie_title",
    "normalize_leaving_soon_series_title",
    "normalize_leaving_soon_titles",
    "order_leaving_soon_item_ids",
]

DEFAULT_LEAVING_SOON_BASE_TITLE = "Leaving Soon"
DEFAULT_LEAVING_SOON_MOVIE_TITLE = f"{DEFAULT_LEAVING_SOON_BASE_TITLE} [Movies]"
DEFAULT_LEAVING_SOON_SERIES_TITLE = f"{DEFAULT_LEAVING_SOON_BASE_TITLE} [Series]"
MAX_LEAVING_SOON_TITLE_LENGTH = 255


@dataclass(frozen=True, slots=True)
class LeavingSoonTitles:
    """The pair of collection names Reclaimerr manages on a single server."""

    movies: str
    series: str

    def as_dict(self) -> dict[str, str]:
        return {"movies": self.movies, "series": self.series}


@dataclass(frozen=True, slots=True)
class LeavingSoonPosters:
    """Custom artwork for the managed collections, already read off disk.

    Bytes rather than paths so the media server clients stay free of filesystem
    concerns, and so one task run reads each file once no matter how many
    servers it pushes to.
    """

    movies: bytes | None = None
    series: bytes | None = None

    def __bool__(self) -> bool:
        return self.movies is not None or self.series is not None


def _normalize_title(value: object, default: str) -> str:
    """Strip a configured collection title, falling back to `default` when blank."""
    title = str(value or "").strip()
    return title or default


def normalize_leaving_soon_movie_title(value: object) -> str:
    """Normalize the managed movie collection title."""
    return _normalize_title(value, DEFAULT_LEAVING_SOON_MOVIE_TITLE)


def normalize_leaving_soon_series_title(value: object) -> str:
    """Normalize the managed series collection title."""
    return _normalize_title(value, DEFAULT_LEAVING_SOON_SERIES_TITLE)


def normalize_leaving_soon_collection_sort(value: object) -> LeavingSoonCollectionSort:
    """Normalize a persisted collection sort, falling back to the default.

    Blank, unknown, and legacy values all resolve to DEFAULT so an install that
    predates the setting - or one whose column holds a value written by a newer
    build - keeps the untouched-collection behavior instead of erroring.
    """
    try:
        return LeavingSoonCollectionSort(str(value or "").strip().lower())
    except ValueError:
        return LeavingSoonCollectionSort.DEFAULT


def leaving_soon_titles_from_base_title(value: object) -> LeavingSoonTitles:
    """Expand a legacy single base title into the movie/series pair.

    Pre-0.4.3 installs stored one base title and derived `<base> [Movies]` /
    `<base> [Series]` inside each media server client. Reading that shape back
    through here keeps a rename made before the upgrade cleanable afterwards.
    """
    base = str(value or "").strip() or DEFAULT_LEAVING_SOON_BASE_TITLE
    return LeavingSoonTitles(movies=f"{base} [Movies]", series=f"{base} [Series]")


def normalize_leaving_soon_titles(value: object) -> LeavingSoonTitles:
    """Normalize a persisted title pair, tolerating the legacy base-title string."""
    if isinstance(value, LeavingSoonTitles):
        return LeavingSoonTitles(
            movies=normalize_leaving_soon_movie_title(value.movies),
            series=normalize_leaving_soon_series_title(value.series),
        )
    if isinstance(value, Mapping):
        return LeavingSoonTitles(
            movies=normalize_leaving_soon_movie_title(value.get("movies")),
            series=normalize_leaving_soon_series_title(value.get("series")),
        )
    return leaving_soon_titles_from_base_title(value)


def order_leaving_soon_item_ids(
    item_ids: Iterable[str],
    *,
    collection_sort: LeavingSoonCollectionSort,
    item_deadlines: Mapping[str, datetime] | None = None,
) -> list[str]:
    """Order server item IDs for the configured collection sort.

    Only `leaving_soonest` needs an explicit order - alphabetical is computed
    by the media server itself, and the default leaves the server alone - so
    every other mode falls back to the lexicographic order the collection sync
    has always used. An item with no known deadline sorts last, and the ID
    breaks ties so repeated runs produce the same order.
    """
    normalized = sorted(
        {stripped for item_id in item_ids if (stripped := str(item_id).strip())}
    )
    if collection_sort is not LeavingSoonCollectionSort.LEAVING_SOONEST:
        return normalized
    deadlines = item_deadlines or {}
    latest = datetime.max.replace(tzinfo=UTC)
    return sorted(
        normalized, key=lambda item_id: (deadlines.get(item_id, latest), item_id)
    )
