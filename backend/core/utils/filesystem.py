from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from os import PathLike
from os import rename as os_rename
from pathlib import Path
from typing import Any

from backend.core.logger import LOG

_MEDIA_FILE_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".avi",
    ".divx",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ogm",
    ".ogv",
    ".rmvb",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}

_NON_PRIMARY_MEDIA_STEM_TOKENS = {
    "behindthescenes",
    "deletedscene",
    "deletedscenes",
    "extra",
    "extras",
    "featurette",
    "featurettes",
    "interview",
    "interviews",
    "sample",
    "scene",
    "scenes",
    "short",
    "shorts",
    "trailer",
    "trailers",
}

_KNOWN_ASSET_DIRECTORY_NAMES = {
    "behindthescenes",
    "deletedscene",
    "deletedscenes",
    "extra",
    "extras",
    "featurette",
    "featurettes",
    "interview",
    "interviews",
    "sample",
    "samples",
    "scene",
    "scenes",
    "short",
    "shorts",
    "subs",
    "subtitles",
    "trailer",
    "trailers",
}


@dataclass(frozen=True, slots=True)
class _MediaMovePlan:
    """The safe strategy for moving one media file."""

    move_parent_directory: bool
    reason: str


@dataclass(slots=True)
class _MoveStats:
    """Counts collected while merging into an existing destination."""

    moved_files: int = 0
    deduplicated_files: int = 0


def _compact_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _name_tokens(value: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", value.lower()) if token]


def _mapping_applies_to_scope(
    mapping: Mapping[str, Any],
    *,
    service_type: str | None,
    service_config_id: int | None,
) -> bool:
    mapping_service_type = str(mapping.get("service_type") or "").lower()
    mapping_config_id = mapping.get("service_config_id")
    if mapping_config_id is not None:
        return service_config_id is not None and mapping_config_id == service_config_id
    if mapping_service_type:
        return service_type is not None and mapping_service_type == service_type.lower()
    return True


def mapped_path_variants(
    path: str | None,
    path_mappings: Sequence[Mapping[str, Any]] | None,
    *,
    service_type: str | None = None,
    service_config_id: int | None = None,
) -> set[str]:
    """Return normalized raw and path-mapped variants without filesystem access."""

    if not path:
        return set()
    normalized = normalize_fpath(path, strip_ending_slash=True)
    if not normalized:
        return set()

    variants = {normalized}
    sorted_mappings = sorted(
        path_mappings or [],
        key=lambda mapping: (
            0
            if service_config_id is not None
            and mapping.get("service_config_id") == service_config_id
            else 1
            if service_type
            and str(mapping.get("service_type") or "").lower() == service_type.lower()
            and not mapping.get("service_config_id")
            else 2
            if not mapping.get("service_type") and not mapping.get("service_config_id")
            else 3,
            -len(str(mapping.get("source_prefix") or "")),
        ),
    )
    for mapping in sorted_mappings:
        if not _mapping_applies_to_scope(
            mapping,
            service_type=service_type,
            service_config_id=service_config_id,
        ):
            continue
        source = normalize_fpath(
            str(mapping.get("source_prefix") or ""), strip_ending_slash=True
        )
        local = normalize_fpath(
            str(mapping.get("local_prefix") or ""), strip_ending_slash=True
        )
        if not source or not local:
            continue
        if normalized == source or normalized.startswith(source + "/"):
            variants.add(
                normalize_fpath(
                    local + normalized[len(source) :], strip_ending_slash=True
                )
            )
    return {variant for variant in variants if variant}


def paths_equivalent(
    left: str | None,
    right: str | None,
    path_mappings: Sequence[Mapping[str, Any]] | None,
    *,
    left_service_type: str | None = None,
    right_service_type: str | None = None,
    right_service_config_id: int | None = None,
) -> bool:
    """Return whether two source paths resolve to the same normalized path."""

    left_variants = mapped_path_variants(
        left, path_mappings, service_type=left_service_type
    )
    right_variants = mapped_path_variants(
        right,
        path_mappings,
        service_type=right_service_type,
        service_config_id=right_service_config_id,
    )
    return bool(left_variants & right_variants)


def normalize_fpath(
    path: str | PathLike[str],
    strip_ending_slash: bool = False,
    lower: bool = False,
    upper: bool = False,
) -> str:
    """Normalize path to forward slashes with optional modifiers.

    Args:
        path: The path to normalize.
        strip_ending_slash: If True, remove any trailing slashes.
        lower: If True, convert the path to lowercase.
        upper: If True, convert the path to uppercase.

    Returns:
        The normalized path as a string.
    """
    if not path:
        return ""
    normalized = str(path).strip()
    if not normalized:
        return ""
    normalized = normalized.replace("\\", "/")
    if strip_ending_slash:
        normalized = normalized.rstrip("/")
    if lower:
        normalized = normalized.lower()
    if upper:
        normalized = normalized.upper()
    return normalized


def resolve_path(
    media_server_path: str | None,
    mappings: list[dict[str, Any]] | None,
    service_type: str | None = None,
    service_config_id: int | None = None,
) -> Path | None:
    """Resolve a media server reported path to a local filesystem path.

    Iterates the configured path mappings, replaces any matching source prefix
    with the corresponding local prefix, and returns a ``Path`` only if the
    result exists on disk.  If no mapping matches, the path is tried as-is.

    Args:
        media_server_path: Path as reported by the media server (may use
            Docker/container paths like ``/movies/Title/file.mkv``).
        mappings: List of mapping dicts with ``source_prefix`` and
            ``local_prefix`` keys (as stored in the DB). Mappings may also
            include optional ``service_type`` and ``service_config_id`` fields.
            Exact service-config mappings are preferred over service-type and
            global mappings.

    Returns:
        An existing ``Path`` object, or ``None`` if the file cannot be located.
    """
    if not media_server_path:
        return None

    normalized_media_path = normalize_fpath(media_server_path, strip_ending_slash=True)

    sorted_mappings = sorted(
        mappings or [],
        key=lambda m: (
            0
            if service_config_id is not None
            and m.get("service_config_id") == service_config_id
            else 1
            if service_type
            and str(m.get("service_type") or "").lower() == service_type.lower()
            and not m.get("service_config_id")
            else 2
            if not m.get("service_type") and not m.get("service_config_id")
            else 3,
            -len(str(m.get("source_prefix") or "")),
        ),
    )

    for m in sorted_mappings:
        mapping_service_type = str(m.get("service_type") or "").lower()
        mapping_config_id = m.get("service_config_id")
        if mapping_config_id is not None:
            if service_config_id is None or mapping_config_id != service_config_id:
                continue
        elif mapping_service_type:
            if not service_type or mapping_service_type != service_type.lower():
                continue

        raw_source = str(m.get("source_prefix", "")).strip()
        source = normalize_fpath(raw_source, strip_ending_slash=True)
        if not source and raw_source.replace("\\", "/") == "/":
            source = "/"
        local = str(m.get("local_prefix", "")).strip()
        source_matches = (
            normalized_media_path.startswith("/")
            if source == "/"
            else normalized_media_path == source
            or normalized_media_path.startswith(source + "/")
        )
        if source and source_matches:
            suffix = normalized_media_path[len(source) :].lstrip("/")
            suffix_parts = [part for part in suffix.split("/") if part]
            p = Path(local).joinpath(*suffix_parts) if suffix_parts else Path(local)
            if p.exists():
                LOG.debug(f"resolve_path: mapped {media_server_path!r} to {p}")
                return p
            LOG.warning(
                f"resolve_path: mapped path {p} does not exist "
                f"(source={source!r}, local={local!r})"
            )
            return None

    # no mapping matched: try the path as is (bare metal installs)
    p = Path(media_server_path)
    if p.exists():
        return p

    return None


def sibling_cleanup(local_path: Path) -> None:
    """Deletes all files sharing the same stem as *local_path*, then remove
    the parent directory if it is now empty.

    This handles subtitle files, NFO files, cover images, and the video file
    itself that may have been left on disk after a media-server-only deletion.

    Args:
        local_path: Absolute path to the primary media file that was deleted
            (or its expected location).  Only files with the **same stem** in
            the same directory are removed; other version files are preserved.
    """
    stem = local_path.stem
    parent = local_path.parent

    if not parent.is_dir():
        LOG.debug(f"sibling_cleanup: parent directory not found: {parent}")
        return

    deleted: list[Path] = []
    for f in parent.iterdir():
        if f.is_file() and f.stem == stem:
            try:
                f.unlink()
                deleted.append(f)
            except OSError as e:
                LOG.warning(f"sibling_cleanup: could not delete {f}: {e}")

    if deleted:
        LOG.info(
            f"sibling_cleanup: deleted {len(deleted)} file(s) "
            f"for stem '{stem}' in {parent}"
        )
    else:
        LOG.debug(f"sibling_cleanup: no files found with stem '{stem}' in {parent}")

    # remove parent directory only when it is now completely empty.
    try:
        if not any(parent.iterdir()):
            parent.rmdir()
            LOG.info(f"sibling_cleanup: removed empty directory {parent}")
    except OSError as e:
        LOG.debug(f"sibling_cleanup: could not remove directory {parent}: {e}")


def _destination_relative_parent(
    src: Path,
    path_mappings: Sequence[Mapping[str, Any]] | None,
    *,
    service_type: str | None = None,
    service_config_id: int | None = None,
    fallback_parent: Path | None = None,
) -> Path:
    """Return the folder structure to preserve under a move destination.

    Prefer the path below the matched local path-mapping root. Without a
    matching mapping, fall back to the immediate media folder so file moves do
    not flatten directly into the destination root.
    """
    resolved_src = src.resolve()
    sorted_mappings = sorted(
        path_mappings or [],
        key=lambda mapping: (
            0
            if service_config_id is not None
            and mapping.get("service_config_id") == service_config_id
            else 1
            if service_type
            and str(mapping.get("service_type") or "").lower() == service_type.lower()
            and not mapping.get("service_config_id")
            else 2
            if not mapping.get("service_type") and not mapping.get("service_config_id")
            else 3,
            -len(str(mapping.get("local_prefix") or "")),
        ),
    )
    for mapping in sorted_mappings:
        if not _mapping_applies_to_scope(
            mapping,
            service_type=service_type,
            service_config_id=service_config_id,
        ):
            continue
        local_prefix = str(mapping.get("local_prefix") or "").strip()
        if not local_prefix:
            continue
        try:
            relative = resolved_src.relative_to(Path(local_prefix).resolve())
        except ValueError:
            continue
        return relative.parent

    return fallback_parent if fallback_parent is not None else Path(src.parent.name)


def _sha256_file(path: Path) -> str:
    """Return a streaming SHA-256 digest without loading a media file in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _files_are_identical(src: Path, dest: Path) -> bool:
    """Return whether two regular files have the same bytes."""
    return src.stat().st_size == dest.stat().st_size and _sha256_file(
        src
    ) == _sha256_file(dest)


def _destination_collision_error(src: Path, dest: Path, reason: str) -> FileExistsError:
    return FileExistsError(f"move: cannot merge {src} into {dest}; {reason}")


def _preflight_file_moves(file_moves: Sequence[tuple[Path, Path]]) -> None:
    """Reject unsafe destination collisions before changing any source file."""
    for src, dest in file_moves:
        if not (dest.exists() or dest.is_symlink()):
            continue
        if dest.is_symlink():
            raise _destination_collision_error(src, dest, "destination is a symlink")
        if not dest.is_file():
            raise _destination_collision_error(
                src,
                dest,
                "source file conflicts with a destination directory",
            )
        if not _files_are_identical(src, dest):
            raise _destination_collision_error(
                src,
                dest,
                "destination file has different content",
            )


def _move_single_file_to_path(
    src: Path,
    dest: Path,
    *,
    stats: _MoveStats | None = None,
) -> Path:
    """Move a single file to *dest*, deleting the source afterward.

    Tries an OS-level rename first (fast, atomic, same filesystem).  Falls back
    to explicit copy + verify + delete so the source is guaranteed to be removed
    even across filesystem/network-share boundaries.

    Raises:
        OSError: If the copy or source deletion fails.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink():
            raise _destination_collision_error(src, dest, "destination is a symlink")
        if dest.is_file() and _files_are_identical(src, dest):
            src.unlink()
            if stats:
                stats.deduplicated_files += 1
            LOG.info(f"move: deduplicated identical source file {src}; retained {dest}")
            return dest
        raise _destination_collision_error(
            src,
            dest,
            "destination file has different content"
            if dest.is_file()
            else "source file conflicts with a destination directory",
        )

    # fast path: same filesystem rename
    try:
        os_rename(src, dest)
        if stats:
            stats.moved_files += 1
        return dest
    except OSError:
        pass  # cross device (fall through to copy + delete)

    # copy to destination
    shutil.copy2(str(src), str(dest))

    # verify destination has the expected size before deleting source
    if dest.stat().st_size != src.stat().st_size:
        dest.unlink(missing_ok=True)
        raise OSError(
            f"move_media: destination size mismatch for {dest} "
            f"(expected {src.stat().st_size}, got {dest.stat().st_size})"
        )

    # delete source only after verified copy
    src.unlink()
    if stats:
        stats.moved_files += 1
    return dest


def _move_single_file(
    src: Path,
    destination_root: Path,
    *,
    stats: _MoveStats | None = None,
) -> Path:
    """Move a single file into *destination_root*, deleting the source afterward."""
    return _move_single_file_to_path(src, destination_root / src.name, stats=stats)


def _sorted_directory_entries(path: Path) -> list[Path]:
    return sorted(path.iterdir(), key=lambda entry: entry.name.casefold())


def _preflight_directory_merge(src: Path, dest: Path) -> None:
    """Ensure recursively merging *src* into *dest* cannot overwrite content."""
    if dest.is_symlink():
        raise _destination_collision_error(src, dest, "destination is a symlink")
    if not dest.is_dir():
        raise _destination_collision_error(
            src,
            dest,
            "source directory conflicts with a destination file",
        )

    for source_entry in _sorted_directory_entries(src):
        destination_entry = dest / source_entry.name
        if source_entry.is_symlink():
            raise OSError(f"move: cannot safely merge source symlink {source_entry}")
        if not (destination_entry.exists() or destination_entry.is_symlink()):
            continue
        if destination_entry.is_symlink():
            raise _destination_collision_error(
                source_entry,
                destination_entry,
                "destination is a symlink",
            )
        if source_entry.is_dir():
            if not destination_entry.is_dir():
                raise _destination_collision_error(
                    source_entry,
                    destination_entry,
                    "source directory conflicts with a destination file",
                )
            _preflight_directory_merge(source_entry, destination_entry)
        elif source_entry.is_file():
            if not destination_entry.is_file():
                raise _destination_collision_error(
                    source_entry,
                    destination_entry,
                    "source file conflicts with a destination directory",
                )
            if not _files_are_identical(source_entry, destination_entry):
                raise _destination_collision_error(
                    source_entry,
                    destination_entry,
                    "destination file has different content",
                )
        else:
            raise OSError(
                f"move: cannot safely merge unsupported source path {source_entry}"
            )


def _count_regular_files(path: Path) -> int:
    return sum(
        1 for entry in path.rglob("*") if entry.is_file() and not entry.is_symlink()
    )


def _move_directory_to_path(src: Path, dest: Path) -> None:
    """Move one directory where *dest* has already been confirmed absent."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        raise _destination_collision_error(src, dest, "destination already exists")
    try:
        os_rename(src, dest)
    except OSError:
        shutil.copytree(str(src), str(dest))
        shutil.rmtree(str(src))


def _merge_directory(src: Path, dest: Path, stats: _MoveStats) -> None:
    """Merge a pre-flighted source directory into an existing destination."""
    for source_entry in _sorted_directory_entries(src):
        destination_entry = dest / source_entry.name
        if source_entry.is_dir():
            if destination_entry.exists():
                _merge_directory(source_entry, destination_entry, stats)
            else:
                stats.moved_files += _count_regular_files(source_entry)
                _move_directory_to_path(source_entry, destination_entry)
        else:
            _move_single_file_to_path(source_entry, destination_entry, stats=stats)
    src.rmdir()


def find_season_folder(series_path: Path, season_number: int) -> Path | None:
    """Locate a season subdirectory within *series_path*.

    Scans the actual directory contents for a subdirectory whose name contains
    the season number, matching the patterns Sonarr and other managers produce
    (e.g. ``Season 1``, ``Season 01``, ``Specials``, ``Season 1 (2005)``).

    Season 0 is treated as specials and also matches folders named ``Specials``.

    Args:
        series_path: Root folder of the series on disk.
        season_number: Season number to look for.

    Returns:
        Path to the season folder, or ``None`` if not found.
    """
    if not series_path.is_dir():
        return None

    # Build patterns: "season 1" must be followed by a non-digit (so S1 ≠ S10)
    if season_number == 0:
        patterns = [
            re.compile(r"season\s+0+(?:\D|$)", re.IGNORECASE),
            re.compile(r"specials", re.IGNORECASE),
        ]
    else:
        patterns = [re.compile(rf"season\s+0*{season_number}(?:\D|$)", re.IGNORECASE)]

    for entry in series_path.iterdir():
        if not entry.is_dir():
            continue
        if any(p.search(entry.name) for p in patterns):
            return entry

    return None


def move_season_files(
    series_path: Path,
    destination_root: Path,
    episode_paths: list[str],
    path_mappings: list[dict[str, Any]] | None = None,
    service_type: str | None = None,
    service_config_id: int | None = None,
) -> Path:
    """Move only the episode files belonging to a season out of a flat series directory.

    Used when all episodes share a single directory with no season sub folders.
    Files are identified by the authoritative episode paths recorded from the
    media server sync.  Each listed file and any same-stem siblings (subtitles,
    NFOs, images, etc.) are moved.  The source directory is removed only if it
    is completely empty afterward.

    Args:
        series_path: Flat series directory containing episodes from multiple seasons.
        destination_root: Parent directory to move files into. Files land in a
            folder preserving path-mapping-relative structure.
        episode_paths: Exact episode file paths as stored from the media server sync.
        path_mappings: Path mappings for resolving container/remote paths to local paths.

    Returns:
        The destination directory (``destination_root / series_path.name``).

    Raises:
        OSError: If a file move fails.
    """
    dest_dir = (
        destination_root
        / _destination_relative_parent(
            series_path,
            path_mappings,
            service_type=service_type,
            service_config_id=service_config_id,
            fallback_parent=Path(),
        )
        / series_path.name
    )
    episode_sources: dict[str, Path | None] = {}
    for raw_path in episode_paths:
        local = resolve_path(
            raw_path,
            path_mappings,
            service_type=service_type,
            service_config_id=service_config_id,
        )
        if local and local.is_file():
            episode_sources[local.stem] = local
        else:
            episode_sources.setdefault(Path(raw_path).stem, None)

    # Collect each matched episode and its direct or language-tagged sidecars
    # (for example ``Episode.en.srt``). Other media files remain in place.
    files_to_move: list[Path] = []
    for stem, selected_episode in episode_sources.items():
        for f in list(series_path.iterdir()):
            is_selected_episode = selected_episode is not None and f == selected_episode
            is_unresolved_primary = (
                selected_episode is None and _is_media_file(f) and f.stem == stem
            )
            if f.is_file() and (
                is_selected_episode
                or is_unresolved_primary
                or _is_related_sidecar(f, stem)
            ):
                files_to_move.append(f)

    unique_files_to_move = list(dict.fromkeys(files_to_move))
    _preflight_file_moves(
        [(file, dest_dir / file.name) for file in unique_files_to_move]
    )

    stats = _MoveStats()
    for file in unique_files_to_move:
        try:
            moved_to = _move_single_file(file, dest_dir, stats=stats)
            LOG.info(f"move_season_files: moved {file} -> {moved_to}")
        except OSError as e:
            LOG.warning(f"move_season_files: could not move {file}: {e}")

    if stats.deduplicated_files:
        LOG.info(
            "move_season_files: merged existing destination "
            f"({stats.moved_files} moved, {stats.deduplicated_files} deduplicated)"
        )

    remove_empty_directory(
        series_path,
        log_context="move_season_files",
        log_remaining=True,
    )

    return dest_dir


def remove_empty_directory(
    path: Path,
    *,
    log_context: str,
    log_remaining: bool = False,
) -> bool:
    """Remove *path* only when it exists as an empty directory.

    This intentionally does not recurse into parent directories. It is safe to
    call after move/delete flows that may have already removed the directory.
    """
    try:
        if not path.is_dir():
            return False
        remaining_entries = list(path.iterdir())
        if remaining_entries:
            if log_remaining:
                displayed_entries = ", ".join(
                    entry.name for entry in remaining_entries[:5]
                )
                suffix = "" if len(remaining_entries) <= 5 else ", ..."
                LOG.info(
                    f"{log_context}: retained non-empty source directory {path}; "
                    f"remaining entries: {displayed_entries}{suffix}"
                )
            return False
        path.rmdir()
        LOG.info(f"{log_context}: removed empty source directory {path}")
        return True
    except OSError as exc:
        LOG.debug(f"{log_context}: could not remove source directory {path}: {exc}")
        return False


def move_directory(
    src: Path,
    destination_root: Path,
    path_mappings: Sequence[Mapping[str, Any]] | None = None,
    *,
    service_type: str | None = None,
    service_config_id: int | None = None,
    cleanup_empty_parent: bool = False,
) -> Path:
    """Move directory *src* into *destination_root*.

    Tries an OS-level rename first (fast, same filesystem). Falls back to
    ``shutil.copytree`` + ``shutil.rmtree`` for cross-device/network moves. If
    the destination directory already exists, it is safely merged after a
    complete collision preflight.

    Args:
        src: Source directory to move.
        destination_root: Parent directory to move *src* into.
        path_mappings: Optional path mappings used to preserve folder structure
            beneath the matched local prefix.
        cleanup_empty_parent: Remove *src*'s immediate parent if it is empty
            after the directory move. This is intentionally not recursive.

    Returns:
        The final destination path (``destination_root / src.name``).

    Raises:
        OSError: If the copy or source removal fails.
    """
    relative_parent = _destination_relative_parent(
        src,
        path_mappings,
        service_type=service_type,
        service_config_id=service_config_id,
        fallback_parent=Path(),
    )
    dest = destination_root / relative_parent / src.name
    source_parent = src.parent
    if dest.exists() or dest.is_symlink():
        _preflight_directory_merge(src, dest)
        stats = _MoveStats()
        _merge_directory(src, dest, stats)
        LOG.info(
            "move_directory: merged source folder "
            f"{src} -> {dest} ({stats.moved_files} moved, "
            f"{stats.deduplicated_files} deduplicated)"
        )
    else:
        _move_directory_to_path(src, dest)

    if cleanup_empty_parent:
        remove_empty_directory(
            source_parent,
            log_context="move_directory",
            log_remaining=True,
        )

    return dest


def _is_media_file(path: Path) -> bool:
    return path.suffix.lower() in _MEDIA_FILE_EXTENSIONS


def _is_known_asset_directory(path: Path) -> bool:
    return _compact_name(path.name) in _KNOWN_ASSET_DIRECTORY_NAMES


def _is_primary_media_candidate(path: Path, primary: Path) -> bool:
    if not _is_media_file(path):
        return False
    if path == primary:
        return True
    compact_stem = _compact_name(path.stem)
    if compact_stem in _NON_PRIMARY_MEDIA_STEM_TOKENS:
        return False
    stem_tokens = _name_tokens(path.stem)
    return not (bool(stem_tokens) and stem_tokens[-1] in _NON_PRIMARY_MEDIA_STEM_TOKENS)


def _is_related_sidecar(
    path: Path,
    primary_stem: str,
) -> bool:
    """Return whether *path* belongs to one primary media stem.

    Exact-stem files cover NFOs, posters, and conventional subtitles. Files
    with a dot suffix cover language and subtitle tags such as ``.en`` and
    ``.en.forced``. Other media files are never considered sidecars so a
    sibling movie version or episode stays untouched.
    """
    if _is_media_file(path):
        return False
    return path.stem == primary_stem or path.stem.startswith(f"{primary_stem}.")


def _plan_media_move(src: Path) -> _MediaMovePlan:
    """Return the safe move strategy for a media file's parent folder.

    Moving a whole parent folder is desirable for movie folders because it keeps
    poster images, trailers, extras, and arbitrary sidecars together. It is not
    safe for mixed folders such as a season folder with many episodes or a movie
    folder with multiple primary versions, so those fall back to same-stem moves.
    """
    parent = src.parent
    if not parent.is_dir():
        return _MediaMovePlan(False, "source parent is not a directory")

    try:
        entries = list(parent.iterdir())
    except OSError as exc:
        return _MediaMovePlan(False, f"could not inspect source directory: {exc}")

    unknown_directories = [
        entry
        for entry in entries
        if entry.is_dir() and not _is_known_asset_directory(entry)
    ]
    if unknown_directories:
        return _MediaMovePlan(
            False,
            "contains non-asset subdirectories: "
            + ", ".join(entry.name for entry in unknown_directories[:5]),
        )

    primary_media_files = [
        entry
        for entry in entries
        if entry.is_file() and _is_primary_media_candidate(entry, src)
    ]
    if len(primary_media_files) != 1:
        return _MediaMovePlan(
            False,
            f"contains {len(primary_media_files)} primary media files",
        )

    try:
        is_selected_file = primary_media_files[0].resolve() == src.resolve()
    except OSError:
        is_selected_file = primary_media_files[0] == src
    if not is_selected_file:
        return _MediaMovePlan(False, "selected file is not the primary media file")
    return _MediaMovePlan(True, "item-scoped source directory")


def move_media(
    src: Path,
    destination_root: Path,
    path_mappings: Sequence[Mapping[str, Any]] | None = None,
    *,
    service_type: str | None = None,
    service_config_id: int | None = None,
) -> Path:
    """Move *src* into *destination_root* with related files where safe.

    If the source folder appears scoped to this one media item, the whole folder
    is moved so arbitrary sidecars, posters, trailers, and extras are preserved.
    Mixed folders fall back to moving the primary file plus direct and
    language-tagged sidecars.
    The source directory is removed afterward if it becomes empty.

    Args:
        src: Absolute path to the primary media file.
        destination_root: Root directory to move all files into.
        path_mappings: Optional path mappings used to preserve folder structure
            beneath the matched local prefix.

    Returns:
        The final destination ``Path`` of the primary file.

    Raises:
        OSError: If the primary file move fails.
    """
    parent = src.parent
    move_plan = _plan_media_move(src)
    if move_plan.move_parent_directory:
        moved_parent = move_directory(
            parent,
            destination_root,
            path_mappings,
            service_type=service_type,
            service_config_id=service_config_id,
        )
        dest = moved_parent / src.name
        LOG.info(
            "move_media: strategy=directory "
            f"reason={move_plan.reason}; moved {parent} -> {moved_parent}"
        )
        return dest

    LOG.info(
        "move_media: strategy=file-and-sidecars "
        f"reason={move_plan.reason}; moving {src}"
    )

    relative_parent = _destination_relative_parent(
        src,
        path_mappings,
        service_type=service_type,
        service_config_id=service_config_id,
    )
    dest_dir = destination_root / relative_parent

    # Preflight every selected file before moving the primary so an existing
    # different-content sidecar cannot leave this operation partially moved.
    files_to_move = [src]
    stem = src.stem
    if parent.is_dir():
        files_to_move.extend(
            sibling
            for sibling in _sorted_directory_entries(parent)
            if (
                sibling.is_file()
                and sibling != src
                and _is_related_sidecar(sibling, stem)
            )
        )
    _preflight_file_moves([(file, dest_dir / file.name) for file in files_to_move])

    stats = _MoveStats()
    # Move the primary file first after the complete preflight succeeds.
    dest = _move_single_file(src, dest_dir, stats=stats)
    LOG.info(f"move_media: moved {src} -> {dest}")

    # Move direct and language-tagged sidecars. Other media files are excluded
    # so a shared season folder or alternate movie version remains untouched.
    for sibling in files_to_move[1:]:
        try:
            sibling_dest = _move_single_file(sibling, dest_dir, stats=stats)
            LOG.info(f"move_media: moved sibling {sibling} -> {sibling_dest}")
        except OSError as e:
            LOG.warning(f"move_media: could not move sibling {sibling}: {e}")

    if stats.deduplicated_files:
        LOG.info(
            "move_media: merged existing destination "
            f"({stats.moved_files} moved, {stats.deduplicated_files} deduplicated)"
        )

    # remove source directory if now empty
    remove_empty_directory(
        parent,
        log_context="move_media",
        log_remaining=True,
    )

    return dest
