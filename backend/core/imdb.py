from __future__ import annotations

import csv
import gzip
import hashlib
import io
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

IMDB_TITLE_RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"


@dataclass(frozen=True, slots=True)
class IMDbTitleRatingRow:
    imdb_id: str
    rating: float | None
    vote_count: int | None


def build_conditional_headers(
    *, etag: str | None = None, last_modified: str | None = None
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def parse_imdb_last_modified(last_modified: str | None) -> datetime | None:
    if not last_modified:
        return None
    parsed = parsedate_to_datetime(last_modified)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_title_ratings_tsv_gz(payload: bytes) -> Iterator[IMDbTitleRatingRow]:
    """
    Parse IMDb `title.ratings.tsv.gz` payload and yield normalized rows.

    Expected TSV header:
      tconst    averageRating    numVotes
    """
    with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as compressed:
        with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text_stream:
            yield from parse_title_ratings_tsv_lines(text_stream)


def parse_title_ratings_tsv_lines(lines: Iterable[str]) -> Iterator[IMDbTitleRatingRow]:
    reader = csv.DictReader(lines, delimiter="\t")
    for raw_row in reader:
        parsed = _parse_rating_row(raw_row)
        if parsed is not None:
            yield parsed


def _parse_rating_row(raw_row: dict[str, str | None]) -> IMDbTitleRatingRow | None:
    imdb_id = (raw_row.get("tconst") or "").strip()
    if not imdb_id:
        return None

    rating_raw = (raw_row.get("averageRating") or "").strip()
    votes_raw = (raw_row.get("numVotes") or "").strip()

    try:
        rating = float(rating_raw) if rating_raw else None
    except ValueError:
        rating = None

    try:
        vote_count = int(votes_raw) if votes_raw else None
    except ValueError:
        vote_count = None

    return IMDbTitleRatingRow(imdb_id=imdb_id, rating=rating, vote_count=vote_count)


def batched_rows(
    rows: Iterable[IMDbTitleRatingRow], batch_size: int
) -> Iterator[list[IMDbTitleRatingRow]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")

    batch: list[IMDbTitleRatingRow] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch
