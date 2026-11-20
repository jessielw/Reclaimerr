from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import User
from backend.enums import Service
from backend.models.services.seerr import SeerrUser
from backend.services import media_user_import as imports
from backend.services import seerr_emails
from backend.services.media_auth import MediaAuthProvider


def seerr_users(*users):
    async def get_users(self, **_):
        return [SimpleNamespace(service_config_id=1, user=user) for user in users]

    return get_users


def seerr_user(id, username, email, jellyfin_user_id=None, jellyfin_username=None):
    return SeerrUser(
        id=id,
        username=username,
        display_name=username,
        email=email,
        jellyfin_user_id=jellyfin_user_id,
        jellyfin_username=jellyfin_username,
    )


def use_seerr(monkeypatch, *users):
    monkeypatch.setattr(
        type(seerr_emails.seerr_snapshot_cache), "get_users", seerr_users(*users)
    )


def test_directory_matches_by_media_id_then_by_name(monkeypatch):
    use_seerr(
        monkeypatch,
        seerr_user(1, "Alex", "Alex@Example.com", jellyfin_user_id="ab-cd"),
        seerr_user(2, "sam", "sam@example.com"),
    )
    directory = asyncio.run(seerr_emails.load_seerr_email_directory())

    assert directory.lookup(media_user_id="ABCD", username="nobody") == (
        "Alex@example.com"
    )
    assert directory.lookup(media_user_id=None, username="Sam") == "sam@example.com"
    assert directory.lookup(media_user_id="x", username="unknown") is None


def test_directory_skips_invalid_and_ambiguous_addresses(monkeypatch):
    use_seerr(
        monkeypatch,
        seerr_user(1, "home", "home"),
        seerr_user(2, "twin", "one@example.com"),
        seerr_user(3, "twin", "two@example.com"),
    )
    directory = asyncio.run(seerr_emails.load_seerr_email_directory())

    assert directory.lookup(media_user_id=None, username="home") is None
    assert directory.lookup(media_user_id=None, username="twin") is None


def test_directory_is_empty_when_seerr_fails(monkeypatch):
    async def boom(self, **_):
        raise RuntimeError("down")

    monkeypatch.setattr(type(seerr_emails.seerr_snapshot_cache), "get_users", boom)
    directory = asyncio.run(seerr_emails.load_seerr_email_directory())

    assert directory.by_media_id == {} and directory.by_username == {}


def test_import_fills_only_missing_emails(monkeypatch):
    use_seerr(
        monkeypatch,
        seerr_user(1, "new_viewer", "seerr@example.com"),
        seerr_user(2, "has_email", "other@example.com"),
    )
    provider = MediaAuthProvider(
        1, Service.JELLYFIN, "main", "https://media.example.com", "credentials"
    )

    def src(id, name, email=None):
        return imports._source(
            provider, {"Id": id, "Name": name, "Email": email, "Policy": {}}
        )

    filled = asyncio.run(
        imports._with_seerr_emails(
            [src("a", "new_viewer"), src("b", "has_email", "mine@example.com")]
        )
    )

    assert filled[0].identity.email == "seerr@example.com"
    assert filled[1].identity.email == "mine@example.com"
    # accounts keep the media server username instead of the email's local part
    assert filled[0].email_from_seerr is True
    assert filled[1].email_from_seerr is False


def test_fill_missing_emails_never_overwrites_or_duplicates(monkeypatch):
    use_seerr(
        monkeypatch,
        seerr_user(1, "blank", "blank@example.com"),
        seerr_user(2, "keeps", "seerr-keeps@example.com"),
        seerr_user(3, "clash", "taken@example.com"),
    )

    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as db:
                db.add_all(
                    [
                        User(username="blank", password_hash="x"),
                        User(username="keeps", password_hash="x", email="mine@x.com"),
                        User(username="clash", password_hash="x"),
                        User(
                            username="other",
                            password_hash="x",
                            email="taken@example.com",
                        ),
                        User(username="unknown", password_hash="x"),
                    ]
                )
                await db.commit()
                result = await seerr_emails.fill_missing_user_emails(db)
                emails = {
                    u.username: u.email
                    for u in (await db.execute(User.__table__.select())).all()
                    for u in [SimpleNamespace(**u._mapping)]
                }
                return result, emails
        finally:
            await engine.dispose()

    result, emails = asyncio.run(run())

    assert result.updated == ["blank"]
    assert result.skipped_conflict == ["clash"]
    assert result.skipped_no_match == ["unknown"]
    assert emails["blank"] == "blank@example.com"
    assert emails["keeps"] == "mine@x.com"
    assert emails["clash"] is None


def test_seerr_email_does_not_rename_the_account(monkeypatch):
    from backend.models.media_user_import import MediaUserImportRequest

    use_seerr(monkeypatch, seerr_user(1, "Sudhakar", "sudhakar86@example.com"))
    provider = MediaAuthProvider(
        1, Service.JELLYFIN, "main", "https://media.example.com", "credentials"
    )
    own = imports._source(
        provider, {"Id": "b", "Name": "Plain", "Email": "mailname@example.com", "Policy": {}}
    )
    sources = asyncio.run(
        imports._with_seerr_emails(
            [
                imports._source(provider, {"Id": "a", "Name": "Sudhakar", "Policy": {}}),
                own,
            ]
        )
    )

    rows = imports._rows(
        sources, [], [], MediaUserImportRequest(service_config_id=1), None, None
    )

    assert rows[0].email == "sudhakar86@example.com"
    assert rows[0].local_username == "Sudhakar"
    # an email the media server itself reports keeps its existing behaviour
    assert rows[1].local_username == "mailname"
