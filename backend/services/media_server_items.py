"""Resolve scoped provider item IDs from synced inventory and saved matches."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.utils.filesystem import normalize_fpath, path_tail
from backend.database.models import (
    Episode,
    MovieVersion,
    Season,
    SeriesServiceRef,
    ServiceConfig,
    ServiceMediaLibrary,
    SupplementalMediaMatch,
)
from backend.enums import MediaType, Service
from backend.models.media_identity import MediaServerItem
from backend.services.media_identity import load_media_identity_ownership


@dataclass(frozen=True, slots=True)
class MediaItemTarget:
    media_type: MediaType
    media_id: int
    scope: str
    movie_version_id: int | None = None
    season_id: int | None = None
    episode_id: int | None = None
    version_path: str | None = None

    @classmethod
    def from_schedule(cls, target: Mapping[str, Any]) -> "MediaItemTarget":
        return cls(
            MediaType(target["media_type"]),
            target["media_id"],
            target["scope"],
            movie_version_id=target.get("movie_version_id"),
            season_id=target.get("season_id"),
            episode_id=target.get("episode_id"),
            version_path=target.get("version_path"),
        )


async def load_media_server_items(
    db: AsyncSession, targets: Iterable[MediaItemTarget]
) -> dict[MediaItemTarget, list[MediaServerItem]]:
    items: dict[MediaItemTarget, list[MediaServerItem]] = {
        target: [] for target in targets
    }
    if not items:
        return items
    ownership = await load_media_identity_ownership(db)
    if not ownership.services_by_config_id:
        return items
    names = dict(
        (
            await db.execute(
                select(ServiceConfig.id, ServiceConfig.name).where(
                    ServiceConfig.id.in_(ownership.services_by_config_id)
                )
            )
        )
        .tuples()
        .all()
    )

    def add(
        target: MediaItemTarget,
        config_id: int | None,
        item_id: str | None,
        media_id: str | None = None,
    ) -> None:
        if config_id is None or not item_id:
            return
        service = ownership.services_by_config_id[config_id]
        items[target].append(
            MediaServerItem(
                source_service=service,
                source_service_config_id=config_id,
                source_service_name=names.get(config_id) or service.value,
                source_item_id=item_id,
                source_media_id=media_id or None,
            )
        )

    movie_ids = {t.media_id for t in items if t.media_type is MediaType.MOVIE}
    series_ids = {t.media_id for t in items if t.media_type is MediaType.SERIES}
    versions: defaultdict[int, list[MovieVersion]] = defaultdict(list)
    if movie_ids:
        for version in await db.scalars(
            select(MovieVersion).where(MovieVersion.movie_id.in_(movie_ids))
        ):
            versions[version.movie_id].append(version)
    series_refs: defaultdict[int, list[SeriesServiceRef]] = defaultdict(list)
    root_series_ids = {t.media_id for t in items if t.scope == "series"}
    if root_series_ids:
        for ref in await db.scalars(
            select(SeriesServiceRef).where(
                SeriesServiceRef.series_id.in_(root_series_ids)
            )
        ):
            series_refs[ref.series_id].append(ref)

    library_ids = {row.library_id for group in versions.values() for row in group}
    library_ids.update(
        row.library_id for group in series_refs.values() for row in group
    )
    library_owners: defaultdict[tuple[str, Service], set[int]] = defaultdict(set)
    if library_ids:
        for library_id, config_id in await db.execute(
            select(
                ServiceMediaLibrary.library_id, ServiceMediaLibrary.service_config_id
            ).where(ServiceMediaLibrary.library_id.in_(library_ids))
        ):
            service = (
                ownership.services_by_config_id.get(config_id)
                if config_id is not None
                else None
            )
            if config_id is not None and service is not None:
                library_owners[library_id, service].add(config_id)

    def shared_owner(service: Service, library_id: str) -> int | None:
        owners = library_owners[library_id, service]
        if not owners:
            owners = ownership.configs_owning_media_rows(service)
        return next(iter(owners)) if len(owners) == 1 else None

    season_ids = {t.season_id for t in items if t.season_id is not None}
    seasons = {}
    if season_ids:
        seasons = {
            s.id: s
            for s in await db.scalars(select(Season).where(Season.id.in_(season_ids)))
        }
    episode_ids = {t.episode_id for t in items if t.episode_id is not None}
    episodes = {}
    if episode_ids:
        episodes = {
            e.id: e
            for e in await db.scalars(
                select(Episode).where(Episode.id.in_(episode_ids))
            )
        }

    supplemental: defaultdict[tuple[MediaType, int], list[SupplementalMediaMatch]] = (
        defaultdict(list)
    )
    for match in await db.scalars(
        select(SupplementalMediaMatch).where(
            or_(
                SupplementalMediaMatch.movie_id.in_(movie_ids),
                SupplementalMediaMatch.series_id.in_(series_ids),
            )
        )
    ):
        parent_id = (
            match.movie_id if match.media_type is MediaType.MOVIE else match.series_id
        )
        if (
            parent_id is not None
            and ownership.services_by_config_id.get(match.source_service_config_id)
            is match.source_service
        ):
            supplemental[match.media_type, parent_id].append(match)

    for target in items:
        linked = supplemental[target.media_type, target.media_id]
        if target.media_type is MediaType.MOVIE:
            movie_versions = versions[target.media_id]
            if target.scope == "version" or target.scope == "movie_version":
                version = next(
                    (v for v in movie_versions if v.id == target.movie_version_id), None
                )
                if version is None or (
                    target.version_path is not None
                    and (
                        not version.path
                        or normalize_fpath(version.path, strip_ending_slash=True)
                        != target.version_path
                    )
                ):
                    continue
                add(
                    target,
                    shared_owner(version.service, version.library_id),
                    version.service_item_id,
                    version.service_media_id,
                )
                # Stored linked matches identify a file by its tail within this movie.
                tail = path_tail(version.path, 2)
                if (
                    tail
                    and sum(path_tail(v.path, 2) == tail for v in movie_versions) == 1
                ):
                    for match in linked:
                        if match.path_tail == tail and match.source_media_id:
                            add(
                                target,
                                match.source_service_config_id,
                                match.source_item_id,
                                match.source_media_id,
                            )
            elif target.scope == "movie":
                for version in movie_versions:
                    add(
                        target,
                        shared_owner(version.service, version.library_id),
                        version.service_item_id,
                        version.service_media_id,
                    )
                for match in linked:
                    add(
                        target,
                        match.source_service_config_id,
                        match.source_item_id,
                        match.source_media_id,
                    )
        elif target.scope == "series":
            for ref in series_refs[target.media_id]:
                add(target, shared_owner(ref.service, ref.library_id), ref.service_id)
            for match in linked:
                if match.season_id is None and match.episode_id is None:
                    add(target, match.source_service_config_id, match.source_item_id)
        else:
            season = (
                seasons.get(target.season_id) if target.season_id is not None else None
            )
            if season is None or season.series_id != target.media_id:
                continue
            if target.scope == "episode":
                episode = (
                    episodes.get(target.episode_id)
                    if target.episode_id is not None
                    else None
                )
                if episode is None or episode.season_id != season.id:
                    continue
                shared_ids = {
                    Service.PLEX: episode.plex_rating_key,
                    Service.JELLYFIN: episode.jellyfin_episode_id,
                    Service.EMBY: episode.emby_episode_id,
                }
                for match in linked:
                    numbers = (match.signals or {}).get("episode_numbers") or []
                    if match.episode_id == episode.id or (
                        match.season_id == season.id
                        and episode.episode_number in numbers
                    ):
                        add(
                            target, match.source_service_config_id, match.source_item_id
                        )
            elif target.scope == "season":
                shared_ids = {
                    Service.PLEX: season.plex_season_rating_key,
                    Service.JELLYFIN: season.jellyfin_season_id,
                    Service.EMBY: season.emby_season_id,
                }
                for match in linked:
                    if match.season_id == season.id and match.episode_id is None:
                        add(
                            target, match.source_service_config_id, match.source_item_id
                        )
            else:
                continue
            for service, item_id in shared_ids.items():
                for config_id in ownership.configs_owning_service_id_columns(service):
                    add(target, config_id, item_id)

    for target, refs in items.items():
        by_identity = {
            (
                ref.source_service_config_id,
                ref.source_item_id,
                ref.source_media_id or "",
            ): ref
            for ref in refs
        }
        items[target] = [by_identity[key] for key in sorted(by_identity)]
    return items
