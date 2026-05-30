from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from backend.database.models import Movie
from backend.enums import MediaType
from backend.tasks.sync import _needs_metadata_refresh, _update_movie_tmdb_metadata


class _FakeTMDBService:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def get_movie_details(self, tmdb_id: int) -> dict:
        return self._payload


class SyncTmdbCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_movie_tmdb_metadata_sets_collection_fields(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=1)
        service = _FakeTMDBService(
            {
                "external_ids": {"imdb_id": "tt0000001"},
                "title": "Movie",
                "original_title": "Movie",
                "belongs_to_collection": {"id": 1234, "name": "Die Hard Collection"},
            }
        )

        await _update_movie_tmdb_metadata(movie, 1, service)  # type: ignore[arg-type]

        self.assertEqual(movie.tmdb_collection_id, 1234)
        self.assertEqual(movie.tmdb_collection_name, "Die Hard Collection")
        self.assertTrue(movie.tmdb_collection_checked)

    async def test_update_movie_tmdb_metadata_clears_collection_fields_when_missing(
        self,
    ) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=1)
        movie.tmdb_collection_id = 999
        movie.tmdb_collection_name = "Old Collection"
        service = _FakeTMDBService(
            {
                "external_ids": {"imdb_id": "tt0000001"},
                "title": "Movie",
                "original_title": "Movie",
                "belongs_to_collection": None,
            }
        )

        await _update_movie_tmdb_metadata(movie, 1, service)  # type: ignore[arg-type]

        self.assertIsNone(movie.tmdb_collection_id)
        self.assertIsNone(movie.tmdb_collection_name)
        self.assertTrue(movie.tmdb_collection_checked)

    def test_needs_metadata_refresh_when_collection_not_checked(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=1)
        movie.last_metadata_refresh_at = datetime.now(UTC) - timedelta(days=1)
        movie.vote_average = 7.0
        movie.popularity = 5.0
        movie.backdrop_url = "/backdrop.jpg"
        movie.poster_url = "/poster.jpg"
        movie.tmdb_release_date = datetime.now(UTC) - timedelta(days=300)
        movie.tmdb_collection_checked = False

        self.assertTrue(_needs_metadata_refresh(movie, MediaType.MOVIE))

    def test_no_forced_refresh_when_collection_checked(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=1)
        movie.last_metadata_refresh_at = datetime.now(UTC) - timedelta(days=1)
        movie.vote_average = 7.0
        movie.popularity = 5.0
        movie.backdrop_url = "/backdrop.jpg"
        movie.poster_url = "/poster.jpg"
        movie.tmdb_release_date = datetime.now(UTC) - timedelta(days=300)
        movie.tmdb_collection_checked = True

        self.assertFalse(_needs_metadata_refresh(movie, MediaType.MOVIE))


if __name__ == "__main__":
    unittest.main()
