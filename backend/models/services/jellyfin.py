from __future__ import annotations

from dataclasses import dataclass

from .emby_base import EmbyMovieBase, EmbySeriesBase, EmbyUserBase, EmbyUserDataBase


@dataclass(slots=True, frozen=True)
class JellyfinUser(EmbyUserBase):
    """Jellyfin user information."""


@dataclass(slots=True, frozen=True)
class JellyfinUserData(EmbyUserDataBase):
    """User-specific watch data for a media item."""


@dataclass(slots=True, frozen=True)
class JellyfinMovie(EmbyMovieBase):
    """Internal Jellyfin movie representation with user data."""


@dataclass(slots=True, frozen=True)
class JellyfinSeries(EmbySeriesBase):
    """Internal Jellyfin series representation with user data."""
