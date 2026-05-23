from __future__ import annotations

import gzip
from datetime import UTC

from backend.core.imdb import (
    batched_rows,
    build_conditional_headers,
    parse_imdb_last_modified,
    parse_title_ratings_tsv_gz,
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
