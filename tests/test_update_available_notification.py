from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import NotificationSetting
from backend.enums import NotificationType
from backend.services.notifications import _compose_body
from backend.tasks import update_check


class _Recorder:
    """Stand-in for notify_update_available that records every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, int]:
        self.calls.append(kwargs)
        return {"sent": 1, "failed": 0}


@pytest.fixture
async def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Point update_check at an in-memory database and a fake notifier."""
    # an in-memory engine pools one connection, so the schema survives across
    # the separate sessions _persist_result opens on each call
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    recorder = _Recorder()
    monkeypatch.setattr(update_check, "async_db", session_maker)
    monkeypatch.setattr(update_check, "notify_update_available", recorder)
    return recorder


async def _persist(version: str | None, *, update_available: bool) -> None:
    await update_check._persist_result(  # pyright: ignore[reportPrivateUsage]
        latest_version=version,
        latest_release_url="https://example.invalid/releases/latest",
        latest_release_published_at=None,
        update_available=update_available,
        error=None,
    )


@pytest.mark.anyio
async def test_announces_a_new_release_once(recorder: _Recorder) -> None:
    # the check runs hourly, so the same release must not be announced again
    await _persist("9.9.9", update_available=True)
    await _persist("9.9.9", update_available=True)
    await _persist("9.9.9", update_available=True)

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["latest_version"] == "9.9.9"
    assert recorder.calls[0]["release_url"] == (
        "https://example.invalid/releases/latest"
    )


@pytest.mark.anyio
async def test_announces_each_distinct_release(recorder: _Recorder) -> None:
    await _persist("9.9.9", update_available=True)
    await _persist("10.0.0", update_available=True)

    assert [call["latest_version"] for call in recorder.calls] == ["9.9.9", "10.0.0"]


@pytest.mark.anyio
async def test_stays_quiet_when_up_to_date(recorder: _Recorder) -> None:
    await _persist("0.0.1", update_available=False)

    assert recorder.calls == []


@pytest.mark.anyio
async def test_a_failed_check_announces_nothing(recorder: _Recorder) -> None:
    # the failure path persists update_available=False with no version
    await _persist(None, update_available=False)

    assert recorder.calls == []


@pytest.mark.anyio
async def test_a_failed_announcement_does_not_fail_the_task(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    async def _boom(**_kwargs: Any) -> dict[str, int]:
        raise RuntimeError("apprise is down")

    monkeypatch.setattr(update_check, "notify_update_available", _boom)

    # state is already committed by this point, so the task must swallow this
    await _persist("9.9.9", update_available=True)


def test_update_available_is_admin_only() -> None:
    assert NotificationType.UPDATE_AVAILABLE.is_admin_only() is True


def test_body_names_both_versions_and_links_the_release() -> None:
    title, body, _ = _compose_body(
        notification_type=NotificationType.UPDATE_AVAILABLE,
        setting=NotificationSetting(
            user_id=1, enabled=True, url="json://localhost", preferences={}
        ),
        fallback_title="Update Available",
        fallback_message="Reclaimerr 9.9.9 is available.",
        context={
            "latest_version": "9.9.9",
            "current_version": "0.4.7",
            "release_url": "https://example.invalid/releases/9.9.9",
        },
    )

    assert title == "Update Available: v9.9.9"
    assert "0.4.7" in body
    assert "9.9.9" in body
    assert "(https://example.invalid/releases/9.9.9)" in body


def test_body_omits_the_link_when_no_release_url_is_known() -> None:
    _title, body, _ = _compose_body(
        notification_type=NotificationType.UPDATE_AVAILABLE,
        setting=NotificationSetting(
            user_id=1, enabled=True, url="json://localhost", preferences={}
        ),
        fallback_title="Update Available",
        fallback_message="Reclaimerr 9.9.9 is available.",
        context={"latest_version": "9.9.9", "current_version": "0.4.7"},
    )

    assert "Release notes" not in body
