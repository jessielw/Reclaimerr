from __future__ import annotations

import unittest

from backend.database.models import Movie, MovieVersion, ReclaimRule
from backend.enums import MediaType, Service
from backend.tasks.cleanup import _evaluate_movie_rule
from backend.tasks.sync import _update_movie_tmdb_metadata


class _FakeTMDBMovieService:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def get_movie_details(self, tmdb_id: int) -> dict:
        return self._payload


def _rating_rule(
    *,
    media_type: MediaType,
    target_scope: str,
    operator: str,
    value: object | None = None,
) -> ReclaimRule:
    condition: dict[str, object] = {
        "type": "condition",
        "field": "tmdb.vote_average",
        "operator": operator,
    }
    if value is not None:
        condition["value"] = value
    return ReclaimRule(
        name="rating-rule",
        media_type=media_type,
        enabled=True,
        target_scope=target_scope,
        definition={
            "version": 1,
            "root": {"type": "group", "op": "and", "children": [condition]},
        },
        action={"candidate": True, "media_server_action": "delete"},
    )


def _make_movie() -> Movie:
    movie = Movie(title="Placeholder Movie", tmdb_id=1, size=10 * 1024**3)
    movie.versions = [
        MovieVersion(
            movie_id=1,
            service=Service.PLEX,
            service_item_id="i1",
            service_media_id="m1",
            library_id="lib-1",
            library_name="Library 1",
        )
    ]
    return movie


class MovieTmdbRatingSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_votes_stores_no_rating(self) -> None:
        movie = _make_movie()
        service = _FakeTMDBMovieService({"vote_average": 0, "vote_count": 0})

        await _update_movie_tmdb_metadata(movie, 1, service)  # type: ignore[arg-type]

        # the updater swallows exceptions, and an aborted run would leave
        # vote_average at its model default of None, so assert it completed
        self.assertIsNotNone(movie.last_metadata_refresh_at)
        self.assertIsNone(movie.vote_average)
        self.assertEqual(movie.vote_count, 0)

    async def test_real_votes_store_the_rating_unchanged(self) -> None:
        movie = _make_movie()
        service = _FakeTMDBMovieService({"vote_average": 7.5, "vote_count": 1200})

        await _update_movie_tmdb_metadata(movie, 1, service)  # type: ignore[arg-type]

        self.assertIsNotNone(movie.last_metadata_refresh_at)
        self.assertEqual(movie.vote_average, 7.5)
        self.assertEqual(movie.vote_count, 1200)

    async def test_missing_vote_count_stores_no_rating(self) -> None:
        movie = _make_movie()
        service = _FakeTMDBMovieService({"vote_average": 7.5})

        await _update_movie_tmdb_metadata(movie, 1, service)  # type: ignore[arg-type]

        self.assertIsNotNone(movie.last_metadata_refresh_at)
        self.assertIsNone(movie.vote_average)
        self.assertIsNone(movie.vote_count)

    async def test_unrated_movie_is_excluded_from_rating_rules(self) -> None:
        movie = _make_movie()
        service = _FakeTMDBMovieService({"vote_average": 0, "vote_count": 0})

        await _update_movie_tmdb_metadata(movie, 1, service)  # type: ignore[arg-type]

        below_five = _rating_rule(
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            operator="less_than",
            value=5,
        )
        missing = _rating_rule(
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            operator="not_exists",
        )

        self.assertFalse(_evaluate_movie_rule(movie, below_five, {}, []))
        self.assertTrue(_evaluate_movie_rule(movie, missing, {}, []))


if __name__ == "__main__":
    unittest.main()
