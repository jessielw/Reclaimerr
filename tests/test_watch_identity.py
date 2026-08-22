import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import ServiceConfig, WatchUserAlias
from backend.enums import Service
from backend.services import watch_identity
from backend.services.plex import PLEX_OWNER_ACCOUNT_ID, PlexService
from backend.services.watch_identity import (
    expand_watch_keys,
    load_watch_user_alias_index,
    refresh_watch_user_aliases,
)

PLEX_USERS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer>
  <User id="12345" title="Black Widow" username="natasha" email="nat@example.com"/>
  <User id="67890" title="Hawkeye" username="clint"/>
</MediaContainer>
"""


def test_plex_user_aliases_include_every_reported_name() -> None:
    aliases = PlexService._parse_plex_user_aliases_xml(PLEX_USERS_XML)

    assert aliases["12345"] == {
        "12345",
        "Black Widow",
        "natasha",
        "nat@example.com",
    }
    assert aliases["67890"] == {"67890", "Hawkeye", "clint"}


def test_plex_user_aliases_tolerate_unparsable_payloads() -> None:
    assert PlexService._parse_plex_user_aliases_xml("not xml") == {}
    assert PlexService._parse_plex_user_aliases_xml("") == {}


def test_expand_watch_keys_is_scoped_to_one_observed_service() -> None:
    aliases = frozenset({"black widow", "natasha"})
    index = {Service.PLEX: {"black widow": aliases, "natasha": aliases}}

    assert expand_watch_keys({"black widow"}, index, Service.PLEX) == {
        "black widow",
        "natasha",
    }
    # The same person on a different server is a different account.
    assert expand_watch_keys({"black widow"}, index, Service.JELLYFIN) == {
        "black widow"
    }


def test_expand_watch_keys_passes_unknown_keys_through() -> None:
    assert expand_watch_keys({"stranger"}, {}, Service.PLEX) == {"stranger"}


def _run(coro):
    return asyncio.run(coro)


async def _build_db():
    tmp_root = Path("tests/.tmp")
    tmp_root.mkdir(parents=True, exist_ok=True)
    db_path = tmp_root / f"test_watch_identity_{uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, sessionmaker, db_path


def test_tautulli_directory_registers_every_alias_under_plex() -> None:
    async def scenario() -> None:
        engine, sessionmaker, db_path = await _build_db()
        try:
            async with sessionmaker() as db:
                db.add(
                    ServiceConfig(
                        service_type=Service.TAUTULLI,
                        base_url="http://tautulli",
                        api_key="key",
                        name="Tautulli",
                        enabled=True,
                    )
                )
                await db.commit()

            client = AsyncMock()
            client.get_users.return_value = [
                {
                    "user_id": 12345,
                    "username": "natasha",
                    "friendly_name": "Black Widow",
                    "email": "nat@example.com",
                }
            ]
            with (
                patch.object(watch_identity, "async_db", sessionmaker),
                patch.object(watch_identity, "TautulliClient", return_value=client),
                patch.object(watch_identity, "_config_api_key", return_value="key"),
            ):
                ok, error = await refresh_watch_user_aliases()

            assert ok, error
            async with sessionmaker() as db:
                rows = (await db.execute(select(WatchUserAlias))).scalars().all()
                # Tautulli only ever reports Plex accounts.
                assert {row.observed_service for row in rows} == {Service.PLEX}
                assert {row.alias_normalized for row in rows} == {
                    "12345",
                    "natasha",
                    "black widow",
                    "nat@example.com",
                }

                index = await load_watch_user_alias_index(db)

            # Any one of the names now reaches all the others.
            assert expand_watch_keys({"black widow"}, index, Service.PLEX) == {
                "12345",
                "natasha",
                "black widow",
                "nat@example.com",
            }
        finally:
            await engine.dispose()
            if db_path.exists():
                db_path.unlink()

    _run(scenario())


def test_refresh_keeps_previous_rows_when_a_provider_fails() -> None:
    async def scenario() -> None:
        engine, sessionmaker, db_path = await _build_db()
        try:
            async with sessionmaker() as db:
                config = ServiceConfig(
                    service_type=Service.TAUTULLI,
                    base_url="http://tautulli",
                    api_key="key",
                    name="Tautulli",
                    enabled=True,
                )
                db.add(config)
                await db.flush()
                db.add(
                    WatchUserAlias(
                        source_service=Service.TAUTULLI,
                        source_service_config_id=config.id,
                        observed_service=Service.PLEX,
                        provider_user_id="12345",
                        alias="natasha",
                        alias_normalized="natasha",
                    )
                )
                await db.commit()

            client = AsyncMock()
            client.get_users.side_effect = RuntimeError("tautulli offline")
            with (
                patch.object(watch_identity, "async_db", sessionmaker),
                patch.object(watch_identity, "TautulliClient", return_value=client),
                patch.object(watch_identity, "_config_api_key", return_value="key"),
            ):
                ok, error = await refresh_watch_user_aliases()

            assert not ok
            assert error is not None and "tautulli offline" in error
            async with sessionmaker() as db:
                rows = (await db.execute(select(WatchUserAlias))).scalars().all()
                # Identities survive a transient outage instead of vanishing
                # mid-scan and quietly breaking requester matching.
                assert [row.alias_normalized for row in rows] == ["natasha"]

            # Provider clients swallow transport errors and return nothing, so
            # an empty directory must be treated the same way.
            client.get_users.side_effect = None
            client.get_users.return_value = []
            with (
                patch.object(watch_identity, "async_db", sessionmaker),
                patch.object(watch_identity, "TautulliClient", return_value=client),
                patch.object(watch_identity, "_config_api_key", return_value="key"),
            ):
                ok, _error = await refresh_watch_user_aliases()

            assert ok
            async with sessionmaker() as db:
                rows = (await db.execute(select(WatchUserAlias))).scalars().all()
                assert [row.alias_normalized for row in rows] == ["natasha"]
        finally:
            await engine.dispose()
            if db_path.exists():
                db_path.unlink()

    _run(scenario())


def test_ambiguous_aliases_do_not_bridge_two_accounts() -> None:
    """A shared display name must not credit one account with another's plays.

    This index feeds deletion rules, so an alias that names more than one
    account proves nothing and is left out. Exact string matching still applies
    to it, exactly as before the registry existed.
    """

    async def scenario() -> None:
        engine, sessionmaker, db_path = await _build_db()
        try:
            async with sessionmaker() as db:
                config = ServiceConfig(
                    service_type=Service.PLEX,
                    base_url="http://plex",
                    api_key="key",
                    name="Plex",
                    enabled=True,
                )
                db.add(config)
                await db.flush()
                # The provider id is always registered as an alias too.
                for provider_user_id, names in (
                    ("1", ("alice", "Home", "a@example.com", "1")),
                    ("2", ("bob", "Home", "b@example.com", "2")),
                ):
                    for name in names:
                        db.add(
                            WatchUserAlias(
                                source_service=Service.PLEX,
                                source_service_config_id=config.id,
                                observed_service=Service.PLEX,
                                provider_user_id=provider_user_id,
                                alias=name,
                                alias_normalized=name.lower(),
                            )
                        )
                await db.commit()

                index = await load_watch_user_alias_index(db)

            # "home" names both accounts, so it bridges neither.
            assert "home" not in index[Service.PLEX]
            assert expand_watch_keys({"home"}, index, Service.PLEX) == {"home"}
            # Unambiguous names still bridge, and never leak the shared one.
            assert expand_watch_keys({"alice"}, index, Service.PLEX) == {
                "alice",
                "a@example.com",
                "1",
            }
        finally:
            await engine.dispose()
            if db_path.exists():
                db_path.unlink()

    _run(scenario())


def test_plex_owner_account_id_is_the_history_account_id() -> None:
    # Plex attributes owner plays to account 1, which is not in plex.tv/api/users.
    assert PLEX_OWNER_ACCOUNT_ID == "1"
