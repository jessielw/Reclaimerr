from __future__ import annotations

import importlib
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from PIL import Image
from sqlalchemy import create_engine, inspect, text

from backend.core.settings import settings as core_settings
from backend.core.utils.image_handling import (
    COLLECTION_THUMB_BOUND,
    save_collection_poster_from_bytes,
)
from backend.models.settings import GeneralSettingsResponse
from backend.tasks import cleanup as cleanup_tasks
from backend.utils.helpers import LeavingSoonThumbs

MIGRATION = importlib.import_module(
    "backend.alembic.versions.f7b2e9d4c160_add_leaving_soon_collection_thumbs"
)


def _image_bytes(*, size: tuple[int, int] = (640, 360)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, (120, 40, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def poster_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "collection-posters"
    monkeypatch.setattr(core_settings, "collection_posters_dir", directory)
    return directory


class TestThumbStorage:
    def test_oversized_thumbs_are_bounded_to_a_landscape_size(
        self, poster_dir: Path
    ) -> None:
        filename = save_collection_poster_from_bytes(
            _image_bytes(size=(3840, 2160)), bound=COLLECTION_THUMB_BOUND
        )

        with Image.open(poster_dir / filename) as saved:
            assert saved.size == (1920, 1080)

    def test_a_full_hd_thumb_is_not_shrunk_to_the_poster_bound(
        self, poster_dir: Path
    ) -> None:
        # the poster bound caps width at 1000; a thumb must not inherit it
        filename = save_collection_poster_from_bytes(
            _image_bytes(size=(1920, 1080)), bound=COLLECTION_THUMB_BOUND
        )

        with Image.open(poster_dir / filename) as saved:
            assert saved.size == (1920, 1080)


class TestThumbReads:
    def test_both_thumbs_load_together(self, poster_dir: Path) -> None:
        series = save_collection_poster_from_bytes(_image_bytes())

        class _SettingsRow:
            leaving_soon_movie_thumb_path = None
            leaving_soon_series_thumb_path = series

        thumbs = cleanup_tasks.load_leaving_soon_thumbs(_SettingsRow())  # type: ignore[arg-type]

        assert thumbs.movies is None
        assert thumbs.series == (poster_dir / series).read_bytes()
        assert bool(thumbs) is True

    def test_no_thumbs_is_falsey(self) -> None:
        assert not LeavingSoonThumbs()


class TestSettingsModel:
    def test_thumbs_default_to_unset(self) -> None:
        settings = GeneralSettingsResponse()

        assert settings.leaving_soon_movie_thumb_path is None
        assert settings.leaving_soon_series_thumb_path is None

    def test_the_settings_put_never_writes_the_thumb_columns(self) -> None:
        route_source = Path("backend/api/routes/settings/general.py").read_text(
            encoding="utf-8"
        )
        update_body = route_source.split("async def update_general_settings", 1)[1]
        update_body = update_body.split("@router.", 1)[0]

        assert "settings.leaving_soon_movie_thumb_path =" not in update_body
        assert "settings.leaving_soon_series_thumb_path =" not in update_body


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None


class _FakeSession:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.posts: list[dict[str, Any]] = []
        self._error = error

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.posts.append({"url": url, **kwargs})
        if self._error is not None:
            raise self._error
        return _FakeResponse()


def _fake_emby(*, error: Exception | None = None) -> Any:
    from backend.enums import Service
    from backend.services.emby_base import EmbyServiceBase

    class FakeEmby(EmbyServiceBase):
        service_url = "http://emby"
        service_type = Service.JELLYFIN

        def __init__(self) -> None:
            self.session = _FakeSession(error=error)  # type: ignore[assignment]

    return FakeEmby()


class TestEmbyThumbUpload(unittest.IsolatedAsyncioTestCase):
    async def test_the_thumb_is_posted_to_the_thumb_image_type(self) -> None:
        from base64 import b64encode

        fake = _fake_emby()
        await fake._upload_collection_thumb(collection_id="boxset-1", thumb=b"jpeg")

        self.assertEqual(
            fake.session.posts,
            [
                {
                    "url": "http://emby/Items/boxset-1/Images/Thumb",
                    "data": b64encode(b"jpeg"),
                    "headers": {"Content-Type": "image/jpeg"},
                    "timeout": 60,
                }
            ],
        )

    async def test_a_rejected_upload_does_not_raise(self) -> None:
        fake = _fake_emby(error=RuntimeError("nope"))

        await fake._upload_collection_thumb(collection_id="boxset-1", thumb=b"jpeg")

    async def test_existing_collection_sync_pushes_poster_and_thumb(self) -> None:
        fake = _fake_emby()
        fake._find_collection_ids_by_title = AsyncMock(return_value=["boxset-1"])
        fake._get_collection_item_ids = AsyncMock(return_value={"item-1"})

        await fake._sync_leaving_soon_collection(
            collection_title="Leaving Soon [Movies]",
            expected_item_ids={"item-1"},
            include_item_types="Movie",
            poster=b"poster",
            thumb=b"thumb",
        )

        self.assertEqual(
            [post["url"] for post in fake.session.posts],
            [
                "http://emby/Items/boxset-1/Images/Primary",
                "http://emby/Items/boxset-1/Images/Thumb",
            ],
        )

    async def test_a_new_collection_is_resolved_for_a_thumb_alone(self) -> None:
        # without a poster the id lookup used to be skipped; a thumb needs it too
        fake = _fake_emby()
        fake._find_collection_ids_by_title = AsyncMock(return_value=[])
        fake._filter_existing_collection_item_ids = AsyncMock(
            return_value={"item-1"}
        )
        fake._create_collection = AsyncMock(return_value="boxset-new")

        await fake._sync_leaving_soon_collection(
            collection_title="Leaving Soon [Series]",
            expected_item_ids={"item-1"},
            include_item_types="Series",
            thumb=b"thumb",
        )

        self.assertTrue(fake._create_collection.await_args.kwargs["resolve_id"])
        self.assertEqual(
            [post["url"] for post in fake.session.posts],
            ["http://emby/Items/boxset-new/Images/Thumb"],
        )

    async def test_apply_thumbs_pushes_only_configured_halves(self) -> None:
        fake = _fake_emby()
        fake._find_collection_ids_by_title = AsyncMock(return_value=["boxset-2"])

        await fake.apply_leaving_soon_collection_thumbs(
            movie_title="Leaving Soon [Movies]",
            series_title="Leaving Soon [Series]",
            movie_thumb=None,
            series_thumb=b"thumb",
        )

        fake._find_collection_ids_by_title.assert_awaited_once_with(
            "Leaving Soon [Series]"
        )
        self.assertEqual(
            [post["url"] for post in fake.session.posts],
            ["http://emby/Items/boxset-2/Images/Thumb"],
        )


class TestPlexIgnoresThumbs(unittest.IsolatedAsyncioTestCase):
    async def test_plex_has_no_thumb_push(self) -> None:
        # push_leaving_soon_thumbs skips servers without this method
        from backend.services.plex import PlexService

        self.assertFalse(hasattr(PlexService, "apply_leaving_soon_collection_thumbs"))


class TestThumbEndpoints(unittest.IsolatedAsyncioTestCase):
    """Round trip through the real routes, with only auth and the DB faked."""

    def _client(self) -> tuple[Any, Any]:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from backend.api.routes.settings import general as general_routes
        from backend.core.auth import require_admin
        from backend.database import get_db

        class _SettingsRow:
            leaving_soon_movie_poster_path: str | None = None
            leaving_soon_series_poster_path: str | None = None
            leaving_soon_movie_thumb_path: str | None = None
            leaving_soon_series_thumb_path: str | None = None
            updated_at: Any = None
            updated_by_user_id: int | None = None

        class _Db:
            def __init__(self, row: Any) -> None:
                self.row = row

            async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
                row = self.row

                class _Result:
                    def scalars(self) -> Any:
                        class _Scalars:
                            def first(self) -> Any:
                                return row

                        return _Scalars()

                return _Result()

            def add(self, *_args: Any) -> None:
                return None

            async def commit(self) -> None:
                return None

        row = _SettingsRow()
        app = FastAPI()
        app.include_router(general_routes.router, prefix="/api/settings")
        app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
            id=1, username="admin"
        )
        app.dependency_overrides[get_db] = lambda: _Db(row)
        return TestClient(app), row

    async def test_upload_then_delete_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            poster_dir = Path(tmp) / "collection-posters"
            push_thumbs = AsyncMock()
            push_posters = AsyncMock()
            with (
                patch.object(core_settings, "collection_posters_dir", poster_dir),
                patch(
                    "backend.api.routes.settings.general.push_leaving_soon_thumbs",
                    push_thumbs,
                ),
                patch(
                    "backend.api.routes.settings.general.push_leaving_soon_posters",
                    push_posters,
                ),
            ):
                client, row = self._client()
                response = client.post(
                    "/api/settings/general/leaving-soon-thumb/series",
                    files={
                        "thumb": (
                            "art.png",
                            _image_bytes(size=(2560, 1440)),
                            "image/png",
                        )
                    },
                )
                self.assertEqual(response.status_code, 200)
                filename = response.json()["path"]
                self.assertEqual(row.leaving_soon_series_thumb_path, filename)
                self.assertIsNone(row.leaving_soon_series_poster_path)
                with Image.open(poster_dir / filename) as saved:
                    self.assertEqual(saved.size, (1920, 1080))
                push_thumbs.assert_awaited_once()
                push_posters.assert_not_awaited()

                response = client.delete(
                    "/api/settings/general/leaving-soon-thumb/series"
                )
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.json()["path"])
                self.assertFalse((poster_dir / filename).exists())
                self.assertIsNone(row.leaving_soon_series_thumb_path)

    async def test_a_wrong_content_type_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            poster_dir = Path(tmp) / "collection-posters"
            with patch.object(core_settings, "collection_posters_dir", poster_dir):
                client, row = self._client()

                response = client.post(
                    "/api/settings/general/leaving-soon-thumb/movies",
                    files={"thumb": ("art.gif", b"GIF89a", "image/gif")},
                )

                self.assertEqual(response.status_code, 400)
                self.assertIsNone(row.leaving_soon_movie_thumb_path)


def test_migration_adds_both_columns_without_backfilling(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'settings.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE general_settings ("
                "id INTEGER PRIMARY KEY, "
                "leaving_soon_enabled BOOLEAN NOT NULL DEFAULT 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO general_settings (id, leaving_soon_enabled) VALUES (1, 1)"
            )
        )
        operations = Operations(MigrationContext.configure(connection))

        with patch.object(MIGRATION, "op", operations):
            MIGRATION.upgrade()

            columns = {
                column["name"]
                for column in inspect(connection).get_columns("general_settings")
            }
            assert "leaving_soon_movie_thumb_path" in columns
            assert "leaving_soon_series_thumb_path" in columns

            row = connection.execute(
                text(
                    "SELECT leaving_soon_movie_thumb_path, "
                    "leaving_soon_series_thumb_path FROM general_settings"
                )
            ).one()
            assert tuple(row) == (None, None)

            # re-running is a no-op rather than a duplicate-column error
            MIGRATION.upgrade()

            MIGRATION.downgrade()
            columns = {
                column["name"]
                for column in inspect(connection).get_columns("general_settings")
            }
            assert "leaving_soon_movie_thumb_path" not in columns
            assert "leaving_soon_series_thumb_path" not in columns
