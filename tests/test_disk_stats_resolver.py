from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.core.rule_engine import DiskStatsResolver
from backend.enums import Service
from backend.tasks import cleanup as cleanup_tasks


def _usage(*, total: int, free: int) -> SimpleNamespace:
    return SimpleNamespace(total=total, used=total - free, free=free)


def test_accessible_media_mount_wins_over_arr_root(tmp_path: Path) -> None:
    media_file = tmp_path / "media" / "movies" / "Movie" / "Movie.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"movie")

    resolver = DiskStatsResolver(
        arr_entries=[
            {
                "path": "/",
                "free_space": 171,
                "total_space": 227,
                "service_type": Service.RADARR.value,
                "service_config_id": 1,
            }
        ]
    )

    with patch(
        "backend.core.rule_engine.shutil.disk_usage",
        return_value=_usage(total=1800, free=317),
    ) as disk_usage:
        result = resolver.resolve(
            str(media_file),
            media_service_type=Service.PLEX.value,
            arr_service_type=Service.RADARR.value,
        )

    assert result == pytest.approx((317, 317 / 1800 * 100))
    disk_usage.assert_called_once_with(media_file)


def test_accessible_mapped_media_mount_wins_over_arr_root(
    tmp_path: Path,
) -> None:
    media_root = tmp_path / "media"
    media_file = media_root / "movies" / "Movie" / "Movie.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"movie")

    resolver = DiskStatsResolver(
        arr_entries=[
            {
                "path": "/",
                "free_space": 171,
                "total_space": 227,
                "service_type": Service.RADARR.value,
                "service_config_id": 1,
            }
        ],
        path_mappings=[
            {
                "source_prefix": "/remote/media",
                "local_prefix": str(media_root),
                "service_type": Service.PLEX.value,
            }
        ],
    )

    with patch(
        "backend.core.rule_engine.shutil.disk_usage",
        return_value=_usage(total=1800, free=317),
    ) as disk_usage:
        result = resolver.resolve(
            "/remote/media/movies/Movie/Movie.mkv",
            media_service_type=Service.PLEX.value,
            arr_service_type=Service.RADARR.value,
        )

    assert result == pytest.approx((317, 317 / 1800 * 100))
    disk_usage.assert_called_once_with(media_file)


def test_remote_media_uses_most_specific_arr_mount() -> None:
    resolver = DiskStatsResolver(
        arr_entries=[
            {
                "path": "/",
                "free_space": 171,
                "total_space": 227,
                "service_type": Service.RADARR.value,
                "service_config_id": 1,
            },
            {
                "path": "/media",
                "free_space": 317,
                "total_space": 1800,
                "service_type": Service.RADARR.value,
                "service_config_id": 1,
            },
        ]
    )

    result = resolver.resolve(
        "/media/movies/Movie/Movie.mkv",
        media_service_type=Service.PLEX.value,
        arr_service_type=Service.RADARR.value,
    )

    assert result == pytest.approx((317, 317 / 1800 * 100))


def test_remote_media_and_arr_paths_match_through_scoped_mappings() -> None:
    resolver = DiskStatsResolver(
        arr_entries=[
            {
                "path": "/radarr-media",
                "free_space": 500,
                "total_space": 2000,
                "service_type": Service.RADARR.value,
                "service_config_id": 7,
            }
        ],
        path_mappings=[
            {
                "source_prefix": "/plex-media",
                "local_prefix": "/canonical-media",
                "service_type": Service.PLEX.value,
            },
            {
                "source_prefix": "/radarr-media",
                "local_prefix": "/canonical-media",
                "service_type": Service.RADARR.value,
                "service_config_id": 7,
            },
        ],
    )

    result = resolver.resolve(
        "/plex-media/movies/Movie/Movie.mkv",
        media_service_type=Service.PLEX.value,
        arr_service_type=Service.RADARR.value,
    )

    assert result == pytest.approx((500, 25.0))


def test_arr_provider_filter_prevents_sonarr_mount_from_serving_movie_rule() -> None:
    resolver = DiskStatsResolver(
        arr_entries=[
            {
                "path": "/media/movies",
                "free_space": 100,
                "total_space": 1000,
                "service_type": Service.SONARR.value,
                "service_config_id": 2,
            },
            {
                "path": "/media",
                "free_space": 400,
                "total_space": 2000,
                "service_type": Service.RADARR.value,
                "service_config_id": 1,
            },
        ]
    )

    result = resolver.resolve(
        "/media/movies/Movie/Movie.mkv",
        media_service_type=Service.PLEX.value,
        arr_service_type=Service.RADARR.value,
    )

    assert result == pytest.approx((400, 20.0))


def test_arr_disk_loader_retains_instance_identity(monkeypatch) -> None:
    class DiskClient:
        def __init__(self, free: int) -> None:
            self.free = free

        async def get_disk_space(self) -> list[dict[str, int | str]]:
            return [
                {
                    "path": "/media",
                    "free_space": self.free,
                    "total_space": 1000,
                }
            ]

    async def run() -> None:
        monkeypatch.setattr(
            cleanup_tasks.service_manager,
            "radarr_clients",
            lambda: {3: DiskClient(300), 4: DiskClient(400)},
        )
        monkeypatch.setattr(
            cleanup_tasks.service_manager,
            "sonarr_clients",
            lambda: {5: DiskClient(500)},
        )

        entries = await cleanup_tasks._load_arr_disk_space()

        assert [
            (entry["service_type"], entry["service_config_id"], entry["free_space"])
            for entry in entries
        ] == [
            (Service.RADARR.value, 3, 300),
            (Service.RADARR.value, 4, 400),
            (Service.SONARR.value, 5, 500),
        ]

    asyncio.run(run())
