import re
import sys
from pathlib import Path
from random import sample as random_sample

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core import __version__
from backend.database import get_db
from backend.database.models import Movie, Series

from .default_backdrops import TOP_RATED_BACKDROPS

router = APIRouter(tags=["info"])

# matches "## [Unreleased] - date" or "## [0.1.0-beta.13] - 2026-04-25"
_RELEASE_HEADER = re.compile(
    r"^## \[(?P<version>[^\]]+)\](?:\s*-\s*(?P<date>[^\n]+))?",
    re.MULTILINE,
)


def _find_changelog() -> Path | None:
    """Return the first readable CHANGELOG.md path."""
    candidates: list[Path] = []
    candidates.extend(
        [
            Path.cwd() / "CHANGELOG.md",
            Path(sys.executable).resolve().parent / "CHANGELOG.md",
            Path(__file__).resolve().parents[4] / "CHANGELOG.md",
            Path(sys.argv[0]).resolve().parent / "CHANGELOG.md",
            Path("/app/CHANGELOG.md"),
        ]
    )
    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _parse_changelog(text: str) -> list[dict]:
    """Split changelog text into per-release dicts sorted newest first."""
    matches = list(_RELEASE_HEADER.finditer(text))
    releases: list[dict] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        releases.append(
            {
                "version": m.group("version"),
                "date": (m.group("date") or "").strip() or None,
                "body": body,
            }
        )
    return releases


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@router.get("/version")
async def get_version() -> dict[str, str]:
    """Get application version."""
    return {
        "version": str(__version__),
        "program": __version__.program_name,
        "url": __version__.program_url,
    }


@router.get("/changelog")
async def get_changelog() -> list[dict]:
    """Return bundled changelog split into per-release entries."""
    path = _find_changelog()
    if path is None:
        raise HTTPException(status_code=404, detail="Changelog not found")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to read changelog") from exc
    releases = _parse_changelog(text)
    if not releases:
        raise HTTPException(status_code=404, detail="Changelog contains no releases")
    return releases


@router.get("/random-backdrop")
async def get_backdrops(
    num_of_bd: int = Query(
        10,
        ge=1,
        le=20,
        alias="num-of-bd",
        description="Number of random backdrops to return",
    ),
    fetch_limit: int = Query(
        40,
        ge=20,
        le=100,
        alias="fetch-limit",
        description="Number of backdrops to fetch from the database",
    ),
    db: AsyncSession = Depends(get_db),
) -> dict[str, list[str]]:
    """
    Get backdrop image URLs.
    If no backdrops are found in the database, return a random selection of default top-rated backdrops.

    `fetch_limit` should be set higher than `num_of_bd` to ensure enough backdrops are available for
    random sampling. If there are fewer backdrops in the database than `num_of_bd`, all available
    backdrops will be returned.
    """
    # ensure fetch limit is at least as large as the number of backdrops requested
    if fetch_limit < num_of_bd:
        raise HTTPException(
            status_code=400,
            detail="Fetch limit must be greater than or equal to the number of backdrops requested",
        )

    KEY = "backdrops"

    # count movies with a backdrop
    movie_count_stmt = (
        select(func.count()).select_from(Movie).where(Movie.backdrop_url.isnot(None))
    )
    series_count_stmt = (
        select(func.count()).select_from(Series).where(Series.backdrop_url.isnot(None))
    )

    movie_count = (await db.execute(movie_count_stmt)).scalar_one()
    series_count = (await db.execute(series_count_stmt)).scalar_one()
    total_count = movie_count + series_count

    if total_count == 0:
        return {KEY: random_sample(TOP_RATED_BACKDROPS, num_of_bd)}

    # fetch up to 'fetch_limit' most popular backdrops from each
    movie_stmt = (
        select(Movie.backdrop_url)
        .where(Movie.backdrop_url.isnot(None))
        .order_by(desc(Movie.popularity))
        .limit(fetch_limit)
    )
    series_stmt = (
        select(Series.backdrop_url)
        .where(Series.backdrop_url.isnot(None))
        .order_by(desc(Series.popularity))
        .limit(fetch_limit)
    )

    movie_backdrops = [row[0] for row in (await db.execute(movie_stmt)).all()]
    series_backdrops = [row[0] for row in (await db.execute(series_stmt)).all()]
    all_backdrops = [url for url in movie_backdrops + series_backdrops if url]

    if not all_backdrops or len(all_backdrops) < num_of_bd:
        return {KEY: random_sample(TOP_RATED_BACKDROPS, num_of_bd)}

    return {KEY: random_sample(all_backdrops, num_of_bd)}
