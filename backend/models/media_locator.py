from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from backend.enums import MediaType


class MediaLocator(BaseModel):
    """Select one catalog title, regardless of its libraries or physical files."""

    media_type: MediaType
    media_id: int | None = Field(default=None, ge=1)
    tmdb_id: int | None = Field(default=None, ge=1)
    imdb_id: str | None = Field(default=None, pattern=r"^tt[0-9]+$", max_length=20)
    tvdb_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]*$", max_length=20)
    anilist_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_media_locator(self) -> MediaLocator:
        values = (
            self.media_id,
            self.tmdb_id,
            self.imdb_id,
            self.tvdb_id,
            self.anilist_id,
        )
        if sum(value is not None for value in values) != 1:
            raise ValueError(
                "Provide exactly one of media_id, tmdb_id, imdb_id, tvdb_id, or anilist_id"
            )
        if self.tvdb_id is not None and self.media_type is MediaType.MOVIE:
            raise ValueError("TVDB IDs are only supported for series")
        return self
