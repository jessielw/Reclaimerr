"""Duplicate detection: media items the main media server holds more than one file for.

Detect broadly, act narrowly. Every group is either actionable or carries a
``manual_reason``; anything unusual gets a reason and is left to the user
rather than growing special handling here.

Groups are computed on request from ``movie_versions`` / ``episode_versions``.
Nothing about a group is stored except the user's "not a duplicate" marks.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.protection_scope import active_protection_clause
from backend.core.utils.filesystem import normalize_fpath
from backend.database.models import (
    DuplicateIgnore,
    Episode,
    EpisodeVersion,
    GeneralSettings,
    Movie,
    MovieVersion,
    ProtectedMedia,
    Season,
    Series,
)
from backend.enums import MediaType, Service

MANUAL_MULTI_EPISODE = "File contains multiple episodes"
MANUAL_SHARED_ITEM = "Server can't delete a single version of this item"
MANUAL_INCOMPLETE = "File info incomplete"

_SQL_CHUNK = 500


class KeeperCriterion(StrEnum):
    RESOLUTION = "resolution"
    DOLBY_VISION = "dolby_vision"
    HDR = "hdr"
    VIDEO_CODEC = "video_codec"
    AUDIO_CHANNELS = "audio_channels"
    VIDEO_BITRATE = "video_bitrate"
    SIZE_LARGER = "size_larger"
    SIZE_SMALLER = "size_smaller"


DEFAULT_KEEPER_PRIORITY: tuple[tuple[KeeperCriterion, bool], ...] = (
    (KeeperCriterion.RESOLUTION, True),
    (KeeperCriterion.DOLBY_VISION, True),
    (KeeperCriterion.HDR, True),
    (KeeperCriterion.VIDEO_CODEC, True),
    (KeeperCriterion.AUDIO_CHANNELS, True),
    (KeeperCriterion.VIDEO_BITRATE, True),
    (KeeperCriterion.SIZE_LARGER, False),
    (KeeperCriterion.SIZE_SMALLER, False),
)

# newer codecs keep the same quality in less space
_CODEC_RANK: dict[str, int] = {
    "h266": 5,
    "vvc": 5,
    "av1": 4,
    "h265": 3,
    "h264": 2,
    "avc": 2,
}

_RESOLUTION_HEIGHT: dict[str, int] = {
    "8k": 4320,
    "4k": 2160,
    "2160": 2160,
    "2160p": 2160,
    "1440": 1440,
    "1440p": 1440,
    "1080": 1080,
    "1080p": 1080,
    "720": 720,
    "720p": 720,
    "576": 576,
    "480": 480,
    "sd": 480,
}


@dataclass(slots=True)
class DuplicateFile:
    """One physical file. ``version_ids`` holds every row describing it (twins)."""

    version_ids: list[int]
    service: Service
    service_item_ids: list[str]
    library_ids: list[str]
    library_names: list[str]
    path: str | None
    size: int
    added_at: datetime | None
    video_resolution: str | None
    video_width: int | None
    video_height: int | None
    video_codec_family: str | None
    video_hdr: bool | None
    video_dolby_vision: bool | None
    video_bitrate: int | None
    audio_codec_family: str | None
    audio_channels: int | None
    protected: bool = False


@dataclass(slots=True)
class DuplicateGroup:
    media_type: MediaType
    # movies.id for movies, episodes.id for episodes
    item_id: int
    title: str
    year: int | None
    poster_url: str | None
    # best first: files[0] is the suggested keeper
    files: list[DuplicateFile]
    series_id: int | None = None
    season_id: int | None = None
    season_number: int | None = None
    episode_number: int | None = None
    episode_name: str | None = None
    manual_reason: str | None = None
    cross_library: bool = False
    ignored: bool = False
    fingerprint: str = ""

    @property
    def key(self) -> str:
        kind = "movie" if self.media_type is MediaType.MOVIE else "episode"
        return f"{kind}:{self.item_id}"

    @property
    def reclaimable_size(self) -> int:
        return sum(f.size for f in self.files[1:] if not f.protected)


@dataclass(slots=True)
class _Row:
    """Common view of a MovieVersion / EpisodeVersion row."""

    id: int
    service: Service
    service_item_id: str
    library_id: str
    library_name: str
    path: str | None
    size: int
    added_at: datetime | None
    video_resolution: str | None
    video_width: int | None
    video_height: int | None
    video_codec_family: str | None
    video_hdr: bool | None
    video_dolby_vision: bool | None
    video_bitrate: int | None
    audio_codec_family: str | None
    audio_channels: int | None

    @classmethod
    def of(cls, row: MovieVersion | EpisodeVersion) -> _Row:
        return cls(
            id=row.id,
            service=row.service,
            service_item_id=row.service_item_id,
            library_id=row.library_id,
            library_name=row.library_name,
            path=row.path,
            size=row.size or 0,
            added_at=row.added_at,
            video_resolution=row.video_resolution,
            video_width=row.video_width,
            video_height=row.video_height,
            video_codec_family=row.video_codec_family,
            video_hdr=row.video_hdr,
            video_dolby_vision=row.video_dolby_vision,
            video_bitrate=row.video_bitrate,
            audio_codec_family=row.audio_codec_family,
            audio_channels=row.audio_channels,
        )


@dataclass(slots=True)
class _Protection:
    whole: bool = False
    movie_version_ids: set[int] = field(default_factory=set)


# ---------------------------------------------------------------------------
# keeper priority
# ---------------------------------------------------------------------------


def normalize_keeper_priority(
    raw: Iterable[Mapping[str, Any]] | None,
) -> list[tuple[KeeperCriterion, bool]]:
    """Validate a stored/submitted priority list.

    Unknown keys are dropped, missing criteria are appended disabled, and only
    the first enabled of size_larger/size_smaller stays enabled.
    """
    if raw is None:
        return list(DEFAULT_KEEPER_PRIORITY)

    result: list[tuple[KeeperCriterion, bool]] = []
    seen: set[KeeperCriterion] = set()
    for entry in raw:
        try:
            criterion = KeeperCriterion(str(entry.get("key")))
        except ValueError:
            continue
        if criterion in seen:
            continue
        seen.add(criterion)
        result.append((criterion, bool(entry.get("enabled", True))))
    for criterion, _ in DEFAULT_KEEPER_PRIORITY:
        if criterion not in seen:
            result.append((criterion, False))

    size_keys = {KeeperCriterion.SIZE_LARGER, KeeperCriterion.SIZE_SMALLER}
    size_enabled = False
    for index, (criterion, enabled) in enumerate(result):
        if criterion in size_keys and enabled:
            if size_enabled:
                result[index] = (criterion, False)
            size_enabled = True
    return result


def keeper_priority_payload(
    priority: Sequence[tuple[KeeperCriterion, bool]],
) -> list[dict[str, Any]]:
    return [{"key": c.value, "enabled": enabled} for c, enabled in priority]


async def load_keeper_priority(
    db: AsyncSession,
) -> list[tuple[KeeperCriterion, bool]]:
    raw = (
        await db.execute(select(GeneralSettings.duplicate_keeper_priority).limit(1))
    ).scalar_one_or_none()
    return normalize_keeper_priority(raw)


def _resolution_height(f: DuplicateFile) -> int:
    if f.video_height and f.video_width:
        # letterboxed encodes are short but full width; rank by width equivalent
        return max(f.video_height, round(f.video_width * 9 / 16))
    if f.video_height:
        return f.video_height
    label = (f.video_resolution or "").strip().lower()
    return _RESOLUTION_HEIGHT.get(label, 0)


def _criterion_value(criterion: KeeperCriterion, f: DuplicateFile) -> int:
    match criterion:
        case KeeperCriterion.RESOLUTION:
            return _resolution_height(f)
        case KeeperCriterion.DOLBY_VISION:
            return int(bool(f.video_dolby_vision))
        case KeeperCriterion.HDR:
            return int(bool(f.video_hdr or f.video_dolby_vision))
        case KeeperCriterion.VIDEO_CODEC:
            return _CODEC_RANK.get((f.video_codec_family or "").lower(), 1)
        case KeeperCriterion.AUDIO_CHANNELS:
            return f.audio_channels or 0
        case KeeperCriterion.VIDEO_BITRATE:
            return f.video_bitrate or 0
        case KeeperCriterion.SIZE_LARGER:
            return f.size
        case KeeperCriterion.SIZE_SMALLER:
            return -f.size


def rank_files(
    files: Sequence[DuplicateFile],
    priority: Sequence[tuple[KeeperCriterion, bool]],
) -> list[DuplicateFile]:
    """Best first. Ties fall back to the newest file, then the lowest row id."""
    enabled = [c for c, on in priority if on]

    def sort_key(f: DuplicateFile) -> tuple[Any, ...]:
        added = f.added_at.timestamp() if f.added_at else 0.0
        return (
            *(_criterion_value(c, f) for c in enabled),
            added,
            -min(f.version_ids),
        )

    return sorted(files, key=sort_key, reverse=True)


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------


def _file_key(row: _Row) -> str:
    # only the main server writes version rows, so equal paths are one file
    if row.path:
        return normalize_fpath(row.path, strip_ending_slash=True)
    return f"#row:{row.id}"


def collapse_twins(rows: Iterable[_Row]) -> list[DuplicateFile]:
    """Merge rows that describe the same physical file (same file, two libraries)."""
    files: dict[str, DuplicateFile] = {}
    for row in sorted(rows, key=lambda r: r.id):
        key = _file_key(row)
        existing = files.get(key)
        if existing is None:
            files[key] = DuplicateFile(
                version_ids=[row.id],
                service=row.service,
                service_item_ids=[row.service_item_id],
                library_ids=[row.library_id],
                library_names=[row.library_name],
                path=row.path,
                size=row.size,
                added_at=row.added_at,
                video_resolution=row.video_resolution,
                video_width=row.video_width,
                video_height=row.video_height,
                video_codec_family=row.video_codec_family,
                video_hdr=row.video_hdr,
                video_dolby_vision=row.video_dolby_vision,
                video_bitrate=row.video_bitrate,
                audio_codec_family=row.audio_codec_family,
                audio_channels=row.audio_channels,
            )
            continue
        existing.version_ids.append(row.id)
        if row.service_item_id not in existing.service_item_ids:
            existing.service_item_ids.append(row.service_item_id)
        if row.library_id not in existing.library_ids:
            existing.library_ids.append(row.library_id)
            existing.library_names.append(row.library_name)
    return list(files.values())


def fingerprint_files(files: Iterable[DuplicateFile]) -> str:
    keys = sorted(
        normalize_fpath(f.path, strip_ending_slash=True)
        if f.path
        else f"#row:{min(f.version_ids)}"
        for f in files
    )
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def _is_cross_library(files: Sequence[DuplicateFile]) -> bool:
    """True when no single library holds every file."""
    common = set(files[0].library_ids)
    for f in files[1:]:
        common &= set(f.library_ids)
    return not common


def _manual_reason(
    files: Sequence[DuplicateFile],
    *,
    multi_episode_paths: set[str] | None = None,
) -> str | None:
    if any(not f.path or f.size <= 0 for f in files):
        return MANUAL_INCOMPLETE
    if multi_episode_paths and any(
        normalize_fpath(f.path, strip_ending_slash=True) in multi_episode_paths
        for f in files
        if f.path
    ):
        return MANUAL_MULTI_EPISODE
    # Jellyfin/Emby delete whole items, so two files under one item id cannot
    # be split apart from here. Plex deletes a single Media entry.
    item_owner: dict[str, int] = {}
    for index, f in enumerate(files):
        if f.service is Service.PLEX:
            continue
        for item_id in f.service_item_ids:
            owner = item_owner.setdefault(item_id, index)
            if owner != index:
                return MANUAL_SHARED_ITEM
    return None


def build_group(
    *,
    media_type: MediaType,
    item_id: int,
    rows: Sequence[_Row],
    priority: Sequence[tuple[KeeperCriterion, bool]],
    title: str,
    year: int | None,
    poster_url: str | None,
    protection: _Protection | None = None,
    multi_episode_paths: set[str] | None = None,
    ignore_fingerprint: str | None = None,
    **extra: Any,
) -> DuplicateGroup | None:
    """Group one item's rows; None when they describe fewer than two files."""
    files = collapse_twins(rows)
    if len(files) < 2:
        return None
    if protection is not None:
        for f in files:
            f.protected = protection.whole or bool(
                protection.movie_version_ids.intersection(f.version_ids)
            )
    fingerprint = fingerprint_files(files)
    return DuplicateGroup(
        media_type=media_type,
        item_id=item_id,
        title=title,
        year=year,
        poster_url=poster_url,
        files=rank_files(files, priority),
        manual_reason=_manual_reason(files, multi_episode_paths=multi_episode_paths),
        cross_library=_is_cross_library(files),
        ignored=ignore_fingerprint == fingerprint,
        fingerprint=fingerprint,
        **extra,
    )


def _chunks(values: Sequence[Any]) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), _SQL_CHUNK):
        yield values[start : start + _SQL_CHUNK]


async def _ignored_fingerprints(
    db: AsyncSession, media_type: MediaType, item_ids: Sequence[int]
) -> dict[int, str]:
    result: dict[int, str] = {}
    for chunk in _chunks(item_ids):
        rows = await db.execute(
            select(DuplicateIgnore.item_id, DuplicateIgnore.fingerprint).where(
                DuplicateIgnore.media_type == media_type,
                DuplicateIgnore.item_id.in_(chunk),
            )
        )
        result.update({item_id: fp for item_id, fp in rows.all()})
    return result


async def _movie_groups(
    db: AsyncSession,
    priority: Sequence[tuple[KeeperCriterion, bool]],
    *,
    movie_ids: Sequence[int] | None,
    search: str | None,
) -> list[DuplicateGroup]:
    multi = (
        select(MovieVersion.movie_id)
        .group_by(MovieVersion.movie_id)
        .having(func.count(MovieVersion.id) > 1)
    )
    if movie_ids is not None:
        multi = multi.where(MovieVersion.movie_id.in_(movie_ids))
    query = (
        select(MovieVersion, Movie.title, Movie.year, Movie.poster_url)
        .join(Movie, Movie.id == MovieVersion.movie_id)
        .where(MovieVersion.movie_id.in_(multi), Movie.removed_at.is_(None))
    )
    if search:
        query = query.where(Movie.title.ilike(f"%{search}%"))

    rows_by_movie: dict[int, list[_Row]] = defaultdict(list)
    meta: dict[int, tuple[str, int | None, str | None]] = {}
    for version, title, year, poster_url in (await db.execute(query)).all():
        rows_by_movie[version.movie_id].append(_Row.of(version))
        meta[version.movie_id] = (title, year, poster_url)
    if not rows_by_movie:
        return []

    ids = list(rows_by_movie)
    protections: dict[int, _Protection] = defaultdict(_Protection)
    for chunk in _chunks(ids):
        prot_rows = await db.execute(
            select(ProtectedMedia.movie_id, ProtectedMedia.movie_version_id).where(
                ProtectedMedia.media_type == MediaType.MOVIE,
                ProtectedMedia.movie_id.in_(chunk),
                active_protection_clause(datetime.now(UTC)),
            )
        )
        for movie_id, version_id in prot_rows.all():
            if version_id is None:
                protections[movie_id].whole = True
            else:
                protections[movie_id].movie_version_ids.add(version_id)
    ignored = await _ignored_fingerprints(db, MediaType.MOVIE, ids)

    groups: list[DuplicateGroup] = []
    for movie_id, rows in rows_by_movie.items():
        title, year, poster_url = meta[movie_id]
        group = build_group(
            media_type=MediaType.MOVIE,
            item_id=movie_id,
            rows=rows,
            priority=priority,
            title=title,
            year=year,
            poster_url=poster_url,
            protection=protections.get(movie_id),
            ignore_fingerprint=ignored.get(movie_id),
        )
        if group is not None:
            groups.append(group)
    return groups


async def _episode_groups(
    db: AsyncSession,
    priority: Sequence[tuple[KeeperCriterion, bool]],
    *,
    episode_ids: Sequence[int] | None,
    search: str | None,
) -> list[DuplicateGroup]:
    multi = (
        select(EpisodeVersion.episode_id)
        .group_by(EpisodeVersion.episode_id)
        .having(func.count(EpisodeVersion.id) > 1)
    )
    if episode_ids is not None:
        multi = multi.where(EpisodeVersion.episode_id.in_(episode_ids))
    query = (
        select(
            EpisodeVersion,
            Episode.episode_number,
            Episode.name,
            Season.id,
            Season.season_number,
            Series.id,
            Series.title,
            Series.year,
            Series.poster_url,
        )
        .join(Episode, Episode.id == EpisodeVersion.episode_id)
        .join(Season, Season.id == Episode.season_id)
        .join(Series, Series.id == Season.series_id)
        .where(EpisodeVersion.episode_id.in_(multi), Series.removed_at.is_(None))
    )
    if search:
        query = query.where(Series.title.ilike(f"%{search}%"))

    rows_by_episode: dict[int, list[_Row]] = defaultdict(list)
    meta: dict[int, dict[str, Any]] = {}
    for (
        version,
        episode_number,
        episode_name,
        season_id,
        season_number,
        series_id,
        title,
        year,
        poster_url,
    ) in (await db.execute(query)).all():
        rows_by_episode[version.episode_id].append(_Row.of(version))
        meta[version.episode_id] = {
            "title": title,
            "year": year,
            "poster_url": poster_url,
            "series_id": series_id,
            "season_id": season_id,
            "season_number": season_number,
            "episode_number": episode_number,
            "episode_name": episode_name,
        }
    if not rows_by_episode:
        return []

    # a path shared by several episodes is a multi-episode file
    paths = sorted(
        {r.path for rows in rows_by_episode.values() for r in rows if r.path}
    )
    multi_episode_paths: set[str] = set()
    for chunk in _chunks(paths):
        shared = await db.execute(
            select(EpisodeVersion.path)
            .where(EpisodeVersion.path.in_(chunk))
            .group_by(EpisodeVersion.path)
            .having(func.count(distinct(EpisodeVersion.episode_id)) > 1)
        )
        multi_episode_paths.update(
            normalize_fpath(p, strip_ending_slash=True) for (p,) in shared.all() if p
        )

    series_ids = sorted({m["series_id"] for m in meta.values()})
    series_prot: dict[int, list[tuple[int | None, int | None]]] = defaultdict(list)
    for chunk in _chunks(series_ids):
        prot_rows = await db.execute(
            select(
                ProtectedMedia.series_id,
                ProtectedMedia.season_id,
                ProtectedMedia.episode_id,
            ).where(
                ProtectedMedia.media_type == MediaType.SERIES,
                ProtectedMedia.series_id.in_(chunk),
                active_protection_clause(datetime.now(UTC)),
            )
        )
        for series_id, season_id, episode_id in prot_rows.all():
            series_prot[series_id].append((season_id, episode_id))
    ignored = await _ignored_fingerprints(db, MediaType.SERIES, list(rows_by_episode))

    groups: list[DuplicateGroup] = []
    for episode_id, rows in rows_by_episode.items():
        m = meta[episode_id]
        covered = any(
            (p_season is None and p_episode is None)
            or (p_episode is None and p_season == m["season_id"])
            or p_episode == episode_id
            for p_season, p_episode in series_prot.get(m["series_id"], [])
        )
        group = build_group(
            media_type=MediaType.SERIES,
            item_id=episode_id,
            rows=rows,
            priority=priority,
            title=m["title"],
            year=m["year"],
            poster_url=m["poster_url"],
            protection=_Protection(whole=covered),
            multi_episode_paths=multi_episode_paths,
            ignore_fingerprint=ignored.get(episode_id),
            series_id=m["series_id"],
            season_id=m["season_id"],
            season_number=m["season_number"],
            episode_number=m["episode_number"],
            episode_name=m["episode_name"],
        )
        if group is not None:
            groups.append(group)
    return groups


async def load_duplicate_groups(
    db: AsyncSession,
    *,
    media_type: MediaType | None = None,
    search: str | None = None,
    priority: Sequence[tuple[KeeperCriterion, bool]] | None = None,
    movie_ids: Sequence[int] | None = None,
    episode_ids: Sequence[int] | None = None,
) -> list[DuplicateGroup]:
    """Every duplicate group, sorted by title then season/episode.

    Pass ``movie_ids``/``episode_ids`` to load specific groups only; a media type
    whose id list is empty is skipped.
    """
    if priority is None:
        priority = await load_keeper_priority(db)
    search = (search or "").strip() or None

    groups: list[DuplicateGroup] = []
    if media_type in (None, MediaType.MOVIE) and movie_ids != []:
        groups += await _movie_groups(db, priority, movie_ids=movie_ids, search=search)
    if media_type in (None, MediaType.SERIES) and episode_ids != []:
        groups += await _episode_groups(
            db, priority, episode_ids=episode_ids, search=search
        )
    groups.sort(
        key=lambda g: (
            g.title.casefold(),
            g.year or 0,
            g.season_number or 0,
            g.episode_number or 0,
        )
    )
    return groups


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


class DuplicateActionError(ValueError):
    """A duplicate action that cannot run; the message is shown to the user."""


async def load_group(
    db: AsyncSession, *, media_type: MediaType, item_id: int
) -> DuplicateGroup | None:
    is_movie = media_type is MediaType.MOVIE
    groups = await load_duplicate_groups(
        db,
        media_type=media_type,
        movie_ids=[item_id] if is_movie else [],
        episode_ids=[] if is_movie else [item_id],
    )
    return groups[0] if groups else None


async def plan_duplicate_delete(
    db: AsyncSession,
    *,
    media_type: MediaType,
    item_id: int,
    version_ids: Iterable[int],
) -> tuple[DuplicateGroup, list[DuplicateFile]]:
    """Check a delete against the group as it is now and return the files to remove.

    Picking any row of a file picks the whole file, since removing it removes
    every row describing it.
    """
    group = await load_group(db, media_type=media_type, item_id=item_id)
    if group is None:
        raise DuplicateActionError("No longer a duplicate - refresh the page")
    if group.manual_reason:
        raise DuplicateActionError(f"Needs manual review: {group.manual_reason}")

    wanted = set(version_ids)
    known = {version_id for f in group.files for version_id in f.version_ids}
    if not wanted or not wanted <= known:
        raise DuplicateActionError("Files changed since the page loaded - refresh")
    selected = [f for f in group.files if wanted.intersection(f.version_ids)]
    if len(selected) >= len(group.files):
        raise DuplicateActionError("At least one file has to be kept")
    if any(f.protected for f in selected):
        raise DuplicateActionError("A selected file is protected")
    return group, selected
