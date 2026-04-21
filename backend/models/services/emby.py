from __future__ import annotations

from dataclasses import dataclass

from .emby_base import EmbyMovieBase, EmbySeriesBase, EmbyUserBase, EmbyUserDataBase


@dataclass(slots=True, frozen=True)
class EmbyUser(EmbyUserBase):
    """Emby user information."""


@dataclass(slots=True, frozen=True)
class EmbyUserData(EmbyUserDataBase):
    """User specific watch data for a media item."""


@dataclass(slots=True, frozen=True)
class EmbyMovie(EmbyMovieBase):
    """Internal Emby movie representation with user data."""


@dataclass(slots=True, frozen=True)
class EmbySeries(EmbySeriesBase):
    """Internal Emby series representation with user data."""
