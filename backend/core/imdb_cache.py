from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from backend.core.imdb import IMDB_TITLE_RATINGS_URL, IMDbTitleRatingRow
from backend.core.settings import settings

SCHEMA_VERSION = 1
UPSERT_BATCH_SIZE = 5000


@dataclass(frozen=True, slots=True)
class IMDbCacheState:
    dataset_url: str
    etag: str | None
    last_modified: str | None
    sha256: str | None
    content_length: int | None
    row_count: int | None
    last_checked_at: datetime | None
    last_successful_refresh_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class IMDbCacheMetadata:
    dataset_url: str
    etag: str | None
    last_modified: str | None
    sha256: str | None
    content_length: int
    row_count: int
    last_checked_at: datetime
    last_successful_refresh_at: datetime
    source_updated_at: datetime | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class IMDbCachedRating:
    rating: float | None
    vote_count: int | None


def imdb_cache_path() -> Path:
    cache_dir = settings.data_dir_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "imdb_ratings.sqlite3"


def read_cache_state(path: Path | None = None) -> IMDbCacheState | None:
    db_path = path or imdb_cache_path()
    if not db_path.exists():
        return None

    with closing(_connect(db_path)) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT dataset_url, etag, last_modified, sha256, content_length, row_count,
                   last_checked_at, last_successful_refresh_at, last_error
            FROM imdb_ratings_ingest_state
            WHERE id = 1
            """
        ).fetchone()
    return _state_from_row(row) if row is not None else None


def persist_not_modified(checked_at: datetime, path: Path | None = None) -> None:
    db_path = path or imdb_cache_path()
    with closing(_connect(db_path)) as conn:
        _ensure_schema(conn)
        existing = read_cache_state(db_path)
        last_successful_refresh_at = (
            existing.last_successful_refresh_at
            if existing and existing.last_successful_refresh_at
            else checked_at
        )
        metadata = IMDbCacheMetadata(
            dataset_url=existing.dataset_url if existing else IMDB_TITLE_RATINGS_URL,
            etag=existing.etag if existing else None,
            last_modified=existing.last_modified if existing else None,
            sha256=existing.sha256 if existing else None,
            content_length=existing.content_length or 0 if existing else 0,
            row_count=existing.row_count or 0 if existing else 0,
            last_checked_at=checked_at,
            last_successful_refresh_at=last_successful_refresh_at,
            source_updated_at=None,
            last_error=None,
        )
        _write_metadata(conn, metadata)
        conn.commit()


def persist_error(
    checked_at: datetime, error: str, *, max_length: int, path: Path | None = None
) -> None:
    db_path = path or imdb_cache_path()
    with closing(_connect(db_path)) as conn:
        _ensure_schema(conn)
        existing = read_cache_state(db_path)
        last_successful_refresh_at = (
            existing.last_successful_refresh_at
            if existing and existing.last_successful_refresh_at
            else checked_at
        )
        metadata = IMDbCacheMetadata(
            dataset_url=existing.dataset_url if existing else IMDB_TITLE_RATINGS_URL,
            etag=existing.etag if existing else None,
            last_modified=existing.last_modified if existing else None,
            sha256=existing.sha256 if existing else None,
            content_length=existing.content_length or 0 if existing else 0,
            row_count=existing.row_count or 0 if existing else 0,
            last_checked_at=checked_at,
            last_successful_refresh_at=last_successful_refresh_at,
            source_updated_at=None,
            last_error=(error or "unknown error")[:max_length],
        )
        _write_metadata(conn, metadata)
        conn.commit()


def replace_cache(
    rows: Iterable[IMDbTitleRatingRow],
    metadata: IMDbCacheMetadata,
    *,
    path: Path | None = None,
) -> int:
    db_path = path or imdb_cache_path()
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    if tmp_path.exists():
        tmp_path.unlink()

    row_count = 0
    try:
        with closing(_connect(tmp_path, wal=False)) as conn:
            _ensure_schema(conn)
            batch: list[tuple[str, float | None, int | None, str | None, str]] = []
            refreshed_at = (
                _serialize_datetime(metadata.last_successful_refresh_at) or ""
            )
            source_updated_at = _serialize_datetime(metadata.source_updated_at)
            for row in rows:
                batch.append(
                    (
                        row.imdb_id,
                        row.rating,
                        row.vote_count,
                        source_updated_at,
                        refreshed_at,
                    )
                )
                if len(batch) >= UPSERT_BATCH_SIZE:
                    _insert_rating_batch(conn, batch)
                    row_count += len(batch)
                    batch.clear()
            if batch:
                _insert_rating_batch(conn, batch)
                row_count += len(batch)

            if row_count <= 0:
                raise RuntimeError(
                    "IMDb ratings refresh produced 0 rows; keeping existing ratings cache"
                )

            final_metadata = IMDbCacheMetadata(
                dataset_url=metadata.dataset_url,
                etag=metadata.etag,
                last_modified=metadata.last_modified,
                sha256=metadata.sha256,
                content_length=metadata.content_length,
                row_count=row_count,
                last_checked_at=metadata.last_checked_at,
                last_successful_refresh_at=metadata.last_successful_refresh_at,
                source_updated_at=metadata.source_updated_at,
                last_error=metadata.last_error,
            )
            _write_metadata(conn, final_metadata)
            conn.commit()
        os.replace(tmp_path, db_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return row_count


def get_cached_ratings(
    imdb_ids: Iterable[str], path: Path | None = None
) -> dict[str, IMDbCachedRating]:
    db_path = path or imdb_cache_path()
    if not db_path.exists():
        return {}

    unique_ids = sorted({imdb_id for imdb_id in imdb_ids if imdb_id})
    if not unique_ids:
        return {}

    ratings: dict[str, IMDbCachedRating] = {}
    with closing(_connect(db_path)) as conn:
        _ensure_schema(conn)
        for start in range(0, len(unique_ids), 900):
            chunk = unique_ids[start : start + 900]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"""
                SELECT imdb_id, rating, vote_count
                FROM imdb_title_ratings
                WHERE imdb_id IN ({placeholders})
                """,
                chunk,
            ):
                ratings[str(row["imdb_id"])] = IMDbCachedRating(
                    rating=row["rating"],
                    vote_count=row["vote_count"],
                )
    return ratings


def _connect(path: Path, *, wal: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA journal_mode={'WAL' if wal else 'DELETE'}")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS imdb_ratings_ingest_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            dataset_url TEXT NOT NULL,
            etag TEXT NULL,
            last_modified TEXT NULL,
            sha256 TEXT NULL,
            content_length INTEGER NULL,
            row_count INTEGER NULL,
            last_checked_at TEXT NULL,
            last_successful_refresh_at TEXT NULL,
            last_error TEXT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS imdb_title_ratings (
            imdb_id TEXT PRIMARY KEY,
            rating REAL NULL,
            vote_count INTEGER NULL,
            source_updated_at TEXT NULL,
            refreshed_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _insert_rating_batch(
    conn: sqlite3.Connection,
    batch: list[tuple[str, float | None, int | None, str | None, str]],
) -> None:
    conn.executemany(
        """
        INSERT INTO imdb_title_ratings (
            imdb_id, rating, vote_count, source_updated_at, refreshed_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(imdb_id) DO UPDATE SET
            rating = excluded.rating,
            vote_count = excluded.vote_count,
            source_updated_at = excluded.source_updated_at,
            refreshed_at = excluded.refreshed_at,
            updated_at = CURRENT_TIMESTAMP
        """,
        batch,
    )


def _write_metadata(conn: sqlite3.Connection, metadata: IMDbCacheMetadata) -> None:
    conn.execute(
        """
        INSERT INTO imdb_ratings_ingest_state (
            id, dataset_url, etag, last_modified, sha256, content_length, row_count,
            last_checked_at, last_successful_refresh_at, last_error
        )
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            dataset_url = excluded.dataset_url,
            etag = excluded.etag,
            last_modified = excluded.last_modified,
            sha256 = excluded.sha256,
            content_length = excluded.content_length,
            row_count = excluded.row_count,
            last_checked_at = excluded.last_checked_at,
            last_successful_refresh_at = excluded.last_successful_refresh_at,
            last_error = excluded.last_error,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            metadata.dataset_url,
            metadata.etag,
            metadata.last_modified,
            metadata.sha256,
            metadata.content_length,
            metadata.row_count,
            _serialize_datetime(metadata.last_checked_at),
            _serialize_datetime(metadata.last_successful_refresh_at),
            metadata.last_error,
        ),
    )


def _state_from_row(row: sqlite3.Row) -> IMDbCacheState:
    return IMDbCacheState(
        dataset_url=row["dataset_url"],
        etag=row["etag"],
        last_modified=row["last_modified"],
        sha256=row["sha256"],
        content_length=row["content_length"],
        row_count=row["row_count"],
        last_checked_at=_parse_datetime(row["last_checked_at"]),
        last_successful_refresh_at=_parse_datetime(row["last_successful_refresh_at"]),
        last_error=row["last_error"],
    )


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
