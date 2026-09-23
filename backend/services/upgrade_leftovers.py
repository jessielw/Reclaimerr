"""Upgrade leftovers: files Radarr imported from its download folder and has since
replaced.

After an upgrade the original download can stay in the download folder. The
media server never sees it, so media-server duplicate detection can't find it.
Radarr's history can: every ``downloadFolderImported`` event records the source
file as ``droppedPath``. For each movie Radarr still has, every import except the
latest one has been replaced, so its ``droppedPath`` is a leftover if the file is
still on disk.

Detect broadly, act narrowly: anything odd gets a ``manual_reason``. The scan
rewrites ``upgrade_leftovers``; only the user's ``ignored`` flag survives it.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.utils.filesystem import (
    mapped_path_variants,
    normalize_fpath,
    resolve_path,
)
from backend.core.utils.misc import as_int
from backend.database import async_db
from backend.database.models import (
    GeneralSettings,
    Movie,
    MovieArrRef,
    UpgradeLeftover,
)
from backend.models.services.radarr import RadarrMovie
from backend.services.duplicates import DuplicateActionError
from backend.services.radarr import RadarrClient

MANUAL_NOT_A_FILE = "Path is a folder, not a single file"
MANUAL_CURRENT_FILE_MISSING = "Couldn't find the current movie file on disk to compare"
MANUAL_NO_HARDLINK_INFO = (
    "This filesystem doesn't report hardlinks, so the file can't be checked"
)

# download roots listed in the "add a path mapping" banner
MAX_UNMAPPED_ROOTS = 10

# what may be left in a release folder that is still removed after a delete
_JUNK_SUFFIXES = frozenset(
    {
        ".nfo",
        ".txt",
        ".jpg",
        ".jpeg",
        ".png",
        ".sfv",
        ".srr",
        ".url",
        ".nzb",
        ".par2",
        ".srt",
        ".sub",
        ".idx",
        ".ass",
        ".ssa",
    }
)
_JUNK_DIRS = frozenset(
    {
        "sample",
        "samples",
        "proof",
        "proofs",
        "screens",
        "screenshots",
        "subs",
        "subtitles",
    }
)


@dataclass(slots=True, frozen=True)
class ImportEvent:
    arr_movie_id: int
    dropped_path: str
    imported_at: datetime | None
    source_title: str | None


@dataclass(slots=True)
class FoundLeftover:
    event: ImportEvent
    local_path: Path
    size: int
    link_count: int
    manual_reason: str | None = None
    file_key: str | None = None


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def parse_import_events(records: Iterable[Mapping[str, Any]]) -> list[ImportEvent]:
    """Import events with a movie id and a dropped path; everything else is skipped."""
    events: list[ImportEvent] = []
    for record in records:
        movie_id = as_int(record.get("movieId"))
        data = record.get("data")
        dropped = data.get("droppedPath") if isinstance(data, Mapping) else None
        if not movie_id or not isinstance(dropped, str) or not dropped.strip():
            continue
        source_title = record.get("sourceTitle")
        events.append(
            ImportEvent(
                arr_movie_id=movie_id,
                dropped_path=dropped.strip(),
                imported_at=_parse_date(record.get("date")),
                source_title=source_title if isinstance(source_title, str) else None,
            )
        )
    return events


def split_imports(
    events: Iterable[ImportEvent],
) -> tuple[list[ImportEvent], list[ImportEvent]]:
    """Each movie's latest import, and every earlier one (one per dropped path).

    A path the latest import also came from is never superseded: that is the
    current file's source, re-imported.
    """
    by_movie: dict[int, list[ImportEvent]] = defaultdict(list)
    for event in events:
        by_movie[event.arr_movie_id].append(event)

    latest: list[ImportEvent] = []
    superseded: list[ImportEvent] = []
    for movie_events in by_movie.values():
        movie_events.sort(key=lambda e: e.imported_at or datetime.min, reverse=True)
        latest.append(movie_events[0])
        seen = {normalize_fpath(movie_events[0].dropped_path, strip_ending_slash=True)}
        for event in movie_events[1:]:
            key = normalize_fpath(event.dropped_path, strip_ending_slash=True)
            if key not in seen:
                seen.add(key)
                superseded.append(event)
    return latest, superseded


def _file_id(st: os.stat_result) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def file_key(st: os.stat_result) -> str:
    return f"{st.st_dev}:{st.st_ino}"


def check_leftover(
    event: ImportEvent, local_path: Path, current_file: Path | None
) -> FoundLeftover | None:
    """Stat a resolved leftover path. None when it is the current file itself."""
    try:
        st = local_path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return FoundLeftover(event, local_path, 0, 1, MANUAL_NOT_A_FILE)

    def manual(reason: str) -> FoundLeftover:
        return FoundLeftover(
            event, local_path, st.st_size, st.st_nlink, reason, file_key(st)
        )

    # some network shares report inode 0 for everything
    if st.st_ino == 0:
        return manual(MANUAL_NO_HARDLINK_INFO)
    if current_file is None:
        return manual(MANUAL_CURRENT_FILE_MISSING)
    try:
        current = current_file.stat()
    except OSError:
        return manual(MANUAL_CURRENT_FILE_MISSING)
    if current.st_ino == 0:
        return manual(MANUAL_NO_HARDLINK_INFO)
    if _file_id(current) == _file_id(st):
        return None
    return FoundLeftover(
        event, local_path, st.st_size, st.st_nlink, file_key=file_key(st)
    )


@dataclass(slots=True)
class _Instance:
    config_id: int
    movies: dict[int, RadarrMovie]
    latest: list[ImportEvent]
    superseded: list[ImportEvent]

    def variants(self, path: str | None, mappings: list[dict[str, Any]]) -> set[str]:
        return mapped_path_variants(
            path, mappings, service_type="radarr", service_config_id=self.config_id
        )


@dataclass(slots=True)
class _InUse:
    """Paths no instance may flag, as normalized raw and path-mapped variants."""

    # every movie's latest import, on any instance
    import_paths: set[str] = field(default_factory=set)
    # every Radarr movie folder, on any instance
    movie_folders: set[str] = field(default_factory=set)

    def covers(self, variants: set[str]) -> bool:
        if variants & self.import_paths:
            return True
        for variant in variants:
            parent = variant.rpartition("/")[0]
            while parent:
                if parent in self.movie_folders:
                    return True
                parent = parent.rpartition("/")[0]
        return False


def _in_use(instances: Sequence[_Instance], mappings: list[dict[str, Any]]) -> _InUse:
    in_use = _InUse()
    for inst in instances:
        for event in inst.latest:
            in_use.import_paths |= inst.variants(event.dropped_path, mappings)
        for movie in inst.movies.values():
            in_use.movie_folders |= inst.variants(movie.path, mappings)
    return in_use


def download_root(path: str) -> str:
    """The top folder of a Radarr path, where a path mapping usually starts.

    ``/downloads/x/y.mkv`` -> ``/downloads``, ``D:\\Downloads\\y.mkv`` ->
    ``D:/Downloads``, ``\\\\nas\\share\\y.mkv`` -> ``//nas/share``. Download files
    sit at different depths, so anything relative to the file itself isn't stable.
    """
    normalized = normalize_fpath(path, strip_ending_slash=True)
    segments = [s for s in normalized.split("/") if s]
    unc = normalized.startswith("//")
    keep = 2 if unc or (segments and segments[0].endswith(":")) else 1
    prefix = "//" if unc else "/" if normalized.startswith("/") else ""
    return prefix + "/".join(segments[:keep])


def _is_checkable(inst: _Instance, path: str, mappings: list[dict[str, Any]]) -> bool:
    """Whether a path that didn't resolve is really gone rather than unreachable.

    A mapped path is trusted. An unmapped one counts when its top folder exists
    here, as on bare-metal installs.
    """
    return len(inst.variants(path, mappings)) > 1 or Path(download_root(path)).is_dir()


async def _load_path_mappings() -> list[dict[str, Any]]:
    async with async_db() as db:
        mappings = (await db.execute(select(GeneralSettings.path_mappings))).scalar()
    return list(mappings or [])


async def _fetch_instance(config_id: int, client: RadarrClient) -> _Instance:
    movies = {m.id: m for m in await client.get_all_movies()}
    latest, superseded = split_imports(
        parse_import_events(await client.get_import_history())
    )
    return _Instance(
        config_id=config_id,
        movies=movies,
        latest=latest,
        # a movie Radarr no longer has isn't an upgrade; leave its downloads alone
        superseded=[e for e in superseded if e.arr_movie_id in movies],
    )


def _find_leftovers(
    inst: _Instance, in_use: _InUse, mappings: list[dict[str, Any]]
) -> tuple[list[FoundLeftover], set[str]]:
    """Leftovers on disk for one instance, plus download roots it couldn't reach."""
    found: list[FoundLeftover] = []
    unmapped: set[str] = set()
    current_files: dict[int, Path | None] = {}
    for event in inst.superseded:
        if in_use.covers(inst.variants(event.dropped_path, mappings)):
            continue
        local = resolve_path(
            event.dropped_path,
            mappings,
            service_type="radarr",
            service_config_id=inst.config_id,
            warn_missing=False,
        )
        if local is None:
            # usually the file is simply gone
            if not _is_checkable(inst, event.dropped_path, mappings):
                unmapped.add(download_root(event.dropped_path))
            continue
        movie_id = event.arr_movie_id
        if movie_id not in current_files:
            current_files[movie_id] = resolve_path(
                inst.movies[movie_id].file_path,
                mappings,
                service_type="radarr",
                service_config_id=inst.config_id,
                warn_missing=False,
            )
        leftover = check_leftover(event, local, current_files[movie_id])
        if leftover is not None:
            found.append(leftover)
    return found, unmapped


async def _store(
    config_id: int, found: Sequence[FoundLeftover], movies: Mapping[int, RadarrMovie]
) -> None:
    async with async_db() as db:
        existing = {
            row.dropped_path: row
            for row in (
                await db.execute(
                    select(UpgradeLeftover).where(
                        UpgradeLeftover.service_config_id == config_id
                    )
                )
            ).scalars()
        }
        ref_rows = await db.execute(
            select(MovieArrRef.arr_movie_id, MovieArrRef.movie_id).where(
                MovieArrRef.service_config_id == config_id
            )
        )
        refs = {arr_id: movie_id for arr_id, movie_id in ref_rows.all()}
        keep: set[str] = set()
        for f in found:
            event = f.event
            keep.add(event.dropped_path)
            movie = movies[event.arr_movie_id]
            row = existing.get(event.dropped_path)
            if row is None:
                row = UpgradeLeftover(
                    service_config_id=config_id,
                    arr_movie_id=event.arr_movie_id,
                    title=movie.title,
                    dropped_path=event.dropped_path,
                    local_path=str(f.local_path),
                    size=f.size,
                    link_count=f.link_count,
                )
                db.add(row)
            row.arr_movie_id = event.arr_movie_id
            row.title = movie.title
            row.year = movie.year
            row.movie_id = refs.get(event.arr_movie_id)
            row.local_path = str(f.local_path)
            row.size = f.size
            row.link_count = f.link_count
            row.file_key = f.file_key
            row.source_title = event.source_title
            row.imported_at = event.imported_at
            row.manual_reason = f.manual_reason
        stale = [row.id for path, row in existing.items() if path not in keep]
        if stale:
            await db.execute(
                delete(UpgradeLeftover).where(UpgradeLeftover.id.in_(stale))
            )
        await db.commit()


async def scan_upgrade_leftovers() -> dict[str, int]:
    """Rescan every configured Radarr instance.

    All instances are fetched before anything is flagged, because one instance's
    current download can sit where another instance imported from. If any fetch
    fails the scan stops and the previous results stay.
    """
    mappings = await _load_path_mappings()
    instances: list[_Instance] = []
    for config_id, client in service_manager.radarr_clients().items():
        try:
            instances.append(await _fetch_instance(config_id, client))
        except Exception as exc:
            raise RuntimeError(
                f"Upgrade leftovers: couldn't read Radarr (config {config_id}), "
                f"previous results kept: {exc}"
            ) from exc

    in_use = _in_use(instances, mappings)
    total = 0
    unmapped: set[str] = set()
    for inst in instances:
        found, inst_unmapped = _find_leftovers(inst, in_use, mappings)
        await _store(inst.config_id, found, inst.movies)
        total += len(found)
        unmapped |= inst_unmapped

    if unmapped:
        LOG.warning(
            "Upgrade leftovers: couldn't reach some Radarr download folders; "
            f"add a path mapping for: {sorted(unmapped)}"
        )
    await _finish_scan([inst.config_id for inst in instances], sorted(unmapped))
    return {"leftovers": total, "unmapped_roots": len(unmapped)}


async def _finish_scan(config_ids: list[int], unmapped: list[str]) -> None:
    """Drop rows of instances that are gone or disabled; save the scan summary."""
    async with async_db() as db:
        await db.execute(
            delete(UpgradeLeftover).where(
                UpgradeLeftover.service_config_id.not_in(config_ids)
            )
        )
        settings = (await db.execute(select(GeneralSettings))).scalars().first()
        if settings is not None:
            settings.upgrade_leftover_scan = {
                "scanned_at": datetime.now(UTC).isoformat(),
                "unmapped_roots": unmapped[:MAX_UNMAPPED_ROOTS],
            }
        await db.commit()


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LeftoverView:
    row: UpgradeLeftover
    poster_url: str | None

    @property
    def frees_space(self) -> bool:
        return self.row.link_count <= 1

    @property
    def actionable(self) -> bool:
        return self.row.manual_reason is None and not self.row.ignored

    @property
    def reclaimable_size(self) -> int:
        return self.row.size if self.actionable and self.frees_space else 0


async def load_leftovers(
    db: AsyncSession,
    *,
    search: str | None = None,
    ids: Sequence[int] | None = None,
) -> list[LeftoverView]:
    """Stored leftovers sorted by title, then oldest import first."""
    query = select(UpgradeLeftover, Movie.poster_url).outerjoin(
        Movie, Movie.id == UpgradeLeftover.movie_id
    )
    search = (search or "").strip()
    if search:
        query = query.where(UpgradeLeftover.title.ilike(f"%{search}%"))
    if ids is not None:
        query = query.where(UpgradeLeftover.id.in_(ids))
    views = [LeftoverView(row, poster) for row, poster in (await db.execute(query))]
    views.sort(
        key=lambda v: (
            v.row.title.casefold(),
            v.row.year or 0,
            v.row.imported_at or datetime.min,
        )
    )
    return views


async def load_scan_summary(db: AsyncSession) -> tuple[datetime | None, list[str]]:
    """When the last scan finished and which download roots it couldn't reach."""
    summary = (
        await db.execute(select(GeneralSettings.upgrade_leftover_scan))
    ).scalar() or {}
    scanned_at = _parse_date(summary.get("scanned_at"))
    roots = summary.get("unmapped_roots")
    return scanned_at, [str(r) for r in roots] if isinstance(roots, list) else []


# ---------------------------------------------------------------------------
# deleting
# ---------------------------------------------------------------------------


def check_before_delete(
    row: UpgradeLeftover,
    movie: RadarrMovie,
    mappings: list[dict[str, Any]],
) -> os.stat_result:
    """Re-check a leftover against disk and Radarr as they are now.

    The scan can be a day old: the path may hold another file, or Radarr may
    have re-imported it. Raises ``DuplicateActionError`` with the reason.
    """
    if row.manual_reason:
        raise DuplicateActionError(f"Needs manual review: {row.manual_reason}")
    inst = _Instance(row.service_config_id, {movie.id: movie}, [], [])
    guard = _InUse(movie_folders=inst.variants(movie.path, mappings))
    if guard.covers(inst.variants(row.dropped_path, mappings)):
        raise DuplicateActionError("File is inside the movie's library folder")

    local = Path(row.local_path)
    try:
        st = local.stat()
    except FileNotFoundError as e:
        raise DuplicateActionError("File is already gone - rescan") from e
    if (
        not stat.S_ISREG(st.st_mode)
        or file_key(st) != row.file_key
        or st.st_size != row.size
    ):
        raise DuplicateActionError("File changed since the scan - rescan")

    current = resolve_path(
        movie.file_path,
        mappings,
        service_type="radarr",
        service_config_id=row.service_config_id,
        warn_missing=False,
    )
    if current is None:
        raise DuplicateActionError(MANUAL_CURRENT_FILE_MISSING)
    if _file_id(current.stat()) == _file_id(st):
        raise DuplicateActionError("Radarr now uses this file")
    return st


def remove_release_folder(folder: Path, release_names: Iterable[str]) -> bool:
    """Remove a release folder once only non-media files are left in it.

    Only a folder named after the release (Radarr's source title or the file's
    own name) is touched, so a shared download category folder never is.
    """
    names = {name.casefold() for name in release_names if name}
    if folder.name.casefold() not in names:
        return False
    try:
        entries = list(folder.iterdir())
    except OSError:
        return False
    for entry in entries:
        if entry.is_symlink():
            return False
        if entry.is_dir():
            if entry.name.casefold() not in _JUNK_DIRS:
                return False
        elif entry.suffix.casefold() not in _JUNK_SUFFIXES:
            return False
    try:
        shutil.rmtree(folder)
    except OSError as e:
        LOG.warning(f"Upgrade leftovers: couldn't remove release folder {folder}: {e}")
        return False
    return True
