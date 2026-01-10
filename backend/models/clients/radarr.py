from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class RadarrMovie:
    id: int
    title: str
    tmdb_id: int | None
    imdb_id: str | None
    year: int | None
    path: str
    has_file: bool
    tags: list[int]
    raw: dict | None = None

    def __repr__(self) -> str:
        return (
            f"RadarrMovie(id={self.id}, title='{self.title}', tmdb_id={self.tmdb_id}, "
            f"imdb_id={self.imdb_id}, year={self.year}, has_file={self.has_file}, tags={self.tags})"
        )


def build_radarr_movie_from_dict(data: dict) -> RadarrMovie:
    return RadarrMovie(
        id=data["id"],
        title=data.get("title", ""),
        tmdb_id=data.get("tmdbId"),
        imdb_id=data.get("imdbId"),
        year=data.get("year"),
        path=data.get("path", ""),
        has_file=data.get("hasFile", False),
        tags=data.get("tags", []),
        raw=data,
    )
