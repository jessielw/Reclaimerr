"""Which media server config owns the item ids recorded on media rows.

A provider item id -- a Plex ratingKey, a Jellyfin or Emby item id -- is only
unique within the server that issued it. Reclaimerr stores those ids in two
places: shared columns written by exactly one server (``movie_versions``,
``episodes.plex_rating_key`` and friends), and ``supplemental_media_matches``,
keyed by the config that contributed it. Anything resolving an id back to media
therefore has to know which server the id came from -- otherwise two servers of
the same type resolve each other's ids, and a play on one lands on whichever
title happens to share that id on the other.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import ServiceConfig
from backend.enums import Service
from backend.user_types import MEDIA_SERVERS


@dataclass(frozen=True, slots=True)
class MediaIdentityOwnership:
    """A snapshot of which media server writes which identity columns."""

    main_config_id: int | None
    main_service: Service | None
    services_by_config_id: Mapping[int, Service]

    def configs_owning_media_rows(self, service: Service) -> set[int]:
        """Configs whose item ids the movie/version rows hold for a service.

        Only the main server contributes those rows -- ``sync_movies`` skips
        every linked server -- so with a main designated this names at most one
        config. An install with no main yet has nothing to attribute away from
        the shared rows, so every server of the type still reads them.
        """

        if self.main_config_id is None or self.main_service is None:
            return self._configs_of(service)
        return {self.main_config_id} if service is self.main_service else set()

    def configs_owning_service_id_columns(self, service: Service) -> set[int]:
        """Configs whose item ids a service's shared id columns hold.

        The main server writes its own type's columns; a linked server writes
        them for a type the main server does not provide (``sync_linked_data``'s
        ``backfill_episode_ids``). Two linked servers of one type both write
        them, which this can no more disambiguate than the sync that wrote them.
        """

        if self.main_service is None or service is not self.main_service:
            return self._configs_of(service)
        return {self.main_config_id} if self.main_config_id is not None else set()

    def owns_media_rows(self, service: Service, config_id: int) -> bool:
        """Whether this config contributed the movie/version rows for a service."""

        return config_id in self.configs_owning_media_rows(service)

    def owns_service_id_columns(self, service: Service, config_id: int) -> bool:
        """Whether this config writes a service's shared id columns."""

        return config_id in self.configs_owning_service_id_columns(service)

    def main_config_id_for(self, service: Service) -> int | None:
        """The main server's config id, when the main server is of this type."""

        return self.main_config_id if self.main_service is service else None

    def _configs_of(self, service: Service) -> set[int]:
        return {
            config_id
            for config_id, config_service in self.services_by_config_id.items()
            if config_service is service
        }


async def load_media_identity_ownership(
    session: AsyncSession,
) -> MediaIdentityOwnership:
    """Read the current main/linked media server layout."""

    rows = (
        await session.execute(
            select(
                ServiceConfig.id,
                ServiceConfig.service_type,
                ServiceConfig.is_main,
            ).where(ServiceConfig.service_type.in_(MEDIA_SERVERS))
        )
    ).all()
    main = next((row for row in rows if row.is_main), None)
    return MediaIdentityOwnership(
        main_config_id=main.id if main is not None else None,
        main_service=main.service_type if main is not None else None,
        services_by_config_id={row.id: row.service_type for row in rows},
    )
