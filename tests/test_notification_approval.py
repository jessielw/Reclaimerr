"""End to end: a non-admin's custom destination stays blocked until approved."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import backend.api.routes.settings.notifications as routes
from backend.api.utils.exception_handlers import register_exception_handlers
from backend.core.auth import get_current_user
from backend.database import get_db
from backend.database.models import AdminNotice, Base, NotificationSetting, User
from backend.enums import UserRole
from backend.services.notification_destinations import url_fingerprint

CUSTOM = "json://example.com/hook"
INTERNAL = "json://127.0.0.1:8989/api/v3/system/status"
BASE = "/api/settings/notifications"


def _run_flow(tmp_path, monkeypatch, flow) -> None:
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with factory() as db:
            admin = User(
                username="admin", password_hash="h", role=UserRole.ADMIN, permissions=[]
            )
            member = User(
                username="member",
                password_hash="h",
                role=UserRole.USER,
                permissions=[],
                allowed_pages=None,
            )
            db.add_all([admin, member])
            await db.commit()
            await db.refresh(admin)
            await db.refresh(member)

        acting: dict[str, User] = {"user": member}
        sent: list[tuple[str, bool]] = []

        async def fake_test(url: str, **kwargs: object):
            sent.append((url, bool(kwargs.get("follow_redirects", True))))
            return True, None

        monkeypatch.setattr(routes, "test_notification_url", fake_test)

        app = FastAPI()
        register_exception_handlers(app)
        app.state.limiter = routes.limiter
        routes.limiter.reset()
        app.include_router(routes.router, prefix="/api/settings")

        async def session():
            async with factory() as db:
                yield db

        app.dependency_overrides[get_db] = session
        app.dependency_overrides[get_current_user] = lambda: acting["user"]

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await flow(client, acting, admin, member, factory, sent)
        await engine.dispose()

    asyncio.run(run())


def _body(url: str, **extra: object) -> dict:
    return {"channel": "apprise", "url": url, "name": "mine", **extra}


def test_custom_destination_is_pending_until_an_admin_approves_it(
    tmp_path, monkeypatch
) -> None:
    async def flow(client, acting, admin, member, factory, sent) -> None:
        # saved as pending, never usable, and the admin is told
        created = await client.post(BASE, json=_body(CUSTOM))
        assert created.status_code == 200, created.text
        item = created.json()["data"]
        assert item["approval"] == "pending"
        nid = item["id"]
        async with factory() as db:
            notices = (await db.execute(select(AdminNotice))).scalars().all()
        assert [n.kind for n in notices] == ["notification_destination_pending"]

        blocked = await client.post(
            f"{BASE}/test", json={"channel": "apprise", "url": CUSTOM}
        )
        assert blocked.status_code == 403
        assert sent == []

        # a non-admin cannot approve their own destination or see the review list
        assert (
            await client.post(
                f"{BASE}/{nid}/approval", json={"fingerprint": url_fingerprint(CUSTOM)}
            )
        ).status_code == 403
        assert (await client.get(f"{BASE}/restricted-destinations")).status_code == 403

        # the admin reviews it and approves exactly what they saw
        acting["user"] = admin
        listed = (await client.get(f"{BASE}/restricted-destinations")).json()
        assert [(d["id"], d["state"], d["username"]) for d in listed] == [
            (nid, "pending", "member")
        ]
        assert listed[0]["fingerprint"] == url_fingerprint(CUSTOM)
        approved = await client.post(
            f"{BASE}/{nid}/approval", json={"fingerprint": listed[0]["fingerprint"]}
        )
        assert approved.status_code == 200, approved.text

        # now the owner can test it, with redirects off
        acting["user"] = member
        assert (await client.get(BASE)).json()[0]["approval"] == "approved"
        ok = await client.post(
            f"{BASE}/test", json={"channel": "apprise", "url": CUSTOM}
        )
        assert ok.status_code == 200, ok.text
        assert sent == [(CUSTOM, False)]

        # editing the URL silently revokes the approval
        edited = await client.post(BASE, json=_body(INTERNAL, id=nid))
        assert edited.status_code == 200, edited.text
        assert edited.json()["data"]["approval"] == "pending"
        again = await client.post(
            f"{BASE}/test", json={"channel": "apprise", "url": INTERNAL}
        )
        assert again.status_code == 403
        assert len(sent) == 1

    _run_flow(tmp_path, monkeypatch, flow)


def test_approval_is_refused_if_the_url_changed_after_review(
    tmp_path, monkeypatch
) -> None:
    async def flow(client, acting, admin, member, factory, sent) -> None:
        nid = (await client.post(BASE, json=_body(CUSTOM))).json()["data"]["id"]
        reviewed = url_fingerprint(CUSTOM)
        # owner swaps the URL while the admin has the old one on screen
        await client.post(BASE, json=_body(INTERNAL, id=nid))

        acting["user"] = admin
        stale = await client.post(
            f"{BASE}/{nid}/approval", json={"fingerprint": reviewed}
        )
        assert stale.status_code == 409
        async with factory() as db:
            row = await db.get(NotificationSetting, nid)
        assert row is not None and row.approved_url_hash is None

    _run_flow(tmp_path, monkeypatch, flow)


def test_revoking_returns_a_destination_to_pending(tmp_path, monkeypatch) -> None:
    async def flow(client, acting, admin, member, factory, sent) -> None:
        nid = (await client.post(BASE, json=_body(CUSTOM))).json()["data"]["id"]
        acting["user"] = admin
        await client.post(
            f"{BASE}/{nid}/approval", json={"fingerprint": url_fingerprint(CUSTOM)}
        )
        assert (await client.delete(f"{BASE}/{nid}/approval")).status_code == 200

        acting["user"] = member
        assert (await client.get(BASE)).json()[0]["approval"] == "pending"
        blocked = await client.post(
            f"{BASE}/test", json={"channel": "apprise", "url": CUSTOM}
        )
        assert blocked.status_code == 403

    _run_flow(tmp_path, monkeypatch, flow)


def test_built_in_services_and_invalid_urls(tmp_path, monkeypatch) -> None:
    async def flow(client, acting, admin, member, factory, sent) -> None:
        fine = await client.post(BASE, json=_body("discord://123/abc"))
        assert fine.json()["data"]["approval"] == "not_required"
        async with factory() as db:
            assert (await db.execute(select(AdminNotice))).scalars().all() == []

        # smuggling a second URL in, or an unparseable one, is refused outright
        for bad in ("discord://123/abc json://127.0.0.1/x", "bogus://x"):
            assert (await client.post(BASE, json=_body(bad))).status_code == 400

        # admins are not restricted, and never see their own rows as pending
        acting["user"] = admin
        own = await client.post(BASE, json=_body(INTERNAL))
        assert own.json()["data"]["approval"] == "not_required"
        listed = await client.get(f"{BASE}/restricted-destinations")
        assert listed.json() == []

    _run_flow(tmp_path, monkeypatch, flow)
