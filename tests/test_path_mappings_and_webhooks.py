from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.utils.filesystem import (
    move_directory,
    move_media,
    move_season_files,
    remove_empty_directory,
    resolve_path,
)
from backend.enums import MediaType, Service
from backend.services.webhook_transport import send_webhook_payload


def test_resolve_path_prefers_scoped_mappings(tmp_path: Path) -> None:
    global_root = tmp_path / "global"
    type_root = tmp_path / "plex"
    config_root = tmp_path / "plex-config"
    for root in (global_root, type_root, config_root):
        (root / "Movie").mkdir(parents=True)
        (root / "Movie" / "file.mkv").write_text("x")

    mappings = [
        {"source_prefix": "/media", "local_prefix": str(global_root)},
        {
            "source_prefix": "/media",
            "local_prefix": str(type_root),
            "service_type": "plex",
        },
        {
            "source_prefix": "/media",
            "local_prefix": str(config_root),
            "service_type": "plex",
            "service_config_id": 10,
        },
    ]

    assert resolve_path("/media/Movie/file.mkv", mappings) == (
        global_root / "Movie" / "file.mkv"
    )
    assert resolve_path("/media/Movie/file.mkv", mappings, service_type="plex") == (
        type_root / "Movie" / "file.mkv"
    )
    assert resolve_path(
        "/media/Movie/file.mkv",
        mappings,
        service_type="plex",
        service_config_id=10,
    ) == (config_root / "Movie" / "file.mkv")


def test_resolve_path_normalizes_slashes_and_requires_prefix_boundary(
    tmp_path: Path,
) -> None:
    media_root = tmp_path / "media"
    (media_root / "Movie").mkdir(parents=True)
    (media_root / "Movie" / "file.mkv").write_text("x")

    mappings = [
        {
            "source_prefix": r"\remote\media",
            "local_prefix": str(media_root),
        }
    ]

    assert resolve_path(r"\remote\media\Movie\file.mkv", mappings) == (
        media_root / "Movie" / "file.mkv"
    )
    assert resolve_path(r"\remote\media-other\Movie\file.mkv", mappings) is None


def test_resolve_path_skips_non_matching_scoped_mapping(tmp_path: Path) -> None:
    scoped_root = tmp_path / "scoped"
    global_root = tmp_path / "global"
    (global_root / "Movie").mkdir(parents=True)
    (global_root / "Movie" / "file.mkv").write_text("x")

    mappings = [
        {
            "source_prefix": "/media",
            "local_prefix": str(scoped_root),
            "service_type": "plex",
            "service_config_id": 10,
        },
        {"source_prefix": "/media", "local_prefix": str(global_root)},
    ]

    assert resolve_path(
        "/media/Movie/file.mkv",
        mappings,
        service_type="plex",
        service_config_id=20,
    ) == (global_root / "Movie" / "file.mkv")


def test_resolve_path_supports_root_source_mapping(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "media" / "Movie").mkdir(parents=True)
    (root / "media" / "Movie" / "file.mkv").write_text("x")

    assert resolve_path(
        "/media/Movie/file.mkv",
        [{"source_prefix": "/", "local_prefix": str(root)}],
    ) == (root / "media" / "Movie" / "file.mkv")


def test_move_media_preserves_mapping_relative_folder_structure(
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "library"
    movie_dir = local_root / "movies" / "Movie One (2024)"
    movie_dir.mkdir(parents=True)
    media_file = movie_dir / "Movie One (2024).mkv"
    subtitle_file = movie_dir / "Movie One (2024).srt"
    media_file.write_bytes(b"movie")
    subtitle_file.write_bytes(b"subtitle")
    destination_root = tmp_path / "reclaimed"

    moved_to = move_media(
        media_file,
        destination_root,
        [{"source_prefix": "/remote", "local_prefix": str(local_root)}],
    )

    expected_dir = destination_root / "movies" / "Movie One (2024)"
    assert moved_to == expected_dir / "Movie One (2024).mkv"
    assert moved_to.read_bytes() == b"movie"
    assert (expected_dir / "Movie One (2024).srt").read_bytes() == b"subtitle"
    assert not media_file.exists()
    assert not subtitle_file.exists()
    assert not movie_dir.exists()


def test_move_media_without_mapping_preserves_immediate_media_folder(
    tmp_path: Path,
) -> None:
    movie_dir = tmp_path / "library" / "Movie Two (2025)"
    movie_dir.mkdir(parents=True)
    media_file = movie_dir / "Movie Two (2025).mkv"
    media_file.write_bytes(b"movie")
    destination_root = tmp_path / "reclaimed"

    moved_to = move_media(media_file, destination_root)

    assert moved_to == destination_root / "Movie Two (2025)" / "Movie Two (2025).mkv"
    assert moved_to.read_bytes() == b"movie"
    assert not media_file.exists()
    assert not movie_dir.exists()


def test_remove_empty_directory_removes_only_empty_directory(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty-movie"
    empty_dir.mkdir()
    non_empty_dir = tmp_path / "non-empty-movie"
    non_empty_dir.mkdir()
    (non_empty_dir / "leftover.nfo").write_text("metadata")
    missing_dir = tmp_path / "missing-movie"

    assert remove_empty_directory(empty_dir, log_context="test") is True
    assert not empty_dir.exists()
    assert remove_empty_directory(non_empty_dir, log_context="test") is False
    assert non_empty_dir.exists()
    assert remove_empty_directory(missing_dir, log_context="test") is False


def test_move_media_does_not_overwrite_existing_destination(tmp_path: Path) -> None:
    local_root = tmp_path / "library"
    movie_dir = local_root / "movies" / "Movie Three (2026)"
    movie_dir.mkdir(parents=True)
    media_file = movie_dir / "Movie Three (2026).mkv"
    media_file.write_bytes(b"source")
    destination_file = (
        tmp_path
        / "reclaimed"
        / "movies"
        / "Movie Three (2026)"
        / "Movie Three (2026).mkv"
    )
    destination_file.parent.mkdir(parents=True)
    destination_file.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        move_media(
            media_file,
            tmp_path / "reclaimed",
            [{"source_prefix": "/remote", "local_prefix": str(local_root)}],
        )

    assert media_file.read_bytes() == b"source"
    assert destination_file.read_bytes() == b"existing"


def test_move_media_deduplicates_identical_destination_file(tmp_path: Path) -> None:
    local_root = tmp_path / "library"
    movie_dir = local_root / "movies" / "Movie Duplicate (2026)"
    movie_dir.mkdir(parents=True)
    media_file = movie_dir / "Movie Duplicate (2026).mkv"
    media_file.write_bytes(b"same movie")
    destination_file = (
        tmp_path
        / "reclaimed"
        / "movies"
        / "Movie Duplicate (2026)"
        / "Movie Duplicate (2026).mkv"
    )
    destination_file.parent.mkdir(parents=True)
    destination_file.write_bytes(b"same movie")

    moved_to = move_media(
        media_file,
        tmp_path / "reclaimed",
        [{"source_prefix": "/remote", "local_prefix": str(local_root)}],
    )

    assert moved_to == destination_file
    assert destination_file.read_bytes() == b"same movie"
    assert not media_file.exists()
    assert not movie_dir.exists()


def test_move_media_merges_existing_destination_and_removes_source_folder(
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "data"
    movie_dir = local_root / "media" / "movies" / "The Mule (2018)"
    movie_dir.mkdir(parents=True)
    media_file = movie_dir / "The Mule (2018).mp4"
    subtitle_file = movie_dir / "The Mule (2018).eng.srt"
    extra_file = movie_dir / "other_file.txt"
    media_file.write_bytes(b"movie")
    subtitle_file.write_bytes(b"subtitle")
    extra_file.write_bytes(b"")

    destination_root = tmp_path / "archive"
    destination_dir = destination_root / "media" / "movies" / "The Mule (2018)"
    destination_dir.mkdir(parents=True)
    existing_file = destination_dir / "im_already_here.txt"
    existing_file.write_bytes(b"")

    moved_to = move_media(
        media_file,
        destination_root,
        [{"source_prefix": "/mnt/data", "local_prefix": str(local_root)}],
    )

    assert moved_to == destination_dir / media_file.name
    assert moved_to.read_bytes() == b"movie"
    assert (destination_dir / subtitle_file.name).read_bytes() == b"subtitle"
    assert (destination_dir / extra_file.name).read_bytes() == b""
    assert existing_file.read_bytes() == b""
    assert not movie_dir.exists()


def test_move_media_preflights_sidecar_conflicts_before_moving_primary(
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "library"
    movie_dir = local_root / "movies" / "Movie Conflict (2026)"
    movie_dir.mkdir(parents=True)
    media_file = movie_dir / "Movie Conflict (2026).mkv"
    subtitle_file = movie_dir / "Movie Conflict (2026).en.srt"
    media_file.write_bytes(b"movie")
    subtitle_file.write_bytes(b"source subtitle")
    destination_file = (
        tmp_path
        / "reclaimed"
        / "movies"
        / "Movie Conflict (2026)"
        / "Movie Conflict (2026).en.srt"
    )
    destination_file.parent.mkdir(parents=True)
    destination_file.write_bytes(b"different subtitle")

    with pytest.raises(FileExistsError, match="different content"):
        move_media(
            media_file,
            tmp_path / "reclaimed",
            [{"source_prefix": "/remote", "local_prefix": str(local_root)}],
        )

    assert media_file.read_bytes() == b"movie"
    assert subtitle_file.read_bytes() == b"source subtitle"
    assert destination_file.read_bytes() == b"different subtitle"


def test_move_media_moves_item_scoped_folder_assets(tmp_path: Path) -> None:
    local_root = tmp_path / "library"
    movie_dir = local_root / "movies" / "Movie With Assets (2026)"
    extras_dir = movie_dir / "Trailers"
    extras_dir.mkdir(parents=True)
    media_file = movie_dir / "Movie With Assets (2026).mkv"
    subtitle_file = movie_dir / "Movie With Assets (2026).en.srt"
    poster_file = movie_dir / "poster.jpg"
    trailer_file = extras_dir / "trailer.mkv"
    media_file.write_bytes(b"movie")
    subtitle_file.write_bytes(b"subtitle")
    poster_file.write_bytes(b"poster")
    trailer_file.write_bytes(b"trailer")

    moved_to = move_media(
        media_file,
        tmp_path / "reclaimed",
        [{"source_prefix": "/remote", "local_prefix": str(local_root)}],
    )

    expected_dir = tmp_path / "reclaimed" / "movies" / "Movie With Assets (2026)"
    assert moved_to == expected_dir / "Movie With Assets (2026).mkv"
    assert moved_to.read_bytes() == b"movie"
    assert (
        expected_dir / "Movie With Assets (2026).en.srt"
    ).read_bytes() == b"subtitle"
    assert (expected_dir / "poster.jpg").read_bytes() == b"poster"
    assert (expected_dir / "Trailers" / "trailer.mkv").read_bytes() == b"trailer"
    assert not movie_dir.exists()


def test_move_media_keeps_multi_version_folder_conservative(tmp_path: Path) -> None:
    local_root = tmp_path / "library"
    movie_dir = local_root / "movies" / "Movie Multi Version (2026)"
    movie_dir.mkdir(parents=True)
    selected_file = movie_dir / "Movie Multi Version (2026) - 1080p.mkv"
    selected_subtitle = movie_dir / "Movie Multi Version (2026) - 1080p.srt"
    selected_language_subtitle = movie_dir / "Movie Multi Version (2026) - 1080p.en.srt"
    other_version = movie_dir / "Movie Multi Version (2026) - 2160p.mkv"
    poster_file = movie_dir / "poster.jpg"
    selected_file.write_bytes(b"1080p")
    selected_subtitle.write_bytes(b"subtitle")
    selected_language_subtitle.write_bytes(b"language subtitle")
    other_version.write_bytes(b"2160p")
    poster_file.write_bytes(b"poster")

    moved_to = move_media(
        selected_file,
        tmp_path / "reclaimed",
        [{"source_prefix": "/remote", "local_prefix": str(local_root)}],
    )

    expected_dir = tmp_path / "reclaimed" / "movies" / "Movie Multi Version (2026)"
    assert moved_to == expected_dir / "Movie Multi Version (2026) - 1080p.mkv"
    assert moved_to.read_bytes() == b"1080p"
    assert (
        expected_dir / "Movie Multi Version (2026) - 1080p.srt"
    ).read_bytes() == b"subtitle"
    assert (
        expected_dir / "Movie Multi Version (2026) - 1080p.en.srt"
    ).read_bytes() == b"language subtitle"
    assert other_version.read_bytes() == b"2160p"
    assert poster_file.read_bytes() == b"poster"
    assert not selected_file.exists()
    assert not selected_subtitle.exists()
    assert not selected_language_subtitle.exists()


def test_move_media_does_not_treat_title_words_as_trailer_assets(
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "library"
    movie_dir = local_root / "movies" / "Trailer Park Movie (2026)"
    movie_dir.mkdir(parents=True)
    selected_file = movie_dir / "Trailer Park Movie (2026) - 1080p.mkv"
    other_version = movie_dir / "Trailer Park Movie (2026) - 2160p.mkv"
    selected_file.write_bytes(b"1080p")
    other_version.write_bytes(b"2160p")

    moved_to = move_media(
        selected_file,
        tmp_path / "reclaimed",
        [{"source_prefix": "/remote", "local_prefix": str(local_root)}],
    )

    expected_dir = tmp_path / "reclaimed" / "movies" / "Trailer Park Movie (2026)"
    assert moved_to == expected_dir / "Trailer Park Movie (2026) - 1080p.mkv"
    assert other_version.read_bytes() == b"2160p"


def test_move_media_keeps_episode_folder_conservative(tmp_path: Path) -> None:
    local_root = tmp_path / "library"
    season_dir = local_root / "tv" / "Show One" / "Season 01"
    season_dir.mkdir(parents=True)
    selected_episode = season_dir / "Show One - S01E01.mkv"
    selected_subtitle = season_dir / "Show One - S01E01.srt"
    selected_language_subtitle = season_dir / "Show One - S01E01.en.srt"
    other_episode = season_dir / "Show One - S01E02.mkv"
    selected_episode.write_bytes(b"e1")
    selected_subtitle.write_bytes(b"sub")
    selected_language_subtitle.write_bytes(b"language sub")
    other_episode.write_bytes(b"e2")

    moved_to = move_media(
        selected_episode,
        tmp_path / "reclaimed",
        [{"source_prefix": "/remote", "local_prefix": str(local_root)}],
    )

    expected_dir = tmp_path / "reclaimed" / "tv" / "Show One" / "Season 01"
    assert moved_to == expected_dir / "Show One - S01E01.mkv"
    assert moved_to.read_bytes() == b"e1"
    assert (expected_dir / "Show One - S01E01.srt").read_bytes() == b"sub"
    assert (expected_dir / "Show One - S01E01.en.srt").read_bytes() == b"language sub"
    assert other_episode.read_bytes() == b"e2"
    assert season_dir.exists()


def test_move_directory_preserves_mapping_relative_folder_structure(
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "library"
    series_dir = local_root / "tv" / "Show One"
    season_dir = series_dir / "Season 01"
    season_dir.mkdir(parents=True)
    episode_file = season_dir / "Show One - S01E01.mkv"
    episode_file.write_bytes(b"episode")

    moved_to = move_directory(
        series_dir,
        tmp_path / "reclaimed",
        [{"source_prefix": "/remote", "local_prefix": str(local_root)}],
    )

    assert moved_to == tmp_path / "reclaimed" / "tv" / "Show One"
    assert (moved_to / "Season 01" / "Show One - S01E01.mkv").read_bytes() == b"episode"
    assert not series_dir.exists()


def test_move_directory_merges_existing_destination_folder(tmp_path: Path) -> None:
    local_root = tmp_path / "library"
    series_dir = local_root / "tv" / "Show Merge"
    season_two = series_dir / "Season 02"
    season_two.mkdir(parents=True)
    (season_two / "Show Merge - S02E01.mkv").write_bytes(b"episode two")
    existing_season = tmp_path / "reclaimed" / "tv" / "Show Merge" / "Season 01"
    existing_season.mkdir(parents=True)
    (existing_season / "Show Merge - S01E01.mkv").write_bytes(b"episode one")

    moved_to = move_directory(
        series_dir,
        tmp_path / "reclaimed",
        [{"source_prefix": "/remote", "local_prefix": str(local_root)}],
    )

    assert moved_to == tmp_path / "reclaimed" / "tv" / "Show Merge"
    assert (
        moved_to / "Season 01" / "Show Merge - S01E01.mkv"
    ).read_bytes() == b"episode one"
    assert (
        moved_to / "Season 02" / "Show Merge - S02E01.mkv"
    ).read_bytes() == b"episode two"
    assert not series_dir.exists()


def test_move_directory_can_remove_empty_source_parent(tmp_path: Path) -> None:
    local_root = tmp_path / "library"
    series_dir = local_root / "tv" / "Show One"
    season_dir = series_dir / "Season 01"
    season_dir.mkdir(parents=True)
    episode_file = season_dir / "Show One - S01E01.mkv"
    episode_file.write_bytes(b"episode")

    moved_to = move_directory(
        season_dir,
        tmp_path / "reclaimed",
        [{"source_prefix": "/remote", "local_prefix": str(local_root)}],
        cleanup_empty_parent=True,
    )

    assert moved_to == tmp_path / "reclaimed" / "tv" / "Show One" / "Season 01"
    assert (moved_to / "Show One - S01E01.mkv").read_bytes() == b"episode"
    assert not season_dir.exists()
    assert not series_dir.exists()


def test_move_directory_leaves_empty_source_parent_by_default(tmp_path: Path) -> None:
    local_root = tmp_path / "library"
    series_dir = local_root / "tv" / "Show Two"
    season_dir = series_dir / "Season 01"
    season_dir.mkdir(parents=True)
    episode_file = season_dir / "Show Two - S01E01.mkv"
    episode_file.write_bytes(b"episode")

    move_directory(
        season_dir,
        tmp_path / "reclaimed",
        [{"source_prefix": "/remote", "local_prefix": str(local_root)}],
    )

    assert not season_dir.exists()
    assert series_dir.exists()


def test_move_season_files_preserves_series_folder_for_flat_series(
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "library"
    series_dir = local_root / "tv" / "Flat Show"
    series_dir.mkdir(parents=True)
    season_one = series_dir / "Flat Show - S01E01.mkv"
    season_one_alternate = series_dir / "Flat Show - S01E01.mp4"
    season_one_sub = series_dir / "Flat Show - S01E01.srt"
    season_one_language_sub = series_dir / "Flat Show - S01E01.en.srt"
    season_two = series_dir / "Flat Show - S02E01.mkv"
    season_one.write_bytes(b"s1")
    season_one_alternate.write_bytes(b"alternate")
    season_one_sub.write_bytes(b"sub")
    season_one_language_sub.write_bytes(b"language sub")
    season_two.write_bytes(b"s2")

    moved_to = move_season_files(
        series_dir,
        tmp_path / "reclaimed",
        episode_paths=["/remote/tv/Flat Show/Flat Show - S01E01.mkv"],
        path_mappings=[{"source_prefix": "/remote", "local_prefix": str(local_root)}],
    )

    assert moved_to == tmp_path / "reclaimed" / "tv" / "Flat Show"
    assert (moved_to / "Flat Show - S01E01.mkv").read_bytes() == b"s1"
    assert (moved_to / "Flat Show - S01E01.srt").read_bytes() == b"sub"
    assert (moved_to / "Flat Show - S01E01.en.srt").read_bytes() == b"language sub"
    assert not season_one.exists()
    assert not season_one_sub.exists()
    assert not season_one_language_sub.exists()
    assert season_one_alternate.read_bytes() == b"alternate"
    assert season_two.read_bytes() == b"s2"


def test_send_webhook_payload_renders_urlencoded_path(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class FakeResponse:
        status_code = 204

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            captured["url"] = url
            return FakeResponse()

    monkeypatch.setattr(
        "backend.services.webhook_transport.niquests.AsyncSession",
        lambda: FakeSession(),
    )

    result = asyncio.run(
        send_webhook_payload(
            {
                "enabled": True,
                "name": "Autopulse",
                "method": "GET",
                "url_template": "http://autopulse:2875/triggers/manual?path={urlencoded_path}",
                "path_mode": "original",
                "actions": ["deleted"],
                "media_types": ["movie"],
                "timeout_seconds": 15,
            },
            {
                "action": "deleted",
                "media_type": "movie",
                "title": "Movie",
                "path": "/media/Movie Name/file.mkv",
                "service_type": "plex",
            },
        )
    )

    assert result == {"success": True, "status_code": 204, "error": None}
    assert captured["url"].endswith("path=%2Fmedia%2FMovie%20Name%2Ffile.mkv")


def test_send_webhook_payload_includes_sanitized_failure_body(monkeypatch) -> None:
    class FakeResponse:
        status_code = 500
        text = '{"error":"boom","token":"keep-me-out"}'

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(
        "backend.services.webhook_transport.niquests.AsyncSession",
        lambda: FakeSession(),
    )

    result = asyncio.run(
        send_webhook_payload(
            {
                "enabled": True,
                "name": "Autopulse",
                "method": "GET",
                "url_template": "http://autopulse:2875/trigger",
                "path_mode": "original",
                "actions": ["deleted"],
                "media_types": ["movie"],
                "timeout_seconds": 15,
            },
            {
                "action": "deleted",
                "media_type": "movie",
                "title": "Movie",
                "path": "/media/Movie Name/file.mkv",
                "service_type": "plex",
            },
        )
    )

    assert result["success"] is False
    assert result["status_code"] == 500
    assert "HTTP 500" in (result["error"] or "")
    assert "<redacted>" in (result["error"] or "")
    assert "keep-me-out" not in (result["error"] or "")
