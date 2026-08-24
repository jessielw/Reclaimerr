"""Registry of the names each playback provider knows its users by.

Requester watch rules have to answer "did the person who requested this watch
it", but every source spells that person differently: Seerr may hold a display
name, Plex history an account id, Tautulli a friendly name, and Tracearr its own
identity id. Matching those as bare strings only works when they happen to
agree, which is why a Seerr account with a custom username silently stopped
matching its own Plex plays.

This module records every alias a provider reports for an account, grouped under
one (config, provider_user_id) pair, so one known name expands to all of the
other names that same account uses before watch evidence is looked up.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.database import async_db
from backend.database.models import ServiceConfig, WatchUserAlias
from backend.enums import Service
from backend.models.media import WatchUserDirectoryEntry
from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService
from backend.services.playback_history import _config_api_key, _tracearr_bindings
from backend.services.plex import PlexService
from backend.services.tautulli import TautulliClient
from backend.services.tracearr import TracearrClient

# Services whose user directories feed the registry.
ALIAS_SOURCE_SERVICES = (
    Service.PLEX,
    Service.JELLYFIN,
    Service.EMBY,
    Service.TAUTULLI,
    Service.TRACEARR,
)

AliasIndex = dict[Service, dict[str, frozenset[str]]]


def normalize_identity(value: object) -> str | None:
    """Casefold one identity value, returning None when it carries no signal."""
    normalized = str(value or "").strip().lower()
    return normalized or None


def _entry_from_values(
    provider_user_id: object, values: Iterable[object]
) -> WatchUserDirectoryEntry | None:
    user_id = str(provider_user_id or "").strip()
    if not user_id:
        return None
    aliases = {text for value in values if (text := str(value or "").strip())}
    aliases.add(user_id)
    return WatchUserDirectoryEntry(
        provider_user_id=user_id, aliases=tuple(sorted(aliases))
    )


async def _plex_directory(config: ServiceConfig) -> list[WatchUserDirectoryEntry]:
    service = await service_manager.return_service(config.service_type)
    if not isinstance(service, PlexService):
        return []
    return await service.get_watch_user_directory()


async def _emby_directory(config: ServiceConfig) -> list[WatchUserDirectoryEntry]:
    service = await service_manager.return_service(config.service_type)
    if not isinstance(service, (JellyfinService, EmbyService)):
        return []
    entries: list[WatchUserDirectoryEntry] = []
    for user in await service.get_users():
        entry = _entry_from_values(user.id, (user.name,))
        if entry is not None:
            entries.append(entry)
    return entries


async def _tautulli_directory(config: ServiceConfig) -> list[WatchUserDirectoryEntry]:
    client = TautulliClient(api_key=_config_api_key(config), base_url=config.base_url)
    try:
        rows = await client.get_users()
    finally:
        await client.session.close()
    entries: list[WatchUserDirectoryEntry] = []
    for row in rows:
        entry = _entry_from_values(
            row.get("user_id"),
            (row.get("username"), row.get("friendly_name"), row.get("email")),
        )
        if entry is not None:
            entries.append(entry)
    return entries


async def _tracearr_directories(
    config: ServiceConfig,
) -> list[tuple[Service, WatchUserDirectoryEntry]]:
    """Return Tracearr identities keyed by the media server each one plays on."""
    bindings = _tracearr_bindings(config)
    if not bindings:
        return []
    observed_by_server = {
        binding.tracearr_server_id: binding.observed_service for binding in bindings
    }

    client = TracearrClient(api_key=_config_api_key(config), base_url=config.base_url)
    rows: list[Mapping[str, object]] = []
    try:
        cursor: str | None = None
        while True:
            page, cursor = await client.get_users_page(cursor=cursor)
            rows.extend(page)
            if not cursor or not page:
                break
    finally:
        await client.session.close()

    results: list[tuple[Service, WatchUserDirectoryEntry]] = []
    for row in rows:
        identity_id = str(row.get("id") or "").strip()
        if not identity_id:
            continue
        accounts = row.get("accounts")
        account_list = accounts if isinstance(accounts, list) else []
        # A Tracearr identity spans servers, so record it once per bound server
        # to keep its username under the same observed service as its plays.
        for account in account_list:
            if not isinstance(account, Mapping):
                continue
            server_id = str(account.get("server_id") or "").strip()
            observed = observed_by_server.get(server_id)
            if observed is None:
                continue
            entry = _entry_from_values(
                identity_id,
                (
                    account.get("username"),
                    account.get("email"),
                    row.get("username"),
                    row.get("email"),
                ),
            )
            if entry is not None:
                results.append((observed, entry))
    return results


async def _collect_aliases(
    config: ServiceConfig,
) -> list[tuple[Service, WatchUserDirectoryEntry]]:
    if config.service_type is Service.PLEX:
        return [(Service.PLEX, entry) for entry in await _plex_directory(config)]
    if config.service_type in {Service.JELLYFIN, Service.EMBY}:
        return [(config.service_type, entry) for entry in await _emby_directory(config)]
    if config.service_type is Service.TAUTULLI:
        # Tautulli only ever reports Plex accounts.
        return [(Service.PLEX, entry) for entry in await _tautulli_directory(config)]
    if config.service_type is Service.TRACEARR:
        return await _tracearr_directories(config)
    return []


async def _replace_aliases_for_config(
    session: AsyncSession,
    config_id: int,
    source_service: Service,
    entries: Sequence[tuple[Service, WatchUserDirectoryEntry]],
) -> int:
    if not entries:
        # Provider clients swallow their own transport errors and return
        # nothing, so an empty result cannot be told apart from a genuine
        # outage. Keeping the previous rows degrades to slightly stale
        # identities; clearing them would silently break requester matching.
        return 0

    await session.execute(
        sql_delete(WatchUserAlias).where(
            WatchUserAlias.source_service_config_id == config_id
        )
    )
    seen: set[tuple[str, str]] = set()
    written = 0
    for observed_service, entry in entries:
        for alias in entry.aliases:
            normalized = normalize_identity(alias)
            if normalized is None:
                continue
            identity = (entry.provider_user_id, normalized)
            if identity in seen:
                continue
            seen.add(identity)
            session.add(
                WatchUserAlias(
                    source_service=source_service,
                    source_service_config_id=config_id,
                    observed_service=observed_service,
                    provider_user_id=entry.provider_user_id,
                    alias=alias,
                    alias_normalized=normalized,
                )
            )
            written += 1
    return written


async def refresh_watch_user_aliases() -> tuple[bool, str | None]:
    """Rebuild the alias registry from every configured playback provider."""
    errors: list[str] = []
    total = 0
    async with async_db() as session:
        configs = list(
            (
                await session.execute(
                    select(ServiceConfig).where(
                        ServiceConfig.service_type.in_(ALIAS_SOURCE_SERVICES),
                        ServiceConfig.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        for config in configs:
            try:
                entries = await _collect_aliases(config)
            except Exception as exc:
                # A provider that cannot answer keeps its previous rows instead
                # of dropping identities mid-scan.
                errors.append(f"{config.service_type.value}: {exc}")
                LOG.warning(
                    "Watch identity refresh failed for "
                    f"{config.service_type.value} config {config.id}: {exc}"
                )
                continue
            total += await _replace_aliases_for_config(
                session, config.id, config.service_type, entries
            )
        await session.commit()

    LOG.debug(f"Registered {total} watch user alias(es)")
    if errors:
        return False, "; ".join(errors)
    return True, None


async def seed_watch_user_aliases_if_empty() -> None:
    """Register identities at startup when nothing has been recorded yet.

    The periodic playback refresh keeps the registry current, but on a fresh
    upgrade the first rule preview would otherwise run against an empty registry
    and match on plain names exactly as before. Only the empty case pays for
    provider round-trips.
    """
    try:
        async with async_db() as session:
            existing = (
                await session.execute(select(WatchUserAlias.id).limit(1))
            ).scalar_one_or_none()
        if existing is not None:
            return
        ok, error = await refresh_watch_user_aliases()
        if not ok and error:
            LOG.warning(f"Initial watch identity alias refresh incomplete: {error}")
    except Exception as exc:
        # Auxiliary: matching falls back to plain name comparison.
        LOG.warning(f"Initial watch identity alias refresh failed: {exc}")


# One directory account: what a single provider config calls one of its users.
DirectoryAccount = tuple[int, str]


async def load_watch_user_alias_index(session: AsyncSession) -> AliasIndex:
    """Return observed service -> alias -> every alias of that same person.

    Looking a known name up in this index yields the other names the person is
    recorded under, which is what lets a Seerr identity reach a Plex history key
    or a Tracearr username.

    One person is usually described by several providers at once: a Plex server,
    a Tautulli beside it and a Plex-bound Tracearr all report the same accounts
    under ``observed_service=PLEX``, each from its own config and under its own
    provider id. Those are merged by `merge_directory_accounts`.
    """
    rows = (
        await session.execute(
            select(
                WatchUserAlias.observed_service,
                WatchUserAlias.source_service_config_id,
                WatchUserAlias.provider_user_id,
                WatchUserAlias.alias_normalized,
            )
        )
    ).all()

    aliases_by_account: dict[Service, dict[DirectoryAccount, set[str]]] = {}
    for observed_service, config_id, provider_user_id, alias_normalized in rows:
        if not alias_normalized:
            continue
        account = (int(config_id), str(provider_user_id))
        aliases_by_account.setdefault(observed_service, {}).setdefault(
            account, set()
        ).add(str(alias_normalized))

    index: AliasIndex = {}
    for observed_service, accounts in aliases_by_account.items():
        people = merge_directory_accounts(accounts)
        # A name that survived into two different people proves nothing about
        # either, so it bridges nothing -- and dropping it keeps the answer
        # independent of the order the rows happened to arrive in.
        people_per_alias = Counter(alias for person in people for alias in person)
        shared = {alias for alias, count in people_per_alias.items() if count > 1}
        by_alias = index.setdefault(observed_service, {})
        for person in people:
            usable = person - shared
            if not usable:
                continue
            for alias in usable:
                by_alias[alias] = usable
    return index


def merge_directory_accounts(
    accounts: Mapping[DirectoryAccount, set[str]],
) -> list[frozenset[str]]:
    """Group one service's directory accounts into people.

    Two rules, pulling in opposite directions:

    * Within **one** config, an alias naming two accounts is a real collision --
      two Plex profiles can both be titled "Home". This index feeds deletion
      rules, so such an alias bridges nothing.
    * Across **different** configs, a shared alias is the opposite signal: the
      same person seen twice, once by Plex and once by the Tautulli watching it.
      Without merging those, every name a second provider also reports would
      look like a collision and the registry would bridge nothing at all.

    Provider ids never bridge across configs. Plex keying its owner as the
    literal "1" and Tautulli numbering its own users from 1 are unrelated facts,
    and treating that as a match would merge two strangers. Ids stay in a
    person's alias set -- Plex history really does report them -- they just do
    not supply the evidence for a merge.

    A merge is also refused when it would put two accounts of the *same* config
    into one person, since no provider lists one user twice.
    """
    holders_by_alias: dict[str, set[DirectoryAccount]] = {}
    for account, aliases in accounts.items():
        for alias in aliases:
            holders_by_alias.setdefault(alias, set()).add(account)
    provider_ids = {user_id.strip().lower() for _, user_id in accounts}

    def bridges(alias: str, account: DirectoryAccount) -> bool:
        """False when this alias names another user of the same config."""
        return not any(
            other != account and other[0] == account[0]
            for other in holders_by_alias.get(alias, ())
        )

    usable: dict[DirectoryAccount, set[str]] = {
        account: {alias for alias in aliases if bridges(alias, account)}
        for account, aliases in accounts.items()
    }

    parent: dict[DirectoryAccount, DirectoryAccount] = {a: a for a in accounts}
    members: dict[DirectoryAccount, set[DirectoryAccount]] = {a: {a} for a in accounts}

    def find(account: DirectoryAccount) -> DirectoryAccount:
        while parent[account] != account:
            parent[account] = parent[parent[account]]
            account = parent[account]
        return account

    for alias in sorted(holders_by_alias):
        if alias in provider_ids:
            continue
        holders = holders_by_alias[alias]
        bridging = sorted(account for account in holders if alias in usable[account])
        for account in bridging[1:]:
            left, right = find(bridging[0]), find(account)
            if left == right:
                continue
            if {config for config, _ in members[left]} & {
                config for config, _ in members[right]
            }:
                continue
            parent[right] = left
            members[left] |= members[right]
            del members[right]

    people: list[frozenset[str]] = []
    for group in members.values():
        aliases: set[str] = set()
        for account in group:
            aliases |= usable[account]
        if aliases:
            people.append(frozenset(aliases))
    return people


def expand_watch_keys(
    keys: Iterable[str],
    alias_index: AliasIndex,
    observed_service: Service,
) -> set[str]:
    """Expand known identity keys with every alias of the accounts they name."""
    expanded = {key for key in keys if key}
    by_alias = alias_index.get(observed_service)
    if not by_alias:
        return expanded
    for key in list(expanded):
        aliases = by_alias.get(key)
        if aliases:
            expanded.update(aliases)
    return expanded
