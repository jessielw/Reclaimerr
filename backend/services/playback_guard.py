"""Operation-scoped live playback checks. Never cache a failed read as empty."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.utils.filesystem import (
    mapped_path_variants,
    media_move_source_scope,
    normalize_fpath,
    resolve_path,
)
from backend.database.models import (
    Episode,
    EpisodeVersion,
    GeneralSettings,
    MovieVersion,
    ReclaimCandidate,
    Season,
    SeriesServiceRef,
    ServiceConfig,
    SupplementalMediaMatch,
)
from backend.enums import Service
from backend.models.live_playback import (
    LiveSession,
    PlaybackDeferral,
    PlaybackDeferred,
    PlaybackSnapshot,
)
from backend.services.media_identity import load_media_identity_ownership
from backend.user_types import MEDIA_SERVERS

MAX_SNAPSHOT_AGE = 30.0
FETCH_TIMEOUT = 10.0


@dataclass
class PlaybackTarget:
    # Raw paths remain qualified by their originating configuration.
    paths: list[tuple[str, Service | None, int | None, bool]] = field(
        default_factory=list
    )
    # (configuration, item, version); None version means all versions of an item.
    identities: set[tuple[int, str, str | None]] = field(default_factory=set)
    parents: set[tuple[int, str]] = field(default_factory=set)
    relevant_configs: set[int] = field(default_factory=set)

    def extend(self, other: PlaybackTarget) -> None:
        self.paths.extend(other.paths)
        self.identities.update(other.identities)
        self.parents.update(other.parents)
        self.relevant_configs.update(other.relevant_configs)


@dataclass
class PlaybackGuard:
    enabled: bool
    mappings: list[dict[str, Any]]
    configs: dict[int, Service]
    snapshots: dict[int, tuple[float, PlaybackSnapshot]] = field(default_factory=dict)

    @classmethod
    async def load(cls, db: AsyncSession) -> PlaybackGuard:
        settings = (await db.scalars(select(GeneralSettings).limit(1))).first()
        configs = (
            await db.execute(
                select(ServiceConfig.id, ServiceConfig.service_type).where(
                    ServiceConfig.service_type.in_(MEDIA_SERVERS)
                )
            )
        ).all()
        return cls(
            settings.active_playback_protection_enabled if settings else True,
            list(settings.path_mappings or []) if settings else [],
            {row.id: row.service_type for row in configs},
        )

    async def _snapshot(self, config_id: int) -> PlaybackSnapshot:
        cached = self.snapshots.get(config_id)
        if cached and monotonic() - cached[0] < MAX_SNAPSHOT_AGE:
            return cached[1]
        from backend.core.service_manager import service_manager

        started = monotonic()
        client = service_manager.get_media_server(self.configs[config_id], config_id)
        try:
            async with asyncio.timeout(FETCH_TIMEOUT):
                snapshot = (
                    await client.get_live_sessions()
                    if client
                    else PlaybackSnapshot(available=False)
                )
            if not isinstance(snapshot, PlaybackSnapshot):
                snapshot = PlaybackSnapshot(available=False)
        except Exception:
            # Details may contain tokens in URLs; expose only a sanitized reason.
            snapshot = PlaybackSnapshot(available=False)
        self.snapshots[config_id] = (started, snapshot)
        return snapshot

    def variants(
        self, path: str, service: Service | None, config_id: int | None
    ) -> set[str]:
        return mapped_path_variants(
            path,
            self.mappings,
            service_type=service.value if service else None,
            service_config_id=config_id,
        )

    def expand_local_target(self, target: PlaybackTarget, *, moving: bool) -> None:
        """Account for local folder moves and same-stem cleanup before preparation."""
        if not self.enabled:
            return
        additions: set[tuple[str, Service | None, int | None, bool]] = set()
        try:
            for path, service, cid, directory in list(target.paths):
                if directory:
                    continue
                local = resolve_path(
                    path,
                    self.mappings,
                    service_type=service.value if service else None,
                    service_config_id=cid,
                    warn_missing=False,
                )
                if local is None or not local.is_file():
                    continue
                if moving:
                    source, is_directory = media_move_source_scope(local)
                    additions.add((str(source), None, None, is_directory))
                else:
                    additions.update(
                        (str(p), None, None, False)
                        for p in local.parent.iterdir()
                        if p.is_file() and p.stem == local.stem
                    )
        except OSError:
            raise PlaybackDeferred(
                PlaybackDeferral(
                    "playback_unavailable",
                    datetime.now(UTC).isoformat(),
                    tuple(sorted(target.relevant_configs)),
                )
            ) from None
        target.paths.extend(additions)

    def _matches(
        self, target: PlaybackTarget, config_id: int, session: LiveSession
    ) -> bool:
        paths = {
            p
            for raw in session.paths
            for p in self.variants(raw, self.configs[config_id], config_id)
        }
        for raw, service, source_config, directory in target.paths:
            variants = self.variants(raw, service, source_config)
            if any(
                p == v or (directory and p.startswith(v + "/"))
                for p in paths
                for v in variants
            ):
                return True
        # Cross-server content identity alone must not protect a different copy
        # when both sides have explicit, disjoint physical path mappings.
        session_mapped = [
            any(
                p != normalize_fpath(raw, strip_ending_slash=True)
                for p in self.variants(raw, self.configs[config_id], config_id)
            )
            for raw in session.paths
        ]
        target_mapped = [
            service is None
            or any(
                p != normalize_fpath(raw, strip_ending_slash=True)
                for p in self.variants(raw, service, cid)
            )
            for raw, service, cid, _ in target.paths
        ]
        if (
            session_mapped
            and all(session_mapped)
            and target_mapped
            and all(target_mapped)
            and all(cid != config_id for _, _, cid, _ in target.paths)
        ):
            return False
        if any((config_id, parent) in target.parents for parent in session.parent_ids):
            return True
        return any(
            cid == config_id
            and item == session.item_id
            and (
                version is None
                or session.media_id is None
                or version == session.media_id
            )
            for cid, item, version in target.identities
        )

    async def check(self, target: PlaybackTarget) -> None:
        if not self.enabled or not self.configs:
            return
        snapshots = await asyncio.gather(*(self._snapshot(cid) for cid in self.configs))
        playing: list[int] = []
        unavailable: list[int] = []
        # A path-only operation with no known server ownership cannot prove an
        # unavailable server unrelated. Candidate identities narrow this set.
        relevant = set(target.relevant_configs)
        for mapping in self.mappings:
            root = normalize_fpath(
                str(mapping.get("local_prefix") or ""), strip_ending_slash=True
            )
            if not root:
                continue
            overlaps = any(
                p == root or p.startswith(root + "/")
                for raw, service, cid, _ in target.paths
                for p in self.variants(raw, service, cid)
            )
            if overlaps:
                relevant.update(
                    cid
                    for cid, service in self.configs.items()
                    if mapping.get("service_config_id") == cid
                    or (
                        not mapping.get("service_config_id")
                        and mapping.get("service_type") == service.value
                    )
                )
        if not relevant:
            relevant = set(self.configs)
        for (cid, _), snapshot in zip(self.configs.items(), snapshots, strict=True):
            if not snapshot.available:
                if cid in relevant:
                    unavailable.append(cid)
            elif any(
                self._matches(target, cid, session) for session in snapshot.sessions
            ):
                playing.append(cid)
            elif cid in relevant and any(
                not s.paths and not s.item_id for s in snapshot.sessions
            ):
                unavailable.append(cid)
        if playing or unavailable:
            raise PlaybackDeferred(
                PlaybackDeferral(
                    "currently_playing" if playing else "playback_unavailable",
                    datetime.now(UTC).isoformat(),
                    tuple(playing or unavailable),
                )
            )


@dataclass
class PlaybackOperation:
    guard: PlaybackGuard
    target: PlaybackTarget
    completed_steps: list[str] = field(default_factory=list)


current_playback_operation: ContextVar[PlaybackOperation | None] = ContextVar(
    "current_playback_operation", default=None
)


async def playback_checkpoint(
    *,
    path: str | None = None,
    service: Service | None = None,
    config_id: int | None = None,
    directory: bool = False,
    siblings: bool = False,
    media_move: bool = False,
) -> None:
    """Check again before a side effect, expanding the target for broader routes."""
    operation = current_playback_operation.get()
    if operation is None:
        return
    if path:
        if media_move:
            source, directory = media_move_source_scope(Path(path))
            path = str(source)
        operation.target.paths.append((path, service, config_id, directory))
        if siblings:
            local = Path(path)
            if local.parent.is_dir():
                operation.target.paths.extend(
                    (str(p), None, None, False)
                    for p in local.parent.iterdir()
                    if p.is_file() and p.stem == local.stem
                )
    try:
        await operation.guard.check(operation.target)
    except PlaybackDeferred as exc:
        if operation.completed_steps:
            raise PlaybackDeferred(
                replace(exc.detail, completed_steps=tuple(operation.completed_steps))
            ) from None
        raise


def playback_step_completed(description: str) -> None:
    """Keep partial execution visible if a later checkpoint defers the remainder."""
    operation = current_playback_operation.get()
    if operation is not None:
        operation.completed_steps.append(description)


async def guard_arr_removal(
    client: Any, service: Service, ids: list[int], *, files: bool = False
) -> None:
    """Resolve the provider's actual deletion scope, including fallback expansion."""
    operation = current_playback_operation.get()
    if operation is None or not operation.guard.enabled or not operation.guard.configs:
        return
    from backend.core.service_manager import service_manager

    clients = (
        service_manager.radarr_clients()
        if service is Service.RADARR
        else service_manager.sonarr_clients()
    )
    config_id = next((cid for cid, value in clients.items() if value is client), None)
    try:
        for item_id in set(ids):
            if files:
                endpoint = "moviefile" if service is Service.RADARR else "episodefile"
                _, row = await client._make_request("GET", f"{endpoint}/{item_id}")
                path = row.get("path") if isinstance(row, dict) else None
            else:
                item = await (
                    client.get_movie(item_id)
                    if service is Service.RADARR
                    else client.get_series(item_id)
                )
                path = item.path
            if not isinstance(path, str) or not path:
                raise ValueError("Missing deletion path")
            await playback_checkpoint(
                path=path, service=service, config_id=config_id, directory=not files
            )
    except PlaybackDeferred:
        raise
    except Exception:
        raise PlaybackDeferred(
            PlaybackDeferral(
                "playback_unavailable",
                datetime.now(UTC).isoformat(),
                tuple(sorted(operation.target.relevant_configs)),
            )
        ) from None


async def guard_native_removal(
    client: Any, service: Service, item_id: str, media_id: str | None = None
) -> None:
    operation = current_playback_operation.get()
    if operation is None or not operation.guard.enabled or not operation.guard.configs:
        return
    from backend.core.service_manager import service_manager
    from backend.services.live_sessions import native_item_paths

    cid = next(
        (
            cid
            for cid, value in service_manager.media_server_clients(service).items()
            if value is client
        ),
        None,
    )
    try:
        paths = await native_item_paths(client, service, item_id, media_id)
        if not paths:
            raise ValueError("Missing deletion paths")
        for path, directory in paths:
            await playback_checkpoint(
                path=path, service=service, config_id=cid, directory=directory
            )
    except PlaybackDeferred:
        raise
    except Exception:
        raise PlaybackDeferred(
            PlaybackDeferral(
                "playback_unavailable",
                datetime.now(UTC).isoformat(),
                tuple(sorted(operation.target.relevant_configs)),
            )
        ) from None


async def candidate_playback_target(
    db: AsyncSession, candidate: ReclaimCandidate
) -> PlaybackTarget:
    target = PlaybackTarget()
    ownership = await load_media_identity_ownership(db)
    main = ownership.main_config_id
    if main is not None:
        target.relevant_configs.add(main)

    def version(row: MovieVersion | EpisodeVersion) -> None:
        owners = ownership.configs_owning_media_rows(row.service)
        for cid in owners:
            target.relevant_configs.add(cid)
            target.identities.add((cid, row.service_item_id, row.service_media_id))
            if row.path:
                target.paths.append((row.path, row.service, cid, False))
        if not owners and row.path:
            target.paths.append((row.path, row.service, None, False))

    if candidate.movie_id is not None:
        query = select(MovieVersion).where(MovieVersion.movie_id == candidate.movie_id)
        if candidate.movie_version_id is not None:
            query = query.where(MovieVersion.id == candidate.movie_version_id)
        for row in (await db.scalars(query)).all():
            version(row)
        matches = select(SupplementalMediaMatch).where(
            SupplementalMediaMatch.movie_id == candidate.movie_id
        )
    else:
        query = (
            select(Episode).join(Season).where(Season.series_id == candidate.series_id)
        )
        if candidate.episode_id is not None:
            query = query.where(Episode.id == candidate.episode_id)
        elif candidate.season_id is not None:
            query = query.where(Episode.season_id == candidate.season_id)
        episodes = (await db.scalars(query)).all()
        if candidate.season_id is not None and candidate.episode_id is None:
            season = await db.get(Season, candidate.season_id)
            if season is not None:
                target.paths.extend(
                    (p, ownership.main_service, main, False)
                    for p in season.episode_paths or []
                )
                # Season paths may equal the series root for flat layouts. Use
                # episode paths there, rather than protecting unrelated seasons.
                roots = (
                    await db.scalars(
                        select(SeriesServiceRef.path).where(
                            SeriesServiceRef.series_id == candidate.series_id
                        )
                    )
                ).all()
                if season.path and all(
                    normalize_fpath(season.path) != normalize_fpath(root)
                    for root in roots
                    if root
                ):
                    target.paths.append(
                        (season.path, ownership.main_service, main, True)
                    )
                for service, item in (
                    (Service.PLEX, season.plex_season_rating_key),
                    (Service.JELLYFIN, season.jellyfin_season_id),
                    (Service.EMBY, season.emby_season_id),
                ):
                    owners = ownership.configs_owning_service_id_columns(service)
                    if item and len(owners) == 1:
                        target.parents.add((next(iter(owners)), item))
        ids = [row.id for row in episodes]
        for row in (
            await db.scalars(
                select(EpisodeVersion).where(EpisodeVersion.episode_id.in_(ids))
            )
        ).all():
            version(row)
        for episode in episodes:
            if episode.path:
                target.paths.append((episode.path, ownership.main_service, main, False))
            for service, item in (
                (Service.PLEX, episode.plex_rating_key),
                (Service.JELLYFIN, episode.jellyfin_episode_id),
                (Service.EMBY, episode.emby_episode_id),
            ):
                owners = ownership.configs_owning_service_id_columns(service)
                if item and len(owners) == 1:
                    target.identities.add((next(iter(owners)), item, None))
        if candidate.season_id is None and candidate.episode_id is None:
            for ref in (
                await db.scalars(
                    select(SeriesServiceRef).where(
                        SeriesServiceRef.series_id == candidate.series_id
                    )
                )
            ).all():
                for cid in ownership.configs_owning_media_rows(ref.service):
                    target.parents.add((cid, ref.service_id))
                    if ref.path:
                        target.paths.append((ref.path, ref.service, cid, True))
        matches = select(SupplementalMediaMatch).where(
            SupplementalMediaMatch.series_id == candidate.series_id
        )
    for match in (await db.scalars(matches)).all():
        cid = match.source_service_config_id
        # Parent associations still establish exposure when the server is down.
        target.relevant_configs.add(cid)
        if candidate.movie_version_id is not None and match.path_tail and target.paths:
            # Narrow an existing high-confidence mapping to the selected version;
            # this never establishes a new media identity from a suffix alone.
            tail = normalize_fpath(match.path_tail).lstrip("/")
            if not any(
                normalize_fpath(path).endswith("/" + tail)
                for path, _, _, _ in target.paths
            ):
                continue
        if (
            candidate.episode_id is not None
            and match.episode_id != candidate.episode_id
        ):
            continue
        if (
            candidate.episode_id is None
            and candidate.season_id is not None
            and match.season_id != candidate.season_id
        ):
            continue
        target.identities.add((cid, match.source_item_id, match.source_media_id))
        if match.series_id is not None and match.episode_id is None:
            target.parents.add((cid, match.source_item_id))
    return target
