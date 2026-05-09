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
