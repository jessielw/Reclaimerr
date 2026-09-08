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
    delete_collection_poster,
    save_collection_poster_from_bytes,
)
from backend.models.settings import GeneralSettingsResponse
from backend.tasks import cleanup as cleanup_tasks
from backend.utils.helpers import LeavingSoonPosters

MIGRATION = importlib.import_module(
    "backend.alembic.versions.a1c5f7e2d940_add_leaving_soon_collection_posters"
)


def _image_bytes(
    *, size: tuple[int, int] = (400, 600), fmt: str = "PNG", mode: str = "RGB"
) -> bytes:
    buffer = BytesIO()
    Image.new(mode, size, (120, 40, 200)).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture
def poster_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "collection-posters"
    monkeypatch.setattr(core_settings, "collection_posters_dir", directory)
    return directory


class TestPosterStorage:
    def test_uploads_are_re_encoded_to_jpeg(self, poster_dir: Path) -> None:
        filename = save_collection_poster_from_bytes(_image_bytes())

        assert filename.endswith(".jpg")
        with Image.open(poster_dir / filename) as saved:
            assert saved.format == "JPEG"

    def test_oversized_posters_are_bounded_keeping_aspect(
        self, poster_dir: Path
    ) -> None:
        filename = save_collection_poster_from_bytes(_image_bytes(size=(2000, 3000)))

        with Image.open(poster_dir / filename) as saved:
            assert saved.size == (1000, 1500)

    def test_poster_sized_uploads_are_left_alone(self, poster_dir: Path) -> None:
        filename = save_collection_poster_from_bytes(_image_bytes(size=(600, 900)))

        with Image.open(poster_dir / filename) as saved:
            assert saved.size == (600, 900)

    def test_transparency_is_flattened_rather_than_corrupted(
        self, poster_dir: Path
    ) -> None:
        # JPEG has no alpha channel; dropping it naively turns a transparent
        # PNG into noise rather than a usable poster
        filename = save_collection_poster_from_bytes(
            _image_bytes(mode="RGBA", fmt="PNG")
        )

        with Image.open(poster_dir / filename) as saved:
            assert saved.mode == "RGB"

    def test_replacing_a_poster_removes_the_old_file(self, poster_dir: Path) -> None:
        first = save_collection_poster_from_bytes(_image_bytes())
        second = save_collection_poster_from_bytes(_image_bytes(), first)

        assert not (poster_dir / first).exists()
        assert (poster_dir / second).exists()

    def test_a_non_image_upload_leaves_nothing_behind(self, poster_dir: Path) -> None:
        poster_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(Exception):
            save_collection_poster_from_bytes(b"this is not an image")

        assert list(poster_dir.glob("*")) == []

    def test_deleting_an_already_missing_poster_is_not_an_error(
        self, poster_dir: Path
    ) -> None:
        poster_dir.mkdir(parents=True, exist_ok=True)

        delete_collection_poster("nothing-here.jpg")


class TestPosterReads:
    def test_unset_and_blank_columns_read_as_no_poster(self, poster_dir: Path) -> None:
        for value in (None, "", "   "):
            assert cleanup_tasks.read_leaving_soon_poster(value) is None

    def test_a_missing_file_degrades_instead_of_raising(self, poster_dir: Path) -> None:
        # artwork deleted out from under Reclaimerr must not fail the sync that
        # was about to push it
        assert cleanup_tasks.read_leaving_soon_poster("gone.jpg") is None

    def test_a_stored_poster_is_read_back(self, poster_dir: Path) -> None:
        filename = save_collection_poster_from_bytes(_image_bytes())

        assert (
            cleanup_tasks.read_leaving_soon_poster(filename)
            == (poster_dir / filename).read_bytes()
        )

    def test_a_path_outside_the_poster_directory_is_refused(
        self, poster_dir: Path
    ) -> None:
        # the column holds a bare filename; anything with separators in it did
        # not come from the upload endpoint
        assert cleanup_tasks.read_leaving_soon_poster("../../secrets.env") is None

    def test_both_posters_load_together(self, poster_dir: Path) -> None:
        movie = save_collection_poster_from_bytes(_image_bytes())

        class _SettingsRow:
            leaving_soon_movie_poster_path = movie
            leaving_soon_series_poster_path = None

        posters = cleanup_tasks.load_leaving_soon_posters(_SettingsRow())  # type: ignore[arg-type]

        assert posters.movies is not None
        assert posters.series is None
        assert bool(posters) is True

    def test_no_posters_is_falsey(self) -> None:
        assert not LeavingSoonPosters()


class TestSettingsModel:
    def test_posters_default_to_unset(self) -> None:
        settings = GeneralSettingsResponse()

        assert settings.leaving_soon_movie_poster_path is None
        assert settings.leaving_soon_series_poster_path is None

    def test_the_settings_put_never_writes_the_poster_columns(self) -> None:
        # the upload and delete endpoints own these; a client PUTting a body it
        # loaded before an upload must not be able to clear a poster
        route_source = Path("backend/api/routes/settings/general.py").read_text(
            encoding="utf-8"
        )
        update_body = route_source.split("async def update_general_settings", 1)[1]
        update_body = update_body.split("@router.", 1)[0]

        assert "settings.leaving_soon_movie_poster_path =" not in update_body
        assert "settings.leaving_soon_series_poster_path =" not in update_body


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


class TestPlexPosterUpload(unittest.IsolatedAsyncioTestCase):
    async def test_the_image_is_posted_raw_to_the_posters_endpoint(self) -> None:
        from backend.services.plex import PlexService

        class FakePlex:
            plex_url = "http://plex"

            def __init__(self) -> None:
                self.session = _FakeSession()

        fake = FakePlex()
        await PlexService._upload_collection_poster(
            fake,  # type: ignore[arg-type]
            collection_id="col-1",
            poster=b"jpeg-bytes",
        )

        self.assertEqual(
            fake.session.posts,
            [
                {
                    "url": "http://plex/library/metadata/col-1/posters",
                    "data": b"jpeg-bytes",
                    "headers": {"Content-Type": "image/jpeg"},
                    "timeout": 60,
                }
            ],
        )

    async def test_a_rejected_upload_does_not_raise(self) -> None:
        # the sync must keep going: a failure here would stop the caller
        # recording the titles it just synced, and rename cleanup would stall
        from backend.services.plex import PlexService

        class FakePlex:
            plex_url = "http://plex"

            def __init__(self) -> None:
                self.session = _FakeSession(error=RuntimeError("nope"))

        await PlexService._upload_collection_poster(
            FakePlex(),  # type: ignore[arg-type]
            collection_id="col-1",
            poster=b"jpeg-bytes",
        )

    async def test_no_poster_skips_the_lookup_that_resolves_the_collection(
        self,
    ) -> None:
        from backend.services.plex import PlexService

        class FakePlex:
            async def _find_collection_ids_by_title(self, **kwargs: Any) -> list[str]:
                raise AssertionError("should not look the collection up")

        resolved = await PlexService._apply_collection_poster(
            FakePlex(),  # type: ignore[arg-type]
            collection_id=None,
            section_id="1",
            collection_title="Leaving Soon [Movies]",
            poster=None,
        )

        self.assertIsNone(resolved)


class TestEmbyPosterUpload(unittest.IsolatedAsyncioTestCase):
    async def test_the_image_is_posted_base64_encoded(self) -> None:
        # Emby and Jellyfin both take the image base64 encoded in the body,
        # not as multipart
        from base64 import b64encode

        from backend.enums import Service
        from backend.services.emby_base import EmbyServiceBase

        class FakeEmby:
            service_url = "http://emby"
            service_type = Service.EMBY

            def __init__(self) -> None:
                self.session = _FakeSession()

        fake = FakeEmby()
        await EmbyServiceBase._upload_collection_poster(
            fake,  # type: ignore[arg-type]
            collection_id="boxset-1",
            poster=b"jpeg-bytes",
        )

        self.assertEqual(
            fake.session.posts,
            [
                {
                    "url": "http://emby/Items/boxset-1/Images/Primary",
                    "data": b64encode(b"jpeg-bytes"),
                    "headers": {"Content-Type": "image/jpeg"},
                    "timeout": 60,
                }
            ],
        )

    async def test_a_rejected_upload_does_not_raise(self) -> None:
        from backend.enums import Service
        from backend.services.emby_base import EmbyServiceBase

        class FakeEmby:
            service_url = "http://emby"
            service_type = Service.JELLYFIN

            def __init__(self) -> None:
                self.session = _FakeSession(error=RuntimeError("nope"))

        await EmbyServiceBase._upload_collection_poster(
            FakeEmby(),  # type: ignore[arg-type]
            collection_id="boxset-1",
            poster=b"jpeg-bytes",
        )


class TestPosterEndpoints(unittest.IsolatedAsyncioTestCase):
    """Round trip through the real routes, with only auth and the DB faked."""

    def _client(self, poster_dir: Path) -> tuple[Any, Any]:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from backend.api.routes.settings import general as general_routes
        from backend.core.auth import require_admin
        from backend.database import get_db

        class _SettingsRow:
            leaving_soon_movie_poster_path: str | None = None
            leaving_soon_series_poster_path: str | None = None
            updated_at: Any = None
            updated_by_user_id: int | None = None

        class _Db:
            def __init__(self, row: Any) -> None:
                self.row = row
                self.commits = 0

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
                self.commits += 1

        row = _SettingsRow()
        db = _Db(row)
        app = FastAPI()
        app.include_router(general_routes.router, prefix="/api/settings")
        app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
            id=1, username="admin"
        )
        app.dependency_overrides[get_db] = lambda: db
        return TestClient(app), row

    async def test_upload_then_delete_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            poster_dir = Path(tmp) / "collection-posters"
            push = AsyncMock()
            with patch.object(
                core_settings, "collection_posters_dir", poster_dir
            ):
                client, row = self._client(poster_dir)
                with patch(
                    "backend.api.routes.settings.general.push_leaving_soon_posters",
                    push,
                ):
                    response = client.post(
                        "/api/settings/general/leaving-soon-poster/movies",
                        files={
                            "poster": ("art.png", _image_bytes(), "image/png")
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    filename = response.json()["path"]
                    self.assertTrue(filename.endswith(".jpg"))
                    self.assertTrue((poster_dir / filename).exists())
                    self.assertEqual(row.leaving_soon_movie_poster_path, filename)
                    # the poster reaches the servers now, not at the next scan
                    push.assert_awaited_once()

                    response = client.delete(
                        "/api/settings/general/leaving-soon-poster/movies"
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertIsNone(response.json()["path"])
                    self.assertFalse((poster_dir / filename).exists())
                    self.assertIsNone(row.leaving_soon_movie_poster_path)

    async def test_a_wrong_content_type_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            poster_dir = Path(tmp) / "collection-posters"
            with patch.object(core_settings, "collection_posters_dir", poster_dir):
                client, row = self._client(poster_dir)

                response = client.post(
                    "/api/settings/general/leaving-soon-poster/series",
                    files={"poster": ("art.gif", b"GIF89a", "image/gif")},
                )

                self.assertEqual(response.status_code, 400)
                self.assertIsNone(row.leaving_soon_series_poster_path)

    async def test_an_unknown_kind_never_reaches_the_handler(self) -> None:
        with TemporaryDirectory() as tmp:
            poster_dir = Path(tmp) / "collection-posters"
            with patch.object(core_settings, "collection_posters_dir", poster_dir):
                client, _row = self._client(poster_dir)

                response = client.post(
                    "/api/settings/general/leaving-soon-poster/everything",
                    files={"poster": ("art.png", _image_bytes(), "image/png")},
                )

                self.assertEqual(response.status_code, 422)

    async def test_deleting_when_nothing_is_set_is_a_no_op(self) -> None:
        with TemporaryDirectory() as tmp:
            poster_dir = Path(tmp) / "collection-posters"
            with patch.object(core_settings, "collection_posters_dir", poster_dir):
                client, _row = self._client(poster_dir)

                response = client.delete(
                    "/api/settings/general/leaving-soon-poster/movies"
                )

                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.json()["path"])


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
        import unittest.mock as mock

        with mock.patch.object(MIGRATION, "op", operations):
            MIGRATION.upgrade()

            columns = {
                column["name"]
                for column in inspect(connection).get_columns("general_settings")
            }
            assert "leaving_soon_movie_poster_path" in columns
            assert "leaving_soon_series_poster_path" in columns

            # an install that never uploaded a poster keeps whatever artwork its
            # media server generates
            row = connection.execute(
                text(
                    "SELECT leaving_soon_movie_poster_path, "
                    "leaving_soon_series_poster_path FROM general_settings"
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
            assert "leaving_soon_movie_poster_path" not in columns
            assert "leaving_soon_series_poster_path" not in columns
