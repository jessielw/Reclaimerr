import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, create_autospec, patch
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import ServiceConfig, WatchUserAlias
from backend.enums import Service
from backend.models.media import WatchUserDirectoryEntry
from backend.models.services.emby_base import EmbyUserBase
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



def _probe_write_from_another_connection(db_path: Path) -> str | None:
    """Try to take the write lock from an unrelated connection.

    Returns the SQLite failure, or None when the write went through.
    """
    connection = sqlite3.connect(db_path, timeout=0.25, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=250")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE service_configs SET name = name")
        connection.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        return str(exc)
    finally:
        connection.close()
    return None


def test_alias_refresh_holds_no_write_lock_while_polling_providers() -> None:
    """No write transaction may span a provider's network call.

    Interleaving the two put the first config's DELETE - and with it SQLite's
    single write lock - at the head of a transaction that then waited on every
    remaining provider's HTTP calls. Unrelated writers failed with "database is
    locked" well past their busy timeout while that ran.
    """

    async def scenario() -> None:
        engine, sessionmaker, db_path = await _build_db()
        probe: dict[str, str | None] = {}
        try:
            async with sessionmaker() as db:
                for name in ("Tautulli A", "Tautulli B"):
                    db.add(
                        ServiceConfig(
                            service_type=Service.TAUTULLI,
                            base_url=f"http://{name}",
                            api_key="key",
                            name=name,
                            enabled=True,
                        )
                    )
                await db.commit()

            def build_client(index: int) -> AsyncMock:
                client = AsyncMock()

                async def get_users() -> list[dict[str, object]]:
                    # By the second provider the first one's rows would already
                    # have been written under the old interleaved refresh.
                    if index == 2:
                        probe["error"] = _probe_write_from_another_connection(db_path)
                    return [{"user_id": index, "username": f"user{index}"}]

                client.get_users = get_users
                return client

            with (
                patch.object(watch_identity, "async_db", sessionmaker),
                patch.object(
                    watch_identity,
                    "TautulliClient",
                    side_effect=[build_client(1), build_client(2)],
                ),
                patch.object(watch_identity, "_config_api_key", return_value="key"),
            ):
                ok, error = await refresh_watch_user_aliases()

            assert ok, error
            assert "error" in probe, "second provider was never polled"
            assert probe["error"] is None, probe["error"]

            async with sessionmaker() as db:
                rows = (await db.execute(select(WatchUserAlias))).scalars().all()
                assert {row.alias_normalized for row in rows} >= {"user1", "user2"}
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


def test_one_person_reported_by_several_providers_is_one_account() -> None:
    """Plex, the Tautulli watching it, and a Plex-bound Tracearr all describe
    the same people. Counting those as three accounts made every shared name
    look like a collision, which silently emptied the whole Plex index and left
    manual mappings as the only thing that worked.
    """

    async def scenario() -> None:
        engine, sessionmaker, db_path = await _build_db()
        try:
            async with sessionmaker() as db:
                configs = {}
                for service, name in (
                    (Service.PLEX, "Plex"),
                    (Service.TAUTULLI, "Tautulli"),
                    (Service.TRACEARR, "Tracearr"),
                ):
                    config = ServiceConfig(
                        service_type=service,
                        base_url=f"http://{name.lower()}",
                        api_key="key",
                        name=name,
                        enabled=True,
                    )
                    db.add(config)
                    await db.flush()
                    configs[service] = config.id

                # Every row is observed on Plex, but each provider uses its own
                # id space and reports a different subset of the same names.
                rows = (
                    (Service.PLEX, "133423146", ("Black Widow", "BlackWidow05")),
                    (Service.TAUTULLI, "133423146", ("BlackWidow05", "nat@x.com")),
                    (Service.TRACEARR, "9f-uuid", ("BlackWidow05", "nat@x.com")),
                )
                for service, provider_user_id, aliases in rows:
                    for alias in (*aliases, provider_user_id):
                        db.add(
                            WatchUserAlias(
                                source_service=service,
                                source_service_config_id=configs[service],
                                observed_service=Service.PLEX,
                                provider_user_id=provider_user_id,
                                alias=alias,
                                alias_normalized=alias.lower(),
                            )
                        )
                await db.commit()

                index = await load_watch_user_alias_index(db)

            # Plex already knows the title and the username belong together, so
            # neither needs a hand-written mapping.
            assert expand_watch_keys({"black widow"}, index, Service.PLEX) == {
                "black widow",
                "blackwidow05",
                "nat@x.com",
                "133423146",
                "9f-uuid",
            }
            # And the Seerr email reaches the same plays.
            assert "black widow" in expand_watch_keys(
                {"nat@x.com"}, index, Service.PLEX
            )
        finally:
            await engine.dispose()
            if db_path.exists():
                db_path.unlink()

    _run(scenario())


def test_provider_ids_never_bridge_two_providers() -> None:
    """Plex keys its owner as the literal "1" and Tautulli numbers its own users
    from 1. That coincidence must not merge two strangers.
    """

    async def scenario() -> None:
        engine, sessionmaker, db_path = await _build_db()
        try:
            async with sessionmaker() as db:
                plex = ServiceConfig(
                    service_type=Service.PLEX,
                    base_url="http://plex",
                    api_key="key",
                    name="Plex",
                    enabled=True,
                )
                tautulli = ServiceConfig(
                    service_type=Service.TAUTULLI,
                    base_url="http://tautulli",
                    api_key="key",
                    name="Tautulli",
                    enabled=True,
                )
                db.add_all([plex, tautulli])
                await db.flush()
                for config, service, provider_user_id, alias in (
                    (plex, Service.PLEX, "1", "owner"),
                    (plex, Service.PLEX, "1", "1"),
                    (tautulli, Service.TAUTULLI, "1", "stranger"),
                    (tautulli, Service.TAUTULLI, "1", "1"),
                ):
                    db.add(
                        WatchUserAlias(
                            source_service=service,
                            source_service_config_id=config.id,
                            observed_service=Service.PLEX,
                            provider_user_id=provider_user_id,
                            alias=alias,
                            alias_normalized=alias,
                        )
                    )
                await db.commit()

                index = await load_watch_user_alias_index(db)

            assert expand_watch_keys({"owner"}, index, Service.PLEX) == {"owner"}
            assert "stranger" not in expand_watch_keys(
                {"owner"}, index, Service.PLEX
            )
        finally:
            await engine.dispose()
            if db_path.exists():
                db_path.unlink()

    _run(scenario())


def test_two_same_type_configs_pull_from_their_own_client() -> None:
    """Two linked Plex configs must each query their own client's directory.

    Before service_manager.get_media_server(config_id) was threaded through
    _plex_directory/_emby_directory, both configs resolved through the type
    singleton (return_service), so a second same-type config's aliases were
    silently built from whichever Plex client was initialized last - never
    its own server.
    """

    async def scenario() -> None:
        engine, sessionmaker, db_path = await _build_db()
        try:
            async with sessionmaker() as db:
                config_a = ServiceConfig(
                    service_type=Service.PLEX,
                    base_url="http://plex-a",
                    api_key="key-a",
                    name="Plex A",
                    enabled=True,
                )
                config_b = ServiceConfig(
                    service_type=Service.PLEX,
                    base_url="http://plex-b",
                    api_key="key-b",
                    name="Plex B",
                    enabled=True,
                )
                db.add_all([config_a, config_b])
                await db.commit()
                config_a_id, config_b_id = config_a.id, config_b.id

            client_a = create_autospec(PlexService, instance=True)
            client_a.get_watch_user_directory.return_value = [
                WatchUserDirectoryEntry(provider_user_id="1", aliases=("alice", "1"))
            ]
            client_b = create_autospec(PlexService, instance=True)
            client_b.get_watch_user_directory.return_value = [
                WatchUserDirectoryEntry(provider_user_id="2", aliases=("bob", "2"))
            ]

            def fake_get_media_server(service_type, config_id=None):
                assert service_type is Service.PLEX
                return {config_a_id: client_a, config_b_id: client_b}.get(config_id)

            with (
                patch.object(watch_identity, "async_db", sessionmaker),
                patch.object(
                    watch_identity.service_manager,
                    "get_media_server",
                    side_effect=fake_get_media_server,
                ),
            ):
                ok, error = await refresh_watch_user_aliases()

            assert ok, error
            async with sessionmaker() as db:
                rows = (await db.execute(select(WatchUserAlias))).scalars().all()
                by_config: dict[int, set[str]] = {}
                for row in rows:
                    by_config.setdefault(row.source_service_config_id, set()).add(
                        row.alias_normalized
                    )

            assert by_config[config_a_id] == {"alice", "1"}
            assert by_config[config_b_id] == {"bob", "2"}
        finally:
            await engine.dispose()
            if db_path.exists():
                db_path.unlink()

    _run(scenario())


def test_two_same_type_emby_family_configs_pull_from_their_own_client() -> None:
    """Same regression as above, for the Jellyfin/Emby directory path."""

    async def scenario() -> None:
        engine, sessionmaker, db_path = await _build_db()
        try:
            async with sessionmaker() as db:
                config_a = ServiceConfig(
                    service_type=Service.JELLYFIN,
                    base_url="http://jellyfin-a",
                    api_key="key-a",
                    name="Jellyfin A",
                    enabled=True,
                )
                config_b = ServiceConfig(
                    service_type=Service.JELLYFIN,
                    base_url="http://jellyfin-b",
                    api_key="key-b",
                    name="Jellyfin B",
                    enabled=True,
                )
                db.add_all([config_a, config_b])
                await db.commit()
                config_a_id, config_b_id = config_a.id, config_b.id

            client_a = AsyncMock()
            client_a.get_users.return_value = [EmbyUserBase(name="Alice", id="1")]
            client_b = AsyncMock()
            client_b.get_users.return_value = [EmbyUserBase(name="Bob", id="2")]

            def fake_get_media_server(service_type, config_id=None):
                assert service_type is Service.JELLYFIN
                return {config_a_id: client_a, config_b_id: client_b}.get(config_id)

            with (
                patch.object(watch_identity, "async_db", sessionmaker),
                patch.object(
                    watch_identity.service_manager,
                    "get_media_server",
                    side_effect=fake_get_media_server,
                ),
                patch.object(watch_identity, "JellyfinService", AsyncMock),
            ):
                ok, error = await refresh_watch_user_aliases()

            assert ok, error
            async with sessionmaker() as db:
                rows = (await db.execute(select(WatchUserAlias))).scalars().all()
                by_config: dict[int, set[str]] = {}
                for row in rows:
                    by_config.setdefault(row.source_service_config_id, set()).add(
                        row.alias_normalized
                    )

            assert by_config[config_a_id] == {"alice", "1"}
            assert by_config[config_b_id] == {"bob", "2"}
        finally:
            await engine.dispose()
            if db_path.exists():
                db_path.unlink()

    _run(scenario())
