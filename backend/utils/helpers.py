from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "DEFAULT_LEAVING_SOON_BASE_TITLE",
    "DEFAULT_LEAVING_SOON_MOVIE_TITLE",
    "DEFAULT_LEAVING_SOON_SERIES_TITLE",
    "MAX_LEAVING_SOON_TITLE_LENGTH",
    "LeavingSoonTitles",
    "leaving_soon_titles_from_base_title",
    "normalize_leaving_soon_movie_title",
    "normalize_leaving_soon_series_title",
    "normalize_leaving_soon_titles",
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
