from __future__ import annotations

import gzip
from datetime import UTC, datetime

from backend.core.imdb import (
    IMDB_TITLE_RATINGS_URL,
    IMDbTitleRatingRow,
    batched_rows,
    build_conditional_headers,
    parse_imdb_last_modified,
    parse_title_ratings_tsv_gz,
)
from backend.core.imdb_cache import (
    IMDbCacheMetadata,
    get_cached_ratings,
    read_cache_state,
    replace_cache,
)


def _gzip_text(content: str) -> bytes:
    return gzip.compress(content.encode("utf-8"))


def test_build_conditional_headers() -> None:
    headers = build_conditional_headers(
        etag='"abc123"',
        last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
    )
    assert headers == {
        "If-None-Match": '"abc123"',
        "If-Modified-Since": "Wed, 21 Oct 2015 07:28:00 GMT",
    }


def test_parse_title_ratings_tsv_gz() -> None:
    payload = _gzip_text(
        "tconst\taverageRating\tnumVotes\ntt0000001\t5.7\t2123\ntt0000002\t9.1\t999\n"
    )
    rows = list(parse_title_ratings_tsv_gz(payload))
    assert [row.imdb_id for row in rows] == ["tt0000001", "tt0000002"]
    assert rows[0].rating == 5.7
    assert rows[0].vote_count == 2123
    assert rows[1].rating == 9.1
    assert rows[1].vote_count == 999


def test_parse_title_ratings_skips_blank_imdb_id_and_tolerates_bad_numbers() -> None:
    payload = _gzip_text(
        "tconst\taverageRating\tnumVotes\n"
        "\t5.7\t2123\n"
        "tt1234567\tnot-a-float\tnot-an-int\n"
    )
    rows = list(parse_title_ratings_tsv_gz(payload))
    assert len(rows) == 1
    assert rows[0].imdb_id == "tt1234567"
    assert rows[0].rating is None
    assert rows[0].vote_count is None


def test_batched_rows() -> None:
    payload = _gzip_text(
        "tconst\taverageRating\tnumVotes\ntt1\t1.0\t1\ntt2\t2.0\t2\ntt3\t3.0\t3\n"
    )
    rows = list(parse_title_ratings_tsv_gz(payload))
    batches = list(batched_rows(rows, 2))
    assert len(batches) == 2
    assert [row.imdb_id for row in batches[0]] == ["tt1", "tt2"]
    assert [row.imdb_id for row in batches[1]] == ["tt3"]


def test_parse_imdb_last_modified() -> None:
    parsed = parse_imdb_last_modified("Wed, 21 Oct 2015 07:28:00 GMT")
    assert parsed is not None
    assert parsed.tzinfo == UTC
    assert parsed.year == 2015


def test_imdb_cache_replace_and_lookup(tmp_path) -> None:
    cache_path = tmp_path / "imdb_ratings.sqlite3"
    refreshed_at = datetime(2026, 7, 13, tzinfo=UTC)

    row_count = replace_cache(
        [
            IMDbTitleRatingRow("tt0000001", 5.7, 2217),
            IMDbTitleRatingRow("tt0000002", 6.4, 2354),
        ],
        IMDbCacheMetadata(
            dataset_url=IMDB_TITLE_RATINGS_URL,
            etag='"etag"',
            last_modified="Mon, 13 Jul 2026 00:00:00 GMT",
            sha256="abc123",
            content_length=123,
            row_count=0,
            last_checked_at=refreshed_at,
            last_successful_refresh_at=refreshed_at,
            source_updated_at=refreshed_at,
        ),
        path=cache_path,
    )

    assert row_count == 2
    state = read_cache_state(cache_path)
    assert state is not None
    assert state.etag == '"etag"'
    assert state.row_count == 2

    ratings = get_cached_ratings(["tt0000001", "tt0000003"], cache_path)
    assert set(ratings) == {"tt0000001"}
    assert ratings["tt0000001"].rating == 5.7
    assert ratings["tt0000001"].vote_count == 2217


def test_imdb_cache_failed_replace_keeps_existing_cache(tmp_path) -> None:
    cache_path = tmp_path / "imdb_ratings.sqlite3"
    refreshed_at = datetime(2026, 7, 13, tzinfo=UTC)
    metadata = IMDbCacheMetadata(
        dataset_url=IMDB_TITLE_RATINGS_URL,
        etag='"etag"',
        last_modified="Mon, 13 Jul 2026 00:00:00 GMT",
        sha256="abc123",
        content_length=123,
        row_count=0,
        last_checked_at=refreshed_at,
        last_successful_refresh_at=refreshed_at,
    )

    replace_cache(
        [IMDbTitleRatingRow("tt0000001", 5.7, 2217)], metadata, path=cache_path
    )

    try:
        replace_cache([], metadata, path=cache_path)
    except RuntimeError:
        pass
    else:
        raise AssertionError("empty IMDb cache replace should fail")

    ratings = get_cached_ratings(["tt0000001"], cache_path)
    assert ratings["tt0000001"].rating == 5.7
