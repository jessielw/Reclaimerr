"""Issue #458: secondary media servers sharing the main server's paths get a
path scan after cleanup, never a delete."""

from __future__ import annotations

import asyncio

import pytest

from backend.tasks import cleanup


class FakeMediaServer:
    def __init__(self, *, fail_scan: bool = False) -> None:
        self.fail_scan = fail_scan
        self.deleted_items: list[str] = []
        self.scanned_paths: list[str] = []
        self.warn_unmatched: list[bool] = []

    async def delete_item(self, item_id: str) -> None:
        self.deleted_items.append(item_id)

    async def scan_item_path(
        self, item_path: str, *, warn_unmatched: bool = True
    ) -> bool:
        if self.fail_scan:
            raise RuntimeError("server unavailable")
        self.scanned_paths.append(item_path)
        self.warn_unmatched.append(warn_unmatched)
        return True


def _patch_servers(
    monkeypatch, main: FakeMediaServer | None, *secondaries: FakeMediaServer
) -> None:
    manager = cleanup.service_manager
    monkeypatch.setattr(manager, "_main_media_server", main)
    monkeypatch.setattr(manager, "_plex", main)
    monkeypatch.setattr(manager, "_plex_clients", {1: main} if main else {})
    monkeypatch.setattr(manager, "_jellyfin", None)
    monkeypatch.setattr(manager, "_emby", None)
    monkeypatch.setattr(
        manager,
        "_jellyfin_clients",
        {index: server for index, server in enumerate(secondaries, start=2)},
    )
    monkeypatch.setattr(manager, "_emby_clients", {})


def _reconcile(**kwargs) -> None:
    kwargs.setdefault("item_id", None)
    kwargs.setdefault("allow_delete_item", False)
    kwargs.setdefault("context", "test")
    asyncio.run(cleanup._reconcile_media_server_after_delete(**kwargs))


def test_secondary_servers_exclude_main_and_duplicates(monkeypatch) -> None:
    main, secondary = FakeMediaServer(), FakeMediaServer()
    _patch_servers(monkeypatch, main, secondary)
    monkeypatch.setattr(cleanup.service_manager, "_jellyfin", secondary)

    assert cleanup.service_manager.secondary_media_servers() == [secondary]


def test_path_scan_reaches_every_server(monkeypatch) -> None:
    main, secondary = FakeMediaServer(), FakeMediaServer()
    _patch_servers(monkeypatch, main, secondary)
    season = "/data/Media/tv/Example Show/Season 01"

    _reconcile(paths=[season, None, season])

    assert main.scanned_paths == [season]
    assert main.warn_unmatched == [True]
    assert secondary.scanned_paths == [season]
    assert secondary.warn_unmatched == [False]


def test_secondary_only_scans_when_main_deletes_item(monkeypatch) -> None:
    main, secondary = FakeMediaServer(), FakeMediaServer()
    _patch_servers(monkeypatch, main, secondary)
    season = "/data/Media/tv/Example Show/Season 01"

    _reconcile(item_id="season-key", paths=[season], allow_delete_item=True)

    assert main.deleted_items == ["season-key"]
    assert main.scanned_paths == []
    assert secondary.deleted_items == []
    assert secondary.scanned_paths == [season]


def test_unavailable_secondary_does_not_fail_cleanup(monkeypatch) -> None:
    main = FakeMediaServer()
    broken, healthy = FakeMediaServer(fail_scan=True), FakeMediaServer()
    _patch_servers(monkeypatch, main, broken, healthy)

    _reconcile(paths=["/data/Show/Season 01"])

    assert main.scanned_paths == ["/data/Show/Season 01"]
    assert healthy.scanned_paths == ["/data/Show/Season 01"]


def test_required_removal_raises_before_secondary_scan(monkeypatch) -> None:
    main, secondary = FakeMediaServer(), FakeMediaServer()
    _patch_servers(monkeypatch, main, secondary)

    with pytest.raises(ValueError, match="could not be confirmed"):
        _reconcile(paths=["/data/Show"], require_removal=True)

    assert main.scanned_paths == []
    assert secondary.scanned_paths == []
