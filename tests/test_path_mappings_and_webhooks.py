from __future__ import annotations

import asyncio
from pathlib import Path

from backend.core.utils.filesystem import resolve_path
from backend.enums import MediaType, Service
from backend.models.post_action_webhooks import PostActionWebhookEvent
from backend.services.post_action_webhooks import send_post_action_webhook


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


def test_send_post_action_webhook_renders_urlencoded_path(monkeypatch) -> None:
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
        "backend.services.post_action_webhooks.niquests.AsyncSession",
        lambda: FakeSession(),
    )

    result = asyncio.run(
        send_post_action_webhook(
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
            PostActionWebhookEvent(
                action="deleted",
                media_type=MediaType.MOVIE,
                title="Movie",
                path="/media/Movie Name/file.mkv",
                service_type=Service.PLEX,
            ),
        )
    )

    assert result == {"success": True, "status_code": 204, "error": None}
    assert captured["url"].endswith("path=%2Fmedia%2FMovie%20Name%2Ffile.mkv")


def test_send_post_action_webhook_includes_sanitized_failure_body(monkeypatch) -> None:
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
        "backend.services.post_action_webhooks.niquests.AsyncSession",
        lambda: FakeSession(),
    )

    result = asyncio.run(
        send_post_action_webhook(
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
            PostActionWebhookEvent(
                action="deleted",
                media_type=MediaType.MOVIE,
                title="Movie",
                path="/media/Movie Name/file.mkv",
                service_type=Service.PLEX,
            ),
        )
    )

    assert result["success"] is False
    assert result["status_code"] == 500
    assert "HTTP 500" in (result["error"] or "")
    assert "<redacted>" in (result["error"] or "")
    assert "keep-me-out" not in (result["error"] or "")
