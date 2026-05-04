from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from backend.core.logger import LOG


def resolve_path(
    media_server_path: str | None,
    mappings: list[dict] | None,
) -> Path | None:
    """Resolve a media server reported path to a local filesystem path.

    Iterates the configured path mappings, replaces any matching source prefix
    with the corresponding local prefix, and returns a ``Path`` only if the
    result exists on disk.  If no mapping matches, the path is tried as-is.

    Args:
        media_server_path: Path as reported by the media server (may use
            Docker/container paths like ``/movies/Title/file.mkv``).
        mappings: List of mapping dicts with ``source_prefix`` and
            ``local_prefix`` keys (as stored in the DB).

    Returns:
        An existing ``Path`` object, or ``None`` if the file cannot be located.
    """
    if not media_server_path:
        return None

    for m in mappings or []:
        source = m.get("source_prefix", "")
        local = m.get("local_prefix", "")
        if source and media_server_path.startswith(source):
            mapped = local + media_server_path[len(source) :]
            p = Path(mapped)
            if p.exists():
                return p
            LOG.debug(
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


def _move_single_file(src: Path, destination_root: Path) -> Path:
    """Move a single file into *destination_root*, deleting the source afterward.

    Tries an OS-level rename first (fast, atomic, same filesystem).  Falls back
    to explicit copy + verify + delete so the source is guaranteed to be removed
    even across filesystem/network-share boundaries.

    Raises:
        OSError: If the copy or source deletion fails.
    """
    dest = destination_root / src.name
    # fast path: same filesystem rename
    try:
        os.rename(src, dest)
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
    return dest


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
    path_mappings: list[dict] | None = None,
) -> Path:
    """Move only the episode files belonging to a season out of a flat series directory.

    Used when all episodes share a single directory with no season sub folders.
    Files are identified by the authoritative episode paths recorded from the
    media server sync.  Each listed file and any same-stem siblings (subtitles,
    NFOs, images, etc.) are moved.  The source directory is removed only if it
    is completely empty afterward.

    Args:
        series_path: Flat series directory containing episodes from multiple seasons.
        destination_root: Parent directory to move files into. Files land in
            ``destination_root / series_path.name /``.
        episode_paths: Exact episode file paths as stored from the media server sync.
        path_mappings: Path mappings for resolving container/remote paths to local paths.

    Returns:
        The destination directory (``destination_root / series_path.name``).

    Raises:
        OSError: If a file move fails.
    """
    dest_dir = destination_root / series_path.name
    dest_dir.mkdir(parents=True, exist_ok=True)

    episode_stems: set[str] = set()
    for raw_path in episode_paths:
        local = resolve_path(raw_path, path_mappings)
        if local and local.is_file():
            episode_stems.add(local.stem)
        else:
            episode_stems.add(Path(raw_path).stem)

    # move each matched episode and any same stem siblings (subs, NFOs, images)
    for stem in episode_stems:
        for f in list(series_path.iterdir()):
            if f.is_file() and f.stem == stem:
                try:
                    moved_to = _move_single_file(f, dest_dir)
                    LOG.info(f"move_season_files: moved {f} -> {moved_to}")
                except OSError as e:
                    LOG.warning(f"move_season_files: could not move {f}: {e}")

    # remove the source directory only if completely empty
    try:
        series_path.rmdir()  # raises OSError if anything remains
        LOG.info(f"move_season_files: removed empty series directory {series_path}")
    except OSError:
        pass  # other seasons still present (leave the directory alone)

    return dest_dir


def move_directory(src: Path, destination_root: Path) -> Path:
    """Move directory *src* into *destination_root*.

    Tries an OS-level rename first (fast, same filesystem).  Falls back to
    ``shutil.copytree`` + ``shutil.rmtree`` for cross-device/network moves.

    Args:
        src: Source directory to move.
        destination_root: Parent directory to move *src* into.

    Returns:
        The final destination path (``destination_root / src.name``).

    Raises:
        OSError: If the copy or source removal fails.
    """
    destination_root.mkdir(parents=True, exist_ok=True)
    dest = destination_root / src.name

    try:
        os.rename(src, dest)
        return dest
    except OSError:
        pass  # cross-device (fall through to copy + remove)

    shutil.copytree(str(src), str(dest))
    shutil.rmtree(str(src))

    return dest


def move_media(src: Path, destination_root: Path) -> Path:
    """Move *src* into *destination_root*, along with any same stem siblings.

    Siblings (subtitles, NFOs, cover images, etc.) share the same filename
    stem as the primary media file and are moved to the same destination root.
    The source directory is removed afterward if it becomes empty.

    Args:
        src: Absolute path to the primary media file.
        destination_root: Root directory to move all files into.

    Returns:
        The final destination ``Path`` of the primary file.

    Raises:
        OSError: If the primary file move fails.
    """
    destination_root.mkdir(parents=True, exist_ok=True)

    # move the primary file first (let this raise on failure)
    dest = _move_single_file(src, destination_root)
    LOG.info(f"move_media: moved {src} -> {dest}")

    # move same stem siblings (subtitles, NFOs, images, or whatever we find)
    stem = src.stem
    parent = src.parent
    if parent.is_dir():
        for sibling in list(parent.iterdir()):
            if sibling.is_file() and sibling.stem == stem:
                try:
                    sibling_dest = _move_single_file(sibling, destination_root)
                    LOG.info(f"move_media: moved sibling {sibling} -> {sibling_dest}")
                except OSError as e:
                    LOG.warning(f"move_media: could not move sibling {sibling}: {e}")

    # remove source directory if now empty
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            LOG.info(f"move_media: removed empty source directory {parent}")
    except OSError as e:
        LOG.debug(f"move_media: could not remove source directory {parent}: {e}")

    return dest
