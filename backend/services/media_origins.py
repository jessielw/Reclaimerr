from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.database.models import (
    MovieArrRef,
    SeriesArrRef,
    ServiceConfig,
)
from backend.enums import MediaType, Service
from backend.models.media import ArrRefResponse, SeerrRequesterResponse
from backend.services.seerr_cache import SeerrRequestSnapshot, seerr_snapshot_cache


def _item_url(base_url: str, route: str, identifier: str | None) -> str | None:
    normalized = str(identifier or "").strip()
    if not normalized:
        return None
    return f"{base_url.rstrip('/')}/{route}/{quote(normalized, safe='')}"


@dataclass(slots=True)
class MediaOriginLookup:
    movie_arr_refs: dict[int, list[ArrRefResponse]] = field(default_factory=dict)
    series_arr_refs: dict[int, list[ArrRefResponse]] = field(default_factory=dict)
    seerr_base_url: str | None = None
    seerr_snapshot: SeerrRequestSnapshot | None = None

    def arr_refs(self, media_type: MediaType, media_id: int) -> list[ArrRefResponse]:
        refs = (
            self.movie_arr_refs
            if media_type is MediaType.MOVIE
            else self.series_arr_refs
        )
        return refs.get(media_id, [])

    def seerr_url(self, media_type: MediaType, tmdb_id: int | None) -> str | None:
        if self.seerr_base_url is None or tmdb_id is None:
            return None
        route = "movie" if media_type is MediaType.MOVIE else "tv"
        return _item_url(self.seerr_base_url, route, str(tmdb_id))

    def seerr_requesters(
        self,
        media_type: MediaType,
        tmdb_id: int | None,
        *,
        season_number: int | None = None,
    ) -> list[SeerrRequesterResponse]:
        if self.seerr_snapshot is None or tmdb_id is None:
            return []

        requester_ids: set[int]
        if media_type is MediaType.SERIES and season_number is not None:
            requester_ids = self.seerr_snapshot.requester_ids_by_series_season.get(
                (tmdb_id, season_number), set()
            )
            if not requester_ids:
                requester_ids = self.seerr_snapshot.requester_ids_by_key.get(
                    (media_type, tmdb_id), set()
                )
        else:
            requester_ids = self.seerr_snapshot.requester_ids_by_key.get(
                (media_type, tmdb_id), set()
            )

        requesters: list[SeerrRequesterResponse] = []
        for user_id in requester_ids:
            user = self.seerr_snapshot.requester_users_by_id.get(user_id)
            display_name = (
                (user.display_name or user.username).strip()
                if user and (user.display_name or user.username)
                else f"User {user_id}"
            )
            requesters.append(
                SeerrRequesterResponse(
                    user_id=user_id,
                    display_name=display_name,
                    username=user.username if user else None,
                )
            )
        requesters.sort(key=lambda item: (item.display_name.casefold(), item.user_id))
        return requesters


async def load_media_origin_lookup(
    db: AsyncSession,
    *,
    movie_ids: set[int] | list[int] = (),
    series_ids: set[int] | list[int] = (),
) -> MediaOriginLookup:
    """Load stored Arr origins and cached Seerr request context in bulk."""

    movie_id_set = set(movie_ids)
    series_id_set = set(series_ids)
    lookup = MediaOriginLookup()
    if not movie_id_set and not series_id_set:
        return lookup

    config_rows = (
        (
            await db.execute(
                select(ServiceConfig).where(
                    ServiceConfig.enabled.is_(True),
                    ServiceConfig.service_type.in_(
                        [Service.RADARR, Service.SONARR, Service.SEERR]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    config_by_id = {config.id: config for config in config_rows}

    if movie_id_set:
        refs = (
            (
                await db.execute(
                    select(MovieArrRef).where(MovieArrRef.movie_id.in_(movie_id_set))
                )
            )
            .scalars()
            .all()
        )
        for ref in refs:
            config = config_by_id.get(ref.service_config_id)
            if config is None or config.service_type is not Service.RADARR:
                continue
            lookup.movie_arr_refs.setdefault(ref.movie_id, []).append(
                ArrRefResponse(
                    service_type="radarr",
                    service_config_id=ref.service_config_id,
                    arr_id=ref.arr_movie_id,
                    service_name=config.name or "Radarr",
                    item_url=_item_url(config.base_url, "movie", ref.arr_title_slug),
                )
            )

    if series_id_set:
        refs = (
            (
                await db.execute(
                    select(SeriesArrRef).where(
                        SeriesArrRef.series_id.in_(series_id_set)
                    )
                )
            )
            .scalars()
            .all()
        )
        for ref in refs:
            config = config_by_id.get(ref.service_config_id)
            if config is None or config.service_type is not Service.SONARR:
                continue
            lookup.series_arr_refs.setdefault(ref.series_id, []).append(
                ArrRefResponse(
                    service_type="sonarr",
                    service_config_id=ref.service_config_id,
                    arr_id=ref.arr_series_id,
                    service_name=config.name or "Sonarr",
                    item_url=_item_url(config.base_url, "series", ref.arr_title_slug),
                )
            )

    for refs_by_id in (lookup.movie_arr_refs, lookup.series_arr_refs):
        for refs in refs_by_id.values():
            refs.sort(
                key=lambda ref: (
                    (ref.service_name or ref.service_type).casefold(),
                    ref.service_config_id,
                )
            )

    seerr_config = next(
        (config for config in config_rows if config.service_type is Service.SEERR),
        None,
    )
    if seerr_config is not None:
        lookup.seerr_base_url = seerr_config.base_url
        snapshot, error = await seerr_snapshot_cache.get_request_snapshot(
            require_fresh=False,
            allow_stale_on_failure=True,
        )
        lookup.seerr_snapshot = snapshot
        if error:
            LOG.debug(f"Using available Seerr origin data after refresh error: {error}")

    return lookup
