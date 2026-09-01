from __future__ import annotations

import asyncio
from asyncio import Lock, create_task
from asyncio import Task as AsyncTask
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.core.logger import LOG
from backend.core.seerr_identity import qualify_seerr_user_id
from backend.core.service_manager import service_manager
from backend.enums import MediaType, SeerrRequestStatus
from backend.models.services.seerr import SeerrRequest, SeerrUser
from backend.services.seerr import SeerrClient


@dataclass(slots=True)
class SeerrRequestSnapshot:
    """Request state for every configured Seerr, merged into one read model.

    Requester ids are instance-qualified (``"<service_config_id>:<user_id>"``),
    so merging is lossless: two Seerrs numbering different people 3 stay two
    requesters, and each one's request dates only ever gate their own plays.
    """

    requester_ids_by_key: dict[tuple[MediaType, int], set[str]]
    first_request_at_by_key_user: dict[tuple[MediaType, int], dict[str, datetime]]
    requester_identity_keys_by_user_id: dict[str, set[str]]
    latest_active_request_at_by_key: dict[tuple[MediaType, int], datetime]
    requester_users_by_id: dict[str, SeerrUser] = field(default_factory=dict)
    requester_ids_by_series_season: dict[tuple[int, int], set[str]] = field(
        default_factory=dict
    )
    first_request_at_by_series_season_user: dict[
        tuple[int, int], dict[str, datetime]
    ] = field(default_factory=dict)
    latest_active_request_at_by_series_season: dict[tuple[int, int], datetime] = field(
        default_factory=dict
    )


@dataclass(slots=True, frozen=True)
class SeerrInstanceUser:
    """One Seerr user, and which Seerr it came from."""

    service_config_id: int
    user: SeerrUser

    @property
    def qualified_id(self) -> str:
        return qualify_seerr_user_id(self.service_config_id, self.user.id)


@dataclass(slots=True, frozen=True)
class SeerrSnapshotState:
    """The merged snapshot plus which instances actually produced it.

    Rule evaluation needs the second half: a snapshot built from only the
    instances that answered looks complete, so a Seerr that is down but still
    holds active requests would read as "nobody asked for this" -- which for a
    cleanup rule is a delete.
    """

    merged: SeerrRequestSnapshot | None
    by_config_id: dict[int, SeerrRequestSnapshot]
    errors_by_config_id: dict[int, str]
    healthy_config_ids: set[int]
    configured_config_ids: set[int]

    @property
    def unavailable_config_ids(self) -> set[int]:
        return self.configured_config_ids - self.healthy_config_ids

    @property
    def error_summary(self) -> str | None:
        if not self.errors_by_config_id:
            return None
        return "; ".join(
            f"Seerr #{config_id}: {error}"
            for config_id, error in sorted(self.errors_by_config_id.items())
        )


@dataclass(slots=True)
class _SeerrInstanceState:
    """Per-instance fetch state, so one unreachable Seerr never blanks the rest."""

    snapshot: SeerrRequestSnapshot | None = None
    expires_at: datetime | None = None
    last_error: str | None = None
    users: list[SeerrUser] | None = None
    users_expires_at: datetime | None = None


def _empty_snapshot() -> SeerrRequestSnapshot:
    return SeerrRequestSnapshot(
        requester_ids_by_key={},
        first_request_at_by_key_user={},
        requester_identity_keys_by_user_id={},
        latest_active_request_at_by_key={},
    )


class SeerrSnapshotCache:
    """In-memory cache for Seerr user identities and request snapshots."""

    __slots__ = (
        "_instances",
        "_users_lock",
        "_users_ttl",
        "_users_empty_ttl",
        "_request_lock",
        "_request_ttl",
        "_request_refresh_task",
    )

    def __init__(
        self,
        *,
        users_ttl: timedelta = timedelta(minutes=5),
        users_empty_ttl: timedelta = timedelta(seconds=20),
        request_ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        self._instances: dict[int, _SeerrInstanceState] = {}
        self._users_lock = Lock()
        self._users_ttl = users_ttl
        self._users_empty_ttl = users_empty_ttl

        self._request_lock = Lock()
        self._request_ttl = request_ttl
        self._request_refresh_task: AsyncTask[None] | None = None

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _clients() -> dict[int, SeerrClient]:
        return service_manager.seerr_clients()

    def _state(self, config_id: int) -> _SeerrInstanceState:
        state = self._instances.get(config_id)
        if state is None:
            state = _SeerrInstanceState()
            self._instances[config_id] = state
        return state

    def _forget_removed_instances(self, config_ids: set[int]) -> None:
        """Drop cached state for Seerrs that are no longer configured."""
        for stale_id in set(self._instances) - config_ids:
            self._instances.pop(stale_id, None)

    @staticmethod
    def _normalize_user_display(user: SeerrUser) -> SeerrUser:
        if user.display_name or user.username:
            return user
        return SeerrUser(
            id=user.id,
            username=user.username,
            display_name=f"User {user.id}",
            email=user.email,
            plex_username=user.plex_username,
            plex_id=user.plex_id,
            jellyfin_username=user.jellyfin_username,
            jellyfin_user_id=user.jellyfin_user_id,
            raw=user.raw,
        )

    def _dedupe_and_sort_users(self, users: list[SeerrUser]) -> list[SeerrUser]:
        """Dedupe one instance's directory by user id.

        Called per instance, never across instances: the same user number on two
        Seerrs is two different people, and collapsing them would lose one.
        """
        by_id: dict[int, SeerrUser] = {}
        for user in users:
            existing = by_id.get(user.id)
            normalized = self._normalize_user_display(user)
            if existing is None:
                by_id[user.id] = normalized
                continue
            existing_score = int(bool(existing.display_name)) + int(
                bool(existing.username)
            )
            new_score = int(bool(normalized.display_name)) + int(
                bool(normalized.username)
            )
            if new_score > existing_score:
                by_id[user.id] = normalized

        deduped = list(by_id.values())
        deduped.sort(
            key=lambda item: (
                (item.display_name or item.username or "").strip().lower(),
                item.id,
            )
        )
        return deduped

    @staticmethod
    def _normalize_identity_key(raw: Any) -> str | None:
        value = str(raw or "").strip().lower()
        return value if value else None

    @staticmethod
    def _user_from_request_payload(user_id: int, raw: Mapping[str, Any]) -> SeerrUser:
        """Build a requester identity from a request's embedded requestedBy block.

        Used when Seerr's user directory is unreachable; the request payload is
        then the only place these identities come from.
        """

        def text(key: str) -> str | None:
            value = raw.get(key)
            return str(value) if value else None

        plex_id_raw = raw.get("plexId")
        try:
            plex_id = int(plex_id_raw) if plex_id_raw is not None else None
        except (TypeError, ValueError):
            plex_id = None

        return SeerrUser(
            id=user_id,
            username=text("username"),
            display_name=text("displayName"),
            email=text("email"),
            plex_username=text("plexUsername"),
            plex_id=plex_id,
            jellyfin_username=text("jellyfinUsername"),
            jellyfin_user_id=text("jellyfinUserId"),
            raw=raw,
        )

    # --------------------------------------------------------- snapshot build

    async def _build_instance_snapshot(
        self, config_id: int, client: SeerrClient
    ) -> SeerrRequestSnapshot:
        """Fetch and shape one Seerr's request state, with qualified requesters."""
        requests = await client.get_all_requests(filter="all")
        directory_identities: dict[str, set[str]] = {}
        requester_users_by_id: dict[str, SeerrUser] = {}
        try:
            users = await client.get_all_users()
            for user in users:
                qualified = qualify_seerr_user_id(config_id, user.id)
                requester_users_by_id[qualified] = self._normalize_user_display(user)
                keys = {
                    normalized
                    for candidate in user.identity_values()
                    if (normalized := self._normalize_identity_key(candidate))
                }
                if keys:
                    directory_identities.setdefault(qualified, set()).update(keys)
        except Exception as exc:
            LOG.debug(
                "Could not enrich Seerr requester identities from the user "
                f"directory of config {config_id}; request payload identities "
                f"will be used: {exc}"
            )

        requester_ids_by_key: dict[tuple[MediaType, int], set[str]] = {}
        first_request_at_by_key_user: dict[
            tuple[MediaType, int], dict[str, datetime]
        ] = {}
        latest_active_request_at_by_key: dict[tuple[MediaType, int], datetime] = {}
        requester_ids_by_series_season: dict[tuple[int, int], set[str]] = {}
        first_request_at_by_series_season_user: dict[
            tuple[int, int], dict[str, datetime]
        ] = {}
        latest_active_request_at_by_series_season: dict[tuple[int, int], datetime] = {}
        requester_identity_keys_by_user_id = directory_identities

        for req in requests:
            requester_key = qualify_seerr_user_id(config_id, req.requested_by_id)
            raw_requested_by = (
                req.raw.get("requestedBy", {}) if isinstance(req.raw, dict) else {}
            )
            if requester_key not in requester_users_by_id:
                requester_users_by_id[requester_key] = self._normalize_user_display(
                    self._user_from_request_payload(
                        req.requested_by_id, raw_requested_by
                    )
                )
            identity_bucket = requester_identity_keys_by_user_id.get(requester_key)
            if identity_bucket is None:
                identity_bucket = set()
                requester_identity_keys_by_user_id[requester_key] = identity_bucket
            for candidate in (
                raw_requested_by.get("username"),
                raw_requested_by.get("displayName"),
                raw_requested_by.get("email"),
                raw_requested_by.get("plexUsername"),
                raw_requested_by.get("plexId"),
                raw_requested_by.get("jellyfinUsername"),
                raw_requested_by.get("jellyfinUserId"),
            ):
                normalized = self._normalize_identity_key(candidate)
                if normalized:
                    identity_bucket.add(normalized)

            if req.status not in {
                SeerrRequestStatus.PENDING,
                SeerrRequestStatus.APPROVED,
                SeerrRequestStatus.COMPLETED,
            }:
                continue

            key = (req.media_type, req.tmdb_id)
            bucket = requester_ids_by_key.get(key)
            if bucket is None:
                bucket = set()
                requester_ids_by_key[key] = bucket
            bucket.add(requester_key)

            request_by_user = first_request_at_by_key_user.get(key)
            if request_by_user is None:
                request_by_user = {}
                first_request_at_by_key_user[key] = request_by_user
            existing = request_by_user.get(requester_key)
            # Earliest wins. This is the bar for "watched after they
            # asked for it", and a user can ask more than once: a 4K
            # request and a re-request of an airing season are separate
            # rows. Keeping the latest would move the bar past watches
            # that already happened and make a finished season read as
            # unwatched. `latest_active_request_at_*` below deliberately
            # keeps the newest, because request *age* is a different
            # question.
            if existing is None or req.created_at < existing:
                request_by_user[requester_key] = req.created_at

            if req.status in {
                SeerrRequestStatus.PENDING,
                SeerrRequestStatus.APPROVED,
            }:
                latest_active = latest_active_request_at_by_key.get(key)
                if latest_active is None or req.created_at > latest_active:
                    latest_active_request_at_by_key[key] = req.created_at

            for requested_season in req.requested_seasons:
                season_key = (req.tmdb_id, requested_season.season_number)
                requester_ids_by_series_season.setdefault(season_key, set()).add(
                    requester_key
                )
                by_user = first_request_at_by_series_season_user.setdefault(
                    season_key, {}
                )
                season_existing = by_user.get(requester_key)
                # Earliest wins, for the same reason as the series bar.
                if (
                    season_existing is None
                    or requested_season.created_at < season_existing
                ):
                    by_user[requester_key] = requested_season.created_at
                if req.status in {
                    SeerrRequestStatus.PENDING,
                    SeerrRequestStatus.APPROVED,
                }:
                    active_existing = latest_active_request_at_by_series_season.get(
                        season_key
                    )
                    if (
                        active_existing is None
                        or requested_season.created_at > active_existing
                    ):
                        latest_active_request_at_by_series_season[season_key] = (
                            requested_season.created_at
                        )

        return SeerrRequestSnapshot(
            requester_ids_by_key=requester_ids_by_key,
            first_request_at_by_key_user=first_request_at_by_key_user,
            requester_identity_keys_by_user_id=requester_identity_keys_by_user_id,
            latest_active_request_at_by_key=latest_active_request_at_by_key,
            requester_users_by_id=requester_users_by_id,
            requester_ids_by_series_season=requester_ids_by_series_season,
            first_request_at_by_series_season_user=(
                first_request_at_by_series_season_user
            ),
            latest_active_request_at_by_series_season=(
                latest_active_request_at_by_series_season
            ),
        )

    @staticmethod
    def _merge_snapshots(parts: list[SeerrRequestSnapshot]) -> SeerrRequestSnapshot:
        """Combine per-instance snapshots.

        Requester-keyed maps merge by plain update because a qualified id can
        only come from one instance. The two ``latest_active`` maps take the
        newest across instances, which keeps "how old is the newest live request"
        answering the same question it always did.
        """
        if len(parts) == 1:
            return parts[0]

        merged = _empty_snapshot()
        for part in parts:
            for key, ids in part.requester_ids_by_key.items():
                merged.requester_ids_by_key.setdefault(key, set()).update(ids)
            for key, by_user in part.first_request_at_by_key_user.items():
                merged.first_request_at_by_key_user.setdefault(key, {}).update(by_user)
            for user_key, identities in part.requester_identity_keys_by_user_id.items():
                merged.requester_identity_keys_by_user_id.setdefault(
                    user_key, set()
                ).update(identities)
            for key, requested_at in part.latest_active_request_at_by_key.items():
                existing = merged.latest_active_request_at_by_key.get(key)
                if existing is None or requested_at > existing:
                    merged.latest_active_request_at_by_key[key] = requested_at
            merged.requester_users_by_id.update(part.requester_users_by_id)
            for season_key, ids in part.requester_ids_by_series_season.items():
                merged.requester_ids_by_series_season.setdefault(
                    season_key, set()
                ).update(ids)
            for (
                season_key,
                by_user,
            ) in part.first_request_at_by_series_season_user.items():
                merged.first_request_at_by_series_season_user.setdefault(
                    season_key, {}
                ).update(by_user)
            for (
                season_key,
                requested_at,
            ) in part.latest_active_request_at_by_series_season.items():
                existing = merged.latest_active_request_at_by_series_season.get(
                    season_key
                )
                if existing is None or requested_at > existing:
                    merged.latest_active_request_at_by_series_season[season_key] = (
                        requested_at
                    )
        return merged

    # ------------------------------------------------------------- refreshing

    async def _refresh_instance(
        self, config_id: int, client: SeerrClient
    ) -> tuple[bool, str | None]:
        state = self._state(config_id)
        try:
            snapshot = await self._build_instance_snapshot(config_id, client)
        except Exception as exc:
            state.last_error = str(exc)
            return False, state.last_error
        state.snapshot = snapshot
        state.expires_at = datetime.now(UTC).replace(microsecond=0) + self._request_ttl
        state.last_error = None
        return True, None

    async def _refresh_instances(self, config_ids: list[int]) -> None:
        """Refresh the named instances concurrently; failures stay per instance."""
        clients = self._clients()
        targets = [(cid, clients[cid]) for cid in config_ids if cid in clients]
        if not targets:
            return
        results = await asyncio.gather(
            *(self._refresh_instance(cid, client) for cid, client in targets),
            return_exceptions=True,
        )
        for (config_id, _), result in zip(targets, results, strict=True):
            if isinstance(result, BaseException):
                self._state(config_id).last_error = str(result)

    def _instance_is_fresh(self, config_id: int, now: datetime) -> bool:
        state = self._instances.get(config_id)
        return (
            state is not None
            and state.snapshot is not None
            and state.expires_at is not None
            and now < state.expires_at
        )

    async def _refresh_request_snapshot(
        self, *, stale_only: bool = False
    ) -> tuple[bool, str | None]:
        """Refresh every configured instance. True only when all of them answered."""
        async with self._request_lock:
            clients = self._clients()
            self._forget_removed_instances(set(clients))
            if not clients:
                return False, "Seerr service is not configured"

            now = datetime.now(UTC)
            targets = [
                config_id
                for config_id in clients
                if not stale_only or not self._instance_is_fresh(config_id, now)
            ]
            await self._refresh_instances(targets)

            state = self._snapshot_state(set(self._clients()))
            if state.unavailable_config_ids:
                return False, state.error_summary
            return True, None

    async def _background_refresh_request_snapshot(self) -> None:
        try:
            ok, error = await self._refresh_request_snapshot(stale_only=True)
            if not ok and error:
                LOG.debug(f"Background Seerr snapshot refresh failed: {error}")
        finally:
            self._request_refresh_task = None

    def _kickoff_background_request_refresh(self) -> None:
        if (
            self._request_refresh_task is not None
            and not self._request_refresh_task.done()
        ):
            return
        self._request_refresh_task = create_task(
            self._background_refresh_request_snapshot()
        )

    # ------------------------------------------------------------------ reads

    def _snapshot_state(self, configured: set[int]) -> SeerrSnapshotState:
        by_config_id: dict[int, SeerrRequestSnapshot] = {}
        errors: dict[int, str] = {}
        for config_id in configured:
            state = self._instances.get(config_id)
            if state is None:
                errors[config_id] = "not refreshed yet"
                continue
            if state.snapshot is not None:
                by_config_id[config_id] = state.snapshot
            if state.snapshot is None or state.last_error:
                errors[config_id] = state.last_error or "not refreshed yet"

        healthy = {cid for cid in by_config_id if cid not in errors}
        merged = (
            self._merge_snapshots(list(by_config_id.values())) if by_config_id else None
        )
        return SeerrSnapshotState(
            merged=merged,
            by_config_id=by_config_id,
            errors_by_config_id=errors,
            healthy_config_ids=healthy,
            configured_config_ids=set(configured),
        )

    async def get_request_snapshot_state(
        self,
        *,
        require_fresh: bool,
        allow_stale_on_failure: bool,
    ) -> SeerrSnapshotState:
        """Return the merged snapshot alongside per-instance health.

        Callers that decide whether a rule may run need the health half; a
        snapshot built from only the instances that answered looks complete.
        """
        now = datetime.now(UTC)
        configured = set(self._clients())
        if not configured:
            self._forget_removed_instances(configured)
            return SeerrSnapshotState(
                merged=None,
                by_config_id={},
                errors_by_config_id={},
                healthy_config_ids=set(),
                configured_config_ids=set(),
            )

        all_fresh = all(
            self._instance_is_fresh(config_id, now) for config_id in configured
        )
        have_any = any(
            (state := self._instances.get(config_id)) is not None
            and state.snapshot is not None
            for config_id in configured
        )

        if require_fresh:
            await self._refresh_request_snapshot()
        elif not all_fresh:
            if have_any:
                self._kickoff_background_request_refresh()
            else:
                await self._refresh_request_snapshot()

        state = self._snapshot_state(set(self._clients()))
        if not allow_stale_on_failure and state.unavailable_config_ids:
            # The caller cannot act on partial data, so do not hand it any.
            return SeerrSnapshotState(
                merged=None,
                by_config_id=state.by_config_id,
                errors_by_config_id=state.errors_by_config_id,
                healthy_config_ids=state.healthy_config_ids,
                configured_config_ids=state.configured_config_ids,
            )
        return state

    async def get_request_snapshot(
        self,
        *,
        require_fresh: bool,
        allow_stale_on_failure: bool,
    ) -> tuple[SeerrRequestSnapshot | None, str | None]:
        """Return the merged snapshot and a summary of any instance failures."""
        state = await self.get_request_snapshot_state(
            require_fresh=require_fresh,
            allow_stale_on_failure=allow_stale_on_failure,
        )
        if not state.configured_config_ids:
            return None, "Seerr service is not configured"
        return state.merged, state.error_summary

    async def _build_user_fallback_from_requests(
        self, requests: list[SeerrRequest]
    ) -> list[SeerrUser]:
        fallback: list[SeerrUser] = []
        for req in requests:
            raw_requested_by = (
                req.raw.get("requestedBy", {}) if isinstance(req.raw, dict) else {}
            )
            if not isinstance(raw_requested_by, Mapping):
                raw_requested_by = {}
            fallback.append(
                self._user_from_request_payload(req.requested_by_id, raw_requested_by)
            )
        return fallback

    async def _instance_users(
        self, config_id: int, client: SeerrClient, *, force_refresh: bool
    ) -> list[SeerrUser]:
        state = self._state(config_id)
        now = datetime.now(UTC)
        if (
            not force_refresh
            and state.users is not None
            and state.users_expires_at is not None
            and now < state.users_expires_at
        ):
            return state.users

        try:
            users = await client.get_all_users()
        except Exception as exc:
            LOG.warning(
                f"Failed to fetch Seerr /user list for config {config_id}: {exc}"
            )
            try:
                requests = await client.get_all_requests(filter="all")
                users = await self._build_user_fallback_from_requests(requests)
            except Exception as fallback_exc:
                LOG.warning(
                    "Failed to build Seerr user fallback from requests for config "
                    f"{config_id}: {fallback_exc}"
                )
                return []

        normalized = self._dedupe_and_sort_users(users)
        state.users = normalized
        state.users_expires_at = now + (
            self._users_ttl if normalized else self._users_empty_ttl
        )
        return normalized

    async def get_users(
        self, *, force_refresh: bool = False
    ) -> list[SeerrInstanceUser]:
        """Return every configured Seerr's users, tagged with their instance."""
        async with self._users_lock:
            clients = self._clients()
            self._forget_removed_instances(set(clients))
            if not clients:
                return []

            results = await asyncio.gather(
                *(
                    self._instance_users(config_id, client, force_refresh=force_refresh)
                    for config_id, client in clients.items()
                ),
                return_exceptions=True,
            )
            collected: list[SeerrInstanceUser] = []
            for config_id, result in zip(clients, results, strict=True):
                if isinstance(result, BaseException):
                    LOG.warning(
                        f"Failed to load Seerr users for config {config_id}: {result}"
                    )
                    continue
                collected.extend(
                    SeerrInstanceUser(service_config_id=config_id, user=user)
                    for user in result
                )
            collected.sort(
                key=lambda item: (
                    (item.user.display_name or item.user.username or "")
                    .strip()
                    .lower(),
                    item.service_config_id,
                    item.user.id,
                )
            )
            return collected


seerr_snapshot_cache = SeerrSnapshotCache()
