from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.core.seerr_identity import seerr_config_id_of, seerr_user_id_of
from backend.database.models import (
    MovieArrRef,
    SeriesArrRef,
    ServiceConfig,
    ServiceMediaLibrary,
)
from backend.enums import MediaType, Service
from backend.models.media import (
    ArrRefResponse,
    SeerrLinkResponse,
    SeerrRequesterResponse,
)
from backend.services.seerr_cache import SeerrRequestSnapshot, seerr_snapshot_cache
from backend.user_types import MEDIA_SERVERS


def _item_url(base_url: str, route: str, identifier: str | None) -> str | None:
    normalized = str(identifier or "").strip()
    if not normalized:
        return None
    return f"{base_url.rstrip('/')}/{route}/{quote(normalized, safe='')}"


@dataclass(slots=True)
class MediaOriginLookup:
    movie_arr_refs: dict[int, list[ArrRefResponse]] = field(default_factory=dict)
    series_arr_refs: dict[int, list[ArrRefResponse]] = field(default_factory=dict)
    # config_id -> (display name, base url). Every enabled Seerr, because a
    # title is reachable in each of them regardless of who requested it.
    seerr_configs: dict[int, tuple[str, str]] = field(default_factory=dict)
    seerr_snapshot: SeerrRequestSnapshot | None = None

    def arr_refs(self, media_type: MediaType, media_id: int) -> list[ArrRefResponse]:
        refs = (
            self.movie_arr_refs
            if media_type is MediaType.MOVIE
            else self.series_arr_refs
        )
        return refs.get(media_id, [])

    def seerr_links(
        self, media_type: MediaType, tmdb_id: int | None
    ) -> list[SeerrLinkResponse]:
        """One link per configured Seerr, mirroring how arr refs are listed."""
        if not self.seerr_configs or tmdb_id is None:
            return []
        route = "movie" if media_type is MediaType.MOVIE else "tv"
        links = [
            SeerrLinkResponse(
                service_config_id=config_id,
                service_name=name,
                item_url=_item_url(base_url, route, str(tmdb_id)),
            )
            for config_id, (name, base_url) in self.seerr_configs.items()
        ]
        links.sort(
            key=lambda link: (
                (link.service_name or "").casefold(),
                link.service_config_id,
            )
        )
        return links

    def seerr_requesters(
        self,
        media_type: MediaType,
        tmdb_id: int | None,
        *,
        season_number: int | None = None,
    ) -> list[SeerrRequesterResponse]:
        if self.seerr_snapshot is None or tmdb_id is None:
            return []

        requester_ids: set[str]
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
        for requester_key in requester_ids:
            config_id = seerr_config_id_of(requester_key)
            bare_user_id = seerr_user_id_of(requester_key)
            if config_id is None or bare_user_id is None:
                continue
            user = self.seerr_snapshot.requester_users_by_id.get(requester_key)
            display_name = (
                (user.display_name or user.username).strip()  # type: ignore[reportOptionalMemberAccess]
                if user and (user.display_name or user.username)
                else f"User {bare_user_id}"
            )
            name, _base_url = self.seerr_configs.get(config_id, (None, ""))
            requesters.append(
                SeerrRequesterResponse(
                    key=requester_key,
                    service_config_id=config_id,
                    service_name=name,
                    user_id=bare_user_id,
                    display_name=display_name,
                    username=user.username if user else None,
                )
            )
        requesters.sort(
            key=lambda item: (
                item.display_name.casefold(),
                item.service_config_id,
                item.user_id,
            )
        )
        return requesters


async def load_media_origin_lookup(
    db: AsyncSession,
    *,
    movie_ids: Collection[int] = (),
    series_ids: Collection[int] = (),
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

    lookup.seerr_configs = {
        config.id: (config.name or "Seerr", config.base_url)
        for config in config_rows
        if config.service_type is Service.SEERR
    }
    if lookup.seerr_configs:
        # Display can live with partial data -- a missing instance costs a badge,
        # not a deletion -- so stale answers are preferred over none.
        snapshot, error = await seerr_snapshot_cache.get_request_snapshot(
            require_fresh=False,
            allow_stale_on_failure=True,
        )
        lookup.seerr_snapshot = snapshot
        if error:
            LOG.debug(f"Using available Seerr origin data after refresh error: {error}")

    return lookup


@dataclass(slots=True, frozen=True)
class LibraryOrigin:
    """A media library and the server it was read from.

    Only the main media server contributes library rows, so in practice every
    origin names the same server -- but naming it is the point: with several
    servers configured, "Movies" alone does not say whose Movies it is.
    """

    library_id: str
    library_name: str
    service_config_id: int | None
    service_name: str | None

    def label(self, *, qualify: bool) -> str:
        """The library name, qualified with its server when that is ambiguous."""
        if not qualify or not self.service_name:
            return self.library_name
        return f"{self.library_name} ({self.service_name})"


@dataclass(slots=True)
class LibraryOriginLookup:
    """Library origins keyed by library id, plus how many servers are configured.

    `qualify` is the single place the "one server needs no disambiguation,
    several do" rule is decided, so every caller labels consistently.
    """

    origins: dict[str, LibraryOrigin] = field(default_factory=dict)
    media_server_count: int = 0

    @property
    def qualify(self) -> bool:
        return self.media_server_count > 1

    def get(self, library_id: str | None) -> LibraryOrigin | None:
        if not library_id:
            return None
        return self.origins.get(library_id)

    def name_for(self, library_id: str | None) -> str | None:
        """The server to show beside this library, or None when it needs none.

        Deliberately empty with a single media server configured: naming the
        only server there is adds noise to every card and dialog. Clients treat
        a present name as "show this", so the decision lives here rather than
        being re-derived at each of the dozen display sites.
        """
        if not self.qualify:
            return None
        origin = self.get(library_id)
        return origin.service_name if origin else None

    def config_id_for(self, library_id: str | None) -> int | None:
        origin = self.get(library_id)
        return origin.service_config_id if origin else None

    def label(self, library_id: str | None, fallback: str) -> str:
        """Render a library for display, qualified by server when ambiguous."""
        origin = self.get(library_id)
        if origin is None:
            return fallback
        return origin.label(qualify=self.qualify)


async def load_library_origins(db: AsyncSession) -> LibraryOriginLookup:
    """Load every known media library with the server that reported it."""
    rows = (
        await db.execute(
            select(
                ServiceMediaLibrary.library_id,
                ServiceMediaLibrary.library_name,
                ServiceMediaLibrary.service_config_id,
                ServiceConfig.name,
                ServiceConfig.service_type,
            ).join(
                ServiceConfig,
                ServiceConfig.id == ServiceMediaLibrary.service_config_id,
                isouter=True,
            )
        )
    ).all()

    server_count = (
        await db.scalar(
            select(func.count(ServiceConfig.id)).where(
                ServiceConfig.service_type.in_(MEDIA_SERVERS)
            )
        )
        or 0
    )

    lookup = LibraryOriginLookup(media_server_count=server_count)
    for library_id, library_name, config_id, config_name, service_type in rows:
        if not library_id or not library_name:
            continue
        # First writer wins, matching the maps this replaces. Duplicate ids are
        # possible only across configs, and a library the main server no longer
        # reports is removed on the next sync.
        if library_id in lookup.origins:
            continue
        name = (config_name or None) if config_id is not None else None
        if name is None and service_type is not None:
            name = service_type.value.title()
        lookup.origins[library_id] = LibraryOrigin(
            library_id=library_id,
            library_name=library_name,
            service_config_id=config_id,
            service_name=name,
        )
    return lookup
