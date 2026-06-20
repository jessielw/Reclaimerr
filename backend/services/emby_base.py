from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal, TypeAlias

import niquests
from niquests.exceptions import HTTPError, ReadTimeout
from tenacity import (
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from backend.core.codecs import (
    normalize_audio_codec_family,
    normalize_video_codec_family,
)
from backend.core.logger import LOG
from backend.core.utils.filesystem import normalize_fpath
from backend.core.utils.language import normalize_language, normalize_languages
from backend.core.utils.misc import as_float, as_int, normalize_name_list
from backend.core.utils.request import format_http_failure, should_retry_on_status
from backend.core.utils.resolution import guesstimate_resolution
from backend.enums import MediaType, Service
from backend.models.media import (
    AggregatedEpisodeData,
    AggregatedMovieData,
    AggregatedSeasonData,
    AggregatedSeriesData,
    ExternalIDs,
    MovieVersionData,
)
from backend.models.services.emby_base import (
    EmbyMovieBase,
    EmbySeriesBase,
    EmbyUserBase,
    EmbyUserDataBase,
)
from backend.utils.helpers import normalize_leaving_soon_collection_title

RawSQL: TypeAlias = str
JsonDict: TypeAlias = dict[str, Any]
JsonList: TypeAlias = list[JsonDict]
JsonPayload: TypeAlias = JsonDict | JsonList

_COLLECTION_MUTATION_BATCH_SIZE = 100
_COLLECTION_PAGE_SIZE = 1000


@dataclass(slots=True)
class _MovieAggregate:
    name: str
    year: int | None
    external_ids: ExternalIDs
    versions: list[MovieVersionData]
    view_count: int
    last_viewed_at: datetime | None
    played_by_user_count: int


@dataclass(slots=True)
class _SeriesAggregate:
    id: str
    name: str
    year: int | None
    service: Literal[Service.JELLYFIN, Service.EMBY, Service.PLEX]
    library_id: str
    library_name: str
    path: str | None
    added_at: datetime | None
    external_ids: ExternalIDs
    size: int
    view_count: int
    last_viewed_at: datetime | None
    played_by_user_count: int
    media_server_collection_names: list[str] | None
    season_data: list[AggregatedSeasonData]


class EmbyServiceBase:
    """Emby/Jellyfin media server backend."""

    __slots__ = ("api_key", "service_url", "service_type", "session", "_library_map")

    def __init__(
        self,
        api_key: str,
        service_url: str,
        service_type: Literal[Service.EMBY, Service.JELLYFIN],
    ) -> None:
        self.api_key = api_key
        self.service_url = service_url.rstrip("/")
        if service_type not in {Service.EMBY, Service.JELLYFIN}:
            raise ValueError(f"EmbyServiceBase does not support {service_type}")
        self.service_type: Literal[Service.EMBY, Service.JELLYFIN] = service_type
        self.session = niquests.AsyncSession(timeout=300)
        self.session.headers.update(
            {
                "X-Emby-Token": self.api_key,
                "accept": "application/json",
            }
        )
        # cache of library ID to library name mapping
        self._library_map: dict[str, str] | None = None

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=(
            retry_if_exception_type((ConnectionError, TimeoutError, ReadTimeout))
            | retry_if_exception(should_retry_on_status)
        ),
    )
    async def _make_request(
        self, endpoint: str, params: JsonDict | None = None, **kwargs: Any
    ) -> JsonPayload:
        """Make HTTP request to Emby's/Jellyfin's API with automatic retry."""
        response = await self.session.get(
            f"{self.service_url}/{endpoint}",
            params=params,
            **kwargs,
        )
        response.raise_for_status()
        resp: JsonPayload = response.json()
        return resp

    async def health(self) -> bool:
        """Check server health and API key."""
        try:
            await self._make_request("System/Info")
            return True
        except Exception:
            return False

    async def delete_item(self, item_id: str) -> None:
        """Deletes an item (movie or series) from Emby/Jellyfin.

        Args:
            item_id: Emby/Jellyfin item ID
        """
        try:
            response = await self.session.delete(f"{self.service_url}/Items/{item_id}")
            response.raise_for_status()
            LOG.debug(f"Deleted {self.service_type} item {item_id}")
        except Exception as e:
            raise ValueError(
                format_http_failure(
                    action=f"Failed to delete {self.service_type} item {item_id}",
                    exception=e,
                )
            ) from e

    async def delete_movie_version(self, item_id: str, _media_source_id: str) -> None:
        """Deletes one movie version for Emby/Jellyfin.

        Emby/Jellyfin libraries represented as separate items per version can delete
        a single version by deleting that specific item id.
        """
        await self.delete_item(item_id)

    async def sync_leaving_soon_collections(
        self,
        *,
        base_title: str,
        movie_item_ids: set[str],
        series_item_ids: set[str],
    ) -> None:
        """Sync managed Leaving Soon collections for movies and series."""
        collection_base = normalize_leaving_soon_collection_title(base_title)
        await self._sync_leaving_soon_collection(
            collection_title=f"{collection_base} [Movies]",
            expected_item_ids=movie_item_ids,
            include_item_types="Movie",
        )
        await self._sync_leaving_soon_collection(
            collection_title=f"{collection_base} [Series]",
            expected_item_ids=series_item_ids,
            include_item_types="Series",
        )

    async def delete_leaving_soon_collections(self, *, base_title: str) -> None:
        """Delete managed Leaving Soon collections for a specific base title."""
        collection_base = normalize_leaving_soon_collection_title(base_title)
        await self._sync_leaving_soon_collection(
            collection_title=f"{collection_base} [Movies]",
            expected_item_ids=set(),
            include_item_types="Movie",
        )
        await self._sync_leaving_soon_collection(
            collection_title=f"{collection_base} [Series]",
            expected_item_ids=set(),
            include_item_types="Series",
        )

    async def prune_leaving_soon_items(
        self,
        *,
        base_title: str,
        movie_item_ids: set[str],
        series_item_ids: set[str],
    ) -> None:
        """Remove items from managed collections before destructive media actions."""
        collection_base = normalize_leaving_soon_collection_title(base_title)
        await self._prune_leaving_soon_collection(
            collection_title=f"{collection_base} [Movies]",
            item_ids=movie_item_ids,
            include_item_types="Movie",
        )
        await self._prune_leaving_soon_collection(
            collection_title=f"{collection_base} [Series]",
            item_ids=series_item_ids,
            include_item_types="Series",
        )

    async def _prune_leaving_soon_collection(
        self,
        *,
        collection_title: str,
        item_ids: set[str],
        include_item_types: str,
    ) -> None:
        normalized_item_ids = {
            str(item_id).strip() for item_id in item_ids if str(item_id).strip()
        }
        if not normalized_item_ids:
            return

        collection_ids = await self._find_collection_ids_by_title(collection_title)
        for collection_id in collection_ids:
            current_item_ids = await self._get_collection_item_ids(
                collection_id=collection_id,
                include_item_types=include_item_types,
            )
            items_to_remove = current_item_ids & normalized_item_ids
            if items_to_remove:
                await self._remove_items_from_collection(
                    collection_id=collection_id,
                    item_ids=items_to_remove,
                )

    async def _sync_leaving_soon_collection(
        self,
        *,
        collection_title: str,
        expected_item_ids: set[str],
        include_item_types: str,
    ) -> None:
        normalized_expected_ids = {
            str(item_id).strip()
            for item_id in expected_item_ids
            if str(item_id).strip()
        }
        existing_collection_ids = await self._find_collection_ids_by_title(
            collection_title
        )

        if not normalized_expected_ids:
            for collection_id in existing_collection_ids:
                await self._delete_collection(collection_id)
            return

        if not existing_collection_ids:
            normalized_expected_ids = await self._filter_existing_collection_item_ids(
                collection_title=collection_title,
                item_ids=normalized_expected_ids,
                include_item_types=include_item_types,
            )
            if not normalized_expected_ids:
                return
            await self._create_collection(
                collection_title=collection_title,
                item_ids=normalized_expected_ids,
            )
            return
        collection_id = existing_collection_ids[0]

        current_item_ids = await self._get_collection_item_ids(
            collection_id=collection_id,
            include_item_types=include_item_types,
        )
        items_to_add = normalized_expected_ids - current_item_ids
        items_to_remove = current_item_ids - normalized_expected_ids

        if items_to_add:
            items_to_add = await self._filter_existing_collection_item_ids(
                collection_title=collection_title,
                item_ids=items_to_add,
                include_item_types=include_item_types,
            )
        if items_to_add:
            await self._add_items_to_collection(
                collection_id=collection_id,
                item_ids=items_to_add,
            )
        if items_to_remove:
            await self._remove_items_from_collection(
                collection_id=collection_id,
                item_ids=items_to_remove,
            )

        for stale_collection_id in existing_collection_ids[1:]:
            await self._delete_collection(stale_collection_id)

    async def _get_paginated_items(
        self,
        *,
        params: JsonDict,
        timeout: int = 300,
    ) -> list[JsonDict]:
        items: list[JsonDict] = []
        start_index = 0

        while True:
            page_params = {
                **params,
                "StartIndex": start_index,
                "Limit": _COLLECTION_PAGE_SIZE,
                "EnableTotalRecordCount": "true",
            }
            data = await self._make_request(
                "Items",
                params=page_params,
                timeout=timeout,
            )
            if not isinstance(data, dict):
                break

            page_items = data.get("Items", [])
            if not isinstance(page_items, list) or not page_items:
                break

            items.extend(item for item in page_items if isinstance(item, dict))
            start_index += len(page_items)

            total_raw = data.get("TotalRecordCount")
            try:
                total = int(total_raw) if total_raw is not None else 0
            except (TypeError, ValueError):
                total = 0

            if total > 0 and start_index >= total:
                break
            if total <= 0 and len(page_items) < _COLLECTION_PAGE_SIZE:
                break

        return items

    async def _find_collection_ids_by_title(self, collection_title: str) -> list[str]:
        items = await self._get_paginated_items(
            params={
                "IncludeItemTypes": "BoxSet",
                "Recursive": "true",
                "Fields": "CollectionType",
            },
            timeout=300,
        )
        normalized_title = collection_title.strip().lower()
        collection_ids: list[str] = []
        for item in items:
            if str(item.get("Name", "")).strip().lower() != normalized_title:
                continue
            collection_id = str(item.get("Id", "")).strip()
            if collection_id:
                collection_ids.append(collection_id)
        return collection_ids

    async def _get_collection_item_ids(
        self, *, collection_id: str, include_item_types: str
    ) -> set[str]:
        items = await self._get_paginated_items(
            params={
                "ParentId": collection_id,
                "IncludeItemTypes": include_item_types,
                "Recursive": "true",
            },
            timeout=300,
        )
        item_ids: set[str] = set()
        for item in items:
            item_id = str(item.get("Id", "")).strip()
            if item_id:
                item_ids.add(item_id)
        return item_ids

    async def _filter_existing_collection_item_ids(
        self,
        *,
        collection_title: str,
        item_ids: set[str],
        include_item_types: str,
    ) -> set[str]:
        existing_item_ids: set[str] = set()
        normalized_item_ids = sorted(
            str(item_id).strip() for item_id in item_ids if str(item_id).strip()
        )

        for start in range(
            0, len(normalized_item_ids), _COLLECTION_MUTATION_BATCH_SIZE
        ):
            chunk = normalized_item_ids[start : start + _COLLECTION_MUTATION_BATCH_SIZE]
            data = await self._make_request(
                "Items",
                params={
                    "Ids": ",".join(chunk),
                    "IncludeItemTypes": include_item_types,
                    "Recursive": "true",
                    "EnableTotalRecordCount": "false",
                },
                timeout=300,
            )
            if not isinstance(data, dict):
                raise RuntimeError(
                    f"{self.service_type.value} returned an invalid response while "
                    f"validating items for collection {collection_title!r}"
                )
            returned_items = data.get("Items", [])
            if not isinstance(returned_items, list):
                raise RuntimeError(
                    f"{self.service_type.value} returned invalid item data while "
                    f"validating collection {collection_title!r}"
                )
            for item in returned_items:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("Id", "")).strip()
                if item_id in chunk:
                    existing_item_ids.add(item_id)

        missing_count = len(normalized_item_ids) - len(existing_item_ids)
        if missing_count:
            LOG.warning(
                f"Skipping {missing_count} missing {self.service_type.value} item(s) "
                f"while syncing Leaving Soon collection {collection_title!r}"
            )
        return existing_item_ids

    async def _get_collection_names_by_item_id(
        self, *, include_item_types: str
    ) -> dict[str, list[str]]:
        items = await self._get_paginated_items(
            params={
                "IncludeItemTypes": "BoxSet",
                "Recursive": "true",
                "Fields": "CollectionType",
            },
            timeout=300,
        )

        names_by_item_id: dict[str, list[str]] = {}
        for collection in items:
            collection_id = str(collection.get("Id", "")).strip()
            collection_name = str(collection.get("Name", "")).strip()
            if not collection_id or not collection_name:
                continue
            child_ids = await self._get_collection_item_ids(
                collection_id=collection_id,
                include_item_types=include_item_types,
            )
            for child_id in child_ids:
                names_by_item_id.setdefault(child_id, []).append(collection_name)

        return {
            item_id: names
            for item_id, raw_names in names_by_item_id.items()
            if (names := normalize_name_list(raw_names))
        }

    async def _create_collection(
        self, *, collection_title: str, item_ids: set[str]
    ) -> None:
        sorted_item_ids = sorted(item_ids)
        chunks = [
            sorted_item_ids[start : start + _COLLECTION_MUTATION_BATCH_SIZE]
            for start in range(
                0,
                len(sorted_item_ids),
                _COLLECTION_MUTATION_BATCH_SIZE,
            )
        ]
        if not chunks:
            return

        endpoint = f"{self.service_url}/Collections"
        response: Any | None = None
        try:
            response = await self.session.post(
                endpoint,
                params={
                    "Name": collection_title,
                    "Ids": ",".join(chunks[0]),
                    "IsLocked": "false",
                },
                timeout=60,
            )
            response.raise_for_status()
        except Exception as e:
            raise ValueError(
                format_http_failure(
                    action=(
                        f"Failed to create {self.service_type.value} collection "
                        f"{collection_title!r}"
                    ),
                    exception=e,
                    response=response,
                    method="POST",
                    endpoint=endpoint,
                )
            ) from e

        if len(chunks) == 1:
            return

        collection_id: str | None = None
        try:
            payload = response.json()
            if isinstance(payload, dict):
                collection_id = str(payload.get("Id", "")).strip() or None
        except Exception:
            collection_id = None

        if collection_id is None:
            collection_ids = await self._find_collection_ids_by_title(collection_title)
            collection_id = collection_ids[0] if collection_ids else None
        if collection_id is None:
            raise RuntimeError(
                f"Created {self.service_type.value} collection {collection_title!r} "
                "but could not resolve its collection ID"
            )

        await self._add_items_to_collection(
            collection_id=collection_id,
            item_ids={item_id for chunk in chunks[1:] for item_id in chunk},
        )

    async def _add_items_to_collection(
        self, *, collection_id: str, item_ids: set[str]
    ) -> None:
        endpoint = f"{self.service_url}/Collections/{collection_id}/Items"
        sorted_item_ids = sorted(item_ids)
        for start in range(
            0,
            len(sorted_item_ids),
            _COLLECTION_MUTATION_BATCH_SIZE,
        ):
            chunk = sorted_item_ids[start : start + _COLLECTION_MUTATION_BATCH_SIZE]
            response: Any | None = None
            try:
                response = await self.session.post(
                    endpoint,
                    params={"Ids": ",".join(chunk)},
                    timeout=60,
                )
                response.raise_for_status()
            except Exception as e:
                raise ValueError(
                    format_http_failure(
                        action=(
                            f"Failed to add items to {self.service_type.value} "
                            f"collection {collection_id}"
                        ),
                        exception=e,
                        response=response,
                        method="POST",
                        endpoint=endpoint,
                    )
                ) from e

    async def _remove_items_from_collection(
        self, *, collection_id: str, item_ids: set[str]
    ) -> None:
        endpoint = f"{self.service_url}/Collections/{collection_id}/Items"
        fallback_endpoint = (
            f"{self.service_url}/Collections/{collection_id}/Items/Delete"
        )
        sorted_item_ids = sorted(item_ids)

        for start in range(
            0,
            len(sorted_item_ids),
            _COLLECTION_MUTATION_BATCH_SIZE,
        ):
            chunk = sorted_item_ids[start : start + _COLLECTION_MUTATION_BATCH_SIZE]
            response: Any | None = None
            try:
                response = await self.session.delete(
                    endpoint,
                    params={"Ids": ",".join(chunk)},
                    timeout=60,
                )
                response.raise_for_status()
                continue
            except HTTPError as e:
                status_code = e.response.status_code if e.response is not None else None
                if status_code == 404:
                    pass
                else:
                    raise ValueError(
                        format_http_failure(
                            action=(
                                f"Failed to remove items from "
                                f"{self.service_type.value} collection {collection_id}"
                            ),
                            exception=e,
                            response=response,
                            method="DELETE",
                            endpoint=endpoint,
                        )
                    ) from e
            except Exception as e:
                raise ValueError(
                    format_http_failure(
                        action=(
                            f"Failed to remove items from "
                            f"{self.service_type.value} collection {collection_id}"
                        ),
                        exception=e,
                        response=response,
                        method="DELETE",
                        endpoint=endpoint,
                    )
                ) from e

            fallback_response: Any | None = None
            try:
                fallback_response = await self.session.post(
                    fallback_endpoint,
                    params={"Ids": ",".join(chunk)},
                    timeout=60,
                )
                fallback_response.raise_for_status()
            except Exception as e:
                raise ValueError(
                    format_http_failure(
                        action=(
                            f"Failed to remove items from "
                            f"{self.service_type.value} collection {collection_id}"
                        ),
                        exception=e,
                        response=fallback_response,
                        method="POST",
                        endpoint=fallback_endpoint,
                    )
                ) from e

    async def _delete_collection(self, collection_id: str) -> None:
        response = await self.session.delete(
            f"{self.service_url}/Items/{collection_id}",
            timeout=60,
        )
        try:
            response.raise_for_status()
        except HTTPError as e:
            raise ValueError(
                format_http_failure(
                    action=f"Failed to delete {self.service_type} collection {collection_id}",
                    exception=e,
                    response=response,
                )
            ) from e

    async def scan_item_path(self, item_path: str) -> bool:
        """Refresh a specific item by its filesystem path in Emby/Jellyfin.

        This triggers Emby/Jellyfin to scan the specific path, similar to how Radarr/Sonarr
        notify Emby/Jellyfin to update specific movie/series paths after deletion.

        Args:
            item_path: Filesystem path to the item (e.g., '/movies/The Matrix (1999)')

        Returns:
            True if refresh was triggered successfully, False otherwise
        """
        try:
            # Emby/Jellyfin's Library/Media/Updated endpoint accepts path-specific updates
            response = await self.session.post(
                f"{self.service_url}/Library/Media/Updated",
                json={"Updates": [{"Path": item_path, "UpdateType": "Deleted"}]},
            )
            response.raise_for_status()
            LOG.debug(f"Triggered {self.service_type} refresh for path: {item_path}")
            return True
        except Exception as e:
            LOG.error(f"Failed to refresh {self.service_type} path {item_path}: {e}")
            return False

    async def _get_media_libraries(self, media_type: str) -> list[dict[str, str]]:
        """Get list of media libraries of a specific type with their IDs and names."""
        virtual_folders = await self._make_request(
            "Library/VirtualFolders", timeout=300
        )
        media_libs: list[dict[str, str]] = []
        if not isinstance(virtual_folders, list):
            return media_libs
        for vf in virtual_folders:
            if not isinstance(vf, dict):
                continue
            if vf.get("CollectionType") == media_type:
                item_id = vf.get("ItemId")
                name = vf.get("Name")
                if item_id and name:
                    media_libs.append({"id": str(item_id), "name": str(name)})
        return media_libs

    async def get_movie_libraries(self) -> list[dict[str, str]]:
        """Get list of movie libraries with their IDs and names."""
        return await self._get_media_libraries("movies")

    async def get_series_libraries(self) -> list[dict[str, str]]:
        """Get list of TV series libraries with their IDs and names."""
        return await self._get_media_libraries("tvshows")

    async def get_users(self) -> list[EmbyUserBase]:
        """Get all Emby/Jellyfin users."""
        users_data = await self._make_request("Users")
        if not isinstance(users_data, list):
            return []
        return [
            EmbyUserBase(name=str(user["Name"]), id=str(user["Id"]))
            for user in users_data
            if isinstance(user, dict) and "Name" in user and "Id" in user
        ]

    async def get_favorite_tmdb_ids_by_user(
        self, media_type: Literal["movie", "series"]
    ) -> dict[str, set[int]]:
        """Return favorite TMDB IDs grouped by username for a media type.

        This intentionally queries each user directly via ``Users/{id}/Items`` so
        favorites remain user scoped and we can build a cross user snapshot.
        """
        include_item_types = "Movie" if media_type == "movie" else "Series"
        users = await self.get_users()
        if not users:
            return {}

        favorite_map: dict[str, set[int]] = {}
        page_size = 1000

        for user in users:
            username = (user.name or "").strip()
            if not username:
                continue

            start_index = 0
            user_tmdb_ids: set[int] = set()

            while True:
                params = {
                    "IncludeItemTypes": include_item_types,
                    "Recursive": "true",
                    "Fields": "ProviderIds",
                    "IsFavorite": "true",
                    "Filters": "IsFavorite",
                    "StartIndex": start_index,
                    "Limit": page_size,
                }
                data = await self._make_request(
                    f"Users/{user.id}/Items",
                    params=params,
                    timeout=300,
                )
                if not isinstance(data, dict):
                    break

                items = data.get("Items", [])
                if not isinstance(items, list) or not items:
                    break

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    provider_ids = item.get("ProviderIds", {})
                    if not isinstance(provider_ids, dict):
                        continue
                    tmdb_id_raw = provider_ids.get("Tmdb")
                    if tmdb_id_raw is None:
                        continue
                    tmdb_text = str(tmdb_id_raw).strip()
                    if tmdb_text.isdigit():
                        user_tmdb_ids.add(int(tmdb_text))

                # paginate
                total_record_count = data.get("TotalRecordCount")
                try:
                    total = (
                        int(total_record_count) if total_record_count is not None else 0
                    )
                except (TypeError, ValueError):
                    total = 0
                start_index += len(items)
                if start_index >= total:
                    break

            if user_tmdb_ids:
                favorite_map[username] = user_tmdb_ids

        return favorite_map

    async def get_movies_for_user(
        self,
        user_id: str,
        library_id: str,
        library_name: str,
        collection_names_by_item_id: dict[str, list[str]] | None = None,
        filters: JsonDict | None = None,
    ) -> list[EmbyMovieBase]:
        """Get movies for a specific user, optionally filtered by library.

        Args:
            user_id: The Emby/Jellyfin user ID
            library_id: Library ID to query
            library_name: The name of the library
            filters: Additional query filters
        """
        params = {
            "userId": user_id,
            "includeItemTypes": "Movie",
            "recursive": "true",
            "enableTotalRecordCount": "true",
            "Fields": (
                "ProviderIds,MediaSources,MediaStreams,Chapters,DateCreated,Path,UserData,"
                "UserDataLastPlayedDate,UserDataPlayCount,ProductionYear,PremiereDate"
            ),
            "ParentId": library_id,
        }

        if filters:
            params.update(filters)
        get_data = await self._make_request("Items", params=params, timeout=300)
        if not isinstance(get_data, dict):
            return []
        items_data = get_data.get("Items", [])
        if not isinstance(items_data, list):
            return []

        data = []
        for item in items_data:
            provider_ids = item.get("ProviderIds", {})
            tmdb_id = provider_ids.get("Tmdb")
            if not tmdb_id:
                continue  # skip items without TMDB ID (will be logged after aggregation)
            if not str(tmdb_id).isdigit():
                LOG.warning(
                    f"Skipping movie '{item.get('Name', item.get('Id'))}': "
                    f"invalid TMDb ID '{tmdb_id}' in {self.service_type} ProviderIds"
                )
                continue
            user_data_raw = item.get("UserData", {})
            user_data = EmbyUserDataBase(
                id=item["Id"],
                key=user_data_raw.get("Key", ""),
                play_count=user_data_raw.get("PlayCount", 0),
                last_played_date=datetime.fromisoformat(user_data_raw["LastPlayedDate"])
                if user_data_raw.get("LastPlayedDate")
                else None,
                played=user_data_raw.get("Played", False),
            )
            external_ids = ExternalIDs(
                imdb=provider_ids.get("Imdb"),
                tmdb=int(tmdb_id),
                tmdb_collection=provider_ids.get("TmdbCollection"),
                tvdb=provider_ids.get("Tvdb"),
            )
            collection_names = (collection_names_by_item_id or {}).get(item["Id"])
            # build one MovieVersionData per MediaSource (each = one physical file)
            added_at = (
                datetime.fromisoformat(item["DateCreated"])
                if item.get("DateCreated")
                else None
            )
            versions: list[MovieVersionData] = []
            for source in item.get("MediaSources", []):
                if not source.get("Id"):
                    continue
                normalized_fpath = (
                    PurePosixPath(normalize_fpath(source["Path"]))
                    if source.get("Path")
                    else None
                )
                video_streams, audio_streams, subtitle_streams = (
                    self._media_streams_by_type(source)
                )
                first_video = video_streams[0] if video_streams else {}
                first_audio = audio_streams[0] if audio_streams else {}
                video_codec_raw = first_video.get("Codec")
                audio_codec_raw = first_audio.get("Codec")
                dv_profile = (
                    first_video.get("DvProfile")
                    or first_video.get("DolbyVisionProfile")
                    or first_video.get("dv_profile")
                )
                dv_profile = (
                    f"{dv_profile}.{first_video['DvBlSignalCompatibilityId']}"
                    if dv_profile and first_video.get("DvBlSignalCompatibilityId")
                    else str(dv_profile)
                    if dv_profile
                    else None
                )

                run_time_ticks = as_float(source.get("RunTimeTicks"))
                width = as_int(first_video.get("Width"))
                height = as_int(first_video.get("Height"))
                versions.append(
                    MovieVersionData(
                        service=self.service_type,
                        service_item_id=item["Id"],
                        service_media_id=source["Id"],
                        library_id=library_id,
                        library_name=library_name,
                        path=str(normalized_fpath) if normalized_fpath else None,
                        size=source.get("Size", 0),
                        added_at=added_at,
                        file_name=normalized_fpath.name if normalized_fpath else None,
                        container=source.get("Container"),
                        duration=(run_time_ticks / 10000.0)
                        if run_time_ticks is not None
                        else None,
                        video_track_count=len(video_streams) or None,
                        video_codec=video_codec_raw,
                        video_codec_family=normalize_video_codec_family(
                            video_codec_raw
                        ),
                        video_hdr=self._is_hdr(first_video),
                        video_dolby_vision=first_video.get("DvProfile") is not None,
                        video_dolby_vision_profile=str(dv_profile)
                        if dv_profile is not None
                        else None,
                        video_bitrate=as_int(first_video.get("BitRate")),
                        video_bit_depth=as_int(first_video.get("BitDepth")),
                        video_width=width,
                        video_height=height,
                        video_resolution=guesstimate_resolution(width, height)
                        if width and height
                        else None,
                        video_color_primaries=first_video.get("ColorPrimaries"),
                        video_color_space=first_video.get("ColorSpace"),
                        video_color_transfer=first_video.get("ColorTransfer"),
                        video_fps=as_float(
                            first_video.get("RealFrameRate")
                            or first_video.get("AverageFrameRate")
                        ),
                        audio_count=len(audio_streams) or None,
                        audio_languages=self._unique_languages(audio_streams),
                        audio_codec=audio_codec_raw,
                        audio_codec_family=normalize_audio_codec_family(
                            audio_codec_raw
                        ),
                        audio_title=first_audio.get("DisplayTitle"),
                        audio_language=normalize_language(first_audio.get("Language")),
                        audio_channels=as_int(first_audio.get("Channels")),
                        audio_channel_layout=first_audio.get("ChannelLayout"),
                        audio_bitrate=as_int(first_audio.get("BitRate")),
                        audio_sample_rate=as_int(first_audio.get("SampleRate")),
                        subtitle_count=len(subtitle_streams) or None,
                        subtitle_has_forced=(
                            any(bool(s.get("IsForced")) for s in subtitle_streams)
                            if subtitle_streams
                            else None
                        ),
                        subtitle_languages=self._unique_languages(subtitle_streams),
                        has_chapters=(
                            bool(item.get("Chapters"))
                            if item.get("Chapters") is not None
                            else (
                                bool(source.get("Chapters"))
                                if source.get("Chapters") is not None
                                else None
                            )
                        ),
                        media_server_collection_names=collection_names,
                    )
                )
            movie = EmbyMovieBase(
                id=item["Id"],
                name=item["Name"],
                year=item.get("ProductionYear"),
                date_created=added_at,
                library_id=library_id,
                library_name=library_name,
                external_ids=external_ids,
                versions=versions,
                user_data=user_data,
                media_server_collection_names=collection_names,
            )
            data.append(movie)
        return data

    async def get_series_for_user(
        self,
        user_id: str,
        library_id: str,
        library_name: str,
        series_sizes: dict[str, int] | None = None,
        collection_names_by_item_id: dict[str, list[str]] | None = None,
        filters: JsonDict | None = None,
    ) -> list[EmbySeriesBase]:
        """Get TV series for a specific user, optionally filtered by library.

        Args:
            user_id: The Emby/Jellyfin user ID
            library_id: Library ID to query
            library_name: The name of the library
            series_sizes: Pre-calculated series sizes (series_id -> total bytes)
            filters: Additional query filters
        """
        params = {
            "userId": user_id,
            "includeItemTypes": "Series",
            "recursive": "true",
            "enableTotalRecordCount": "true",
            "Fields": (
                "ProviderIds,Path,UserData,UserDataLastPlayedDate,UserDataPlayCount,"
                "ProductionYear,PremiereDate,DateCreated"
            ),
            "ParentId": library_id,
        }

        if filters:
            params.update(filters)
        get_data = await self._make_request("Items", params=params, timeout=300)
        if not isinstance(get_data, dict):
            return []
        items_data = get_data.get("Items", [])
        if not isinstance(items_data, list):
            return []

        data = []
        for item in items_data:
            provider_ids = item.get("ProviderIds", {})
            tmdb_id = provider_ids.get("Tmdb")
            if not tmdb_id:
                continue  # skip items without TMDB ID
            if not str(tmdb_id).lstrip("-").isdigit():
                LOG.warning(
                    f"Skipping series '{item.get('Name', item.get('Id'))}': "
                    f"invalid TMDb ID '{tmdb_id}' in {self.service_type} ProviderIds"
                )
                continue
            user_data_raw = item.get("UserData", {})
            user_data = EmbyUserDataBase(
                id=item["Id"],
                key=user_data_raw.get("Key", ""),
                play_count=user_data_raw.get("PlayCount", 0),
                last_played_date=datetime.fromisoformat(user_data_raw["LastPlayedDate"])
                if user_data_raw.get("LastPlayedDate")
                else None,
                played=user_data_raw.get("Played", False),
            )
            provider_ids = item.get("ProviderIds", {})
            external_ids = ExternalIDs(
                imdb=provider_ids.get("Imdb"),
                tmdb=int(tmdb_id),
                tmdb_collection=provider_ids.get("TmdbCollection"),
                tvdb=provider_ids.get("Tvdb"),
            )

            # get size from pre-calculated series sizes (if available)
            total_size = series_sizes.get(item["Id"], 0) if series_sizes else 0

            series = EmbySeriesBase(
                id=item["Id"],
                name=item["Name"],
                year=item.get("ProductionYear"),
                date_created=datetime.fromisoformat(item.get("DateCreated")),
                library_id=library_id,
                library_name=library_name,
                path=str(normalize_fpath(item["Path"])) if item.get("Path") else None,
                external_ids=external_ids,
                size=total_size,
                user_data=user_data,
                media_server_collection_names=(collection_names_by_item_id or {}).get(
                    item["Id"]
                ),
            )
            data.append(series)
        return data

    async def get_series_sizes_for_library(
        self, library_id: str, user_id: str
    ) -> tuple[dict[str, int], dict[tuple[str, int], AggregatedSeasonData]]:
        """Get total sizes and season data for all series in a library.

        Args:
            library_id: The Emby/Jellyfin library ID
            user_id: The Emby/Jellyfin user ID to fetch episodes for

        Returns:
            Tuple of (series_sizes, season_data) where:
            - series_sizes: Dictionary mapping series_id to total size in bytes
            - season_data: Dictionary mapping (series_id, season_number) to AggregatedSeasonData
        """
        series_sizes: dict[str, int] = {}
        # season accumulation
        season_sizes: dict[tuple[str, int], int] = {}
        season_episode_counts: dict[tuple[str, int], int] = {}
        season_view_counts: dict[tuple[str, int], int] = {}
        season_last_viewed: dict[tuple[str, int], datetime | None] = {}
        season_ids: dict[tuple[str, int], str] = {}  # Emby/Jellyfin SeasonId
        season_air_date: dict[tuple[str, int], datetime | None] = {}
        season_added_at: dict[tuple[str, int], datetime | None] = {}
        season_has_hdr: dict[tuple[str, int], bool] = {}
        season_has_dv: dict[tuple[str, int], bool] = {}
        season_max_width: dict[tuple[str, int], int] = {}
        season_max_height: dict[tuple[str, int], int] = {}
        season_video_families: dict[tuple[str, int], set[str]] = {}
        season_audio_families: dict[tuple[str, int], set[str]] = {}
        season_audio_languages: dict[tuple[str, int], set[str]] = {}
        season_max_audio_channels: dict[tuple[str, int], int] = {}
        season_subtitle_languages: dict[tuple[str, int], set[str]] = {}
        season_paths: dict[tuple[str, int], str] = {}
        season_episode_paths: dict[tuple[str, int], list[str]] = {}
        # episode data: (series_id, season_number) -> list of AggregatedEpisodeData
        season_episode_data: dict[tuple[str, int], list[AggregatedEpisodeData]] = {}

        start_index = 0
        limit = 500

        while True:
            params = {
                "userId": user_id,
                "includeItemTypes": "Episode",
                "recursive": "true",
                "Fields": (
                    "MediaSources,MediaStreams,SeriesId,DateCreated,PremiereDate,ParentIndexNumber,SeasonId,UserData,"
                    "UserDataLastPlayedDate,UserDataPlayCount"
                ),
                "ParentId": library_id,
                "StartIndex": str(start_index),
                "Limit": str(limit),
            }

            get_data = await self._make_request("Items", params=params, timeout=60)
            if not isinstance(get_data, dict):
                break

            raw_episodes = get_data.get("Items", [])
            if not isinstance(raw_episodes, list) or not raw_episodes:
                break
            episodes = [
                episode for episode in raw_episodes if isinstance(episode, dict)
            ]
            if not episodes:
                break

            LOG.debug(
                f"Processing episodes {start_index} to {start_index + len(episodes)}"
            )

            for episode in episodes:
                series_id = episode.get("SeriesId")
                if not series_id:
                    continue

                # sum sizes
                episode_size = 0
                for source in episode.get("MediaSources", []):
                    episode_size += source.get("Size", 0)

                series_sizes[series_id] = series_sizes.get(series_id, 0) + episode_size

                # season accumulation
                season_num_raw = episode.get("ParentIndexNumber")
                if season_num_raw is None:
                    continue
                try:
                    season_num = int(season_num_raw)
                except (TypeError, ValueError):
                    continue

                sk = (series_id, season_num)
                season_sizes[sk] = season_sizes.get(sk, 0) + episode_size
                season_episode_counts[sk] = season_episode_counts.get(sk, 0) + 1

                # season air date = earliest episode premiere date
                premiere_date = episode.get("PremiereDate")
                if premiere_date:
                    try:
                        dt = datetime.fromisoformat(
                            premiere_date.replace("Z", "+00:00")
                        )
                        prev = season_air_date.get(sk)
                        if prev is None or dt < prev:
                            season_air_date[sk] = dt
                    except (TypeError, ValueError):
                        pass

                # season added_at = earliest episode date created
                created_date = episode.get("DateCreated")
                if created_date:
                    try:
                        dt = datetime.fromisoformat(created_date.replace("Z", "+00:00"))
                        prev = season_added_at.get(sk)
                        if prev is None or dt < prev:
                            season_added_at[sk] = dt
                    except (TypeError, ValueError):
                        pass

                # store Emby/Jellyfin SeasonId for first episode of each season
                if sk not in season_ids and episode.get("SeasonId"):
                    season_ids[sk] = episode["SeasonId"]

                # season path (first episode of each season)
                if sk not in season_paths:
                    for _source in episode.get("MediaSources", []):
                        _ep_path = _source.get("Path")
                        if _ep_path:
                            season_paths[sk] = str(
                                PurePosixPath(normalize_fpath(_ep_path)).parent
                            )
                            break

                # episode paths for this season
                for _source in episode.get("MediaSources", []):
                    _ep_path = _source.get("Path")
                    if _ep_path:
                        season_episode_paths.setdefault(sk, []).append(
                            normalize_fpath(_ep_path)
                        )
                        break

                # watch data
                user_data = episode.get("UserData", {})
                play_count = user_data.get("PlayCount", 0) or 0
                season_view_counts[sk] = season_view_counts.get(sk, 0) + play_count
                ep_last_viewed_at: datetime | None = None
                if play_count > 0 and user_data.get("LastPlayedDate"):
                    try:
                        lva = datetime.fromisoformat(user_data["LastPlayedDate"])
                        prev = season_last_viewed.get(sk)
                        if prev is None or lva > prev:
                            season_last_viewed[sk] = lva
                        ep_last_viewed_at = lva
                    except (TypeError, ValueError):
                        pass
                elif sk not in season_last_viewed:
                    season_last_viewed[sk] = None

                # collect episode data
                ep_index_raw = episode.get("IndexNumber")
                if ep_index_raw is not None:
                    try:
                        ep_num = int(ep_index_raw)
                    except (TypeError, ValueError):
                        ep_num = None
                    if ep_num is not None:
                        ep_path: str | None = None
                        ep_size_ep: int | None = None
                        for _src in episode.get("MediaSources", []):
                            _p = _src.get("Path")
                            if _p:
                                ep_path = normalize_fpath(_p)
                                ep_size_ep = _src.get("Size") or None
                                break
                        ep_air_ep: datetime | None = season_air_date.get(sk)
                        _ep_premiere = episode.get("PremiereDate")
                        if _ep_premiere:
                            try:
                                ep_air_ep = datetime.fromisoformat(
                                    _ep_premiere.replace("Z", "+00:00")
                                )
                            except (TypeError, ValueError):
                                pass
                        ep_item_id = str(episode.get("Id", ""))
                        season_episode_data.setdefault(sk, []).append(
                            AggregatedEpisodeData(
                                episode_number=ep_num,
                                name=episode.get("Name") or None,
                                view_count=play_count,
                                air_date=ep_air_ep,
                                last_viewed_at=ep_last_viewed_at,
                                size=ep_size_ep,
                                path=ep_path,
                                jellyfin_episode_id=ep_item_id
                                if self.service_type == Service.JELLYFIN
                                else None,
                                emby_episode_id=ep_item_id
                                if self.service_type == Service.EMBY
                                else None,
                            )
                        )

                # media aggregate signals per season
                for source in episode.get("MediaSources", []):
                    media_width = as_int(source.get("Width"))
                    if media_width is not None:
                        season_max_width[sk] = max(
                            season_max_width.get(sk, 0), media_width
                        )
                    media_height = as_int(source.get("Height"))
                    if media_height is not None:
                        season_max_height[sk] = max(
                            season_max_height.get(sk, 0), media_height
                        )
                    streams = source.get("MediaStreams", []) or []
                    for stream in streams:
                        stream_type = str(stream.get("Type", "")).lower()
                        if stream_type == "video":
                            vcodec = stream.get("Codec")
                            if vcodec:
                                vf = normalize_video_codec_family(str(vcodec))
                                if vf:
                                    season_video_families.setdefault(sk, set()).add(vf)
                            width = as_int(stream.get("Width"))
                            if width is not None:
                                season_max_width[sk] = max(
                                    season_max_width.get(sk, 0), width
                                )
                            height = as_int(stream.get("Height"))
                            if height is not None:
                                season_max_height[sk] = max(
                                    season_max_height.get(sk, 0), height
                                )
                            dv_profile = stream.get("DvProfile")
                            video_range_type = str(
                                stream.get("VideoRangeType", "")
                            ).lower()
                            is_hdr_range = (
                                video_range_type.startswith("hdr")
                                or "hlg" in video_range_type
                                or "dovi" in video_range_type
                                or "dolby" in video_range_type
                            )
                            has_dv = (
                                dv_profile is not None or "dovi" in video_range_type
                            )
                            has_hdr = (
                                has_dv or bool(stream.get("IsHdr")) or is_hdr_range
                            )
                            if has_dv:
                                season_has_dv[sk] = True
                            if has_hdr:
                                season_has_hdr[sk] = True
                        elif stream_type == "audio":
                            acodec = stream.get("Codec")
                            if acodec:
                                af = normalize_audio_codec_family(str(acodec))
                                if af:
                                    season_audio_families.setdefault(sk, set()).add(af)
                            alang = stream.get("Language")
                            normalized_alang = normalize_language(alang)
                            if normalized_alang:
                                season_audio_languages.setdefault(sk, set()).add(
                                    normalized_alang
                                )
                            channels = as_int(stream.get("Channels"))
                            if channels is not None:
                                season_max_audio_channels[sk] = max(
                                    season_max_audio_channels.get(sk, 0), channels
                                )
                        elif stream_type == "subtitle":
                            lang = stream.get("Language")
                            normalized_lang = normalize_language(lang)
                            if normalized_lang:
                                season_subtitle_languages.setdefault(sk, set()).add(
                                    normalized_lang
                                )

            total_record_count = int(get_data.get("TotalRecordCount", 0) or 0)
            start_index += len(episodes)
            if start_index >= total_record_count:
                break

        # build AggregatedSeasonData objects
        season_data: dict[tuple[str, int], AggregatedSeasonData] = {}
        for sk, size in season_sizes.items():
            series_id, season_num = sk
            agg_view = season_view_counts.get(sk, 0)
            season_last_viewed_at: datetime | None = season_last_viewed.get(sk)
            season_data[sk] = AggregatedSeasonData(
                service_series_id=series_id,
                season_number=season_num,
                size=size,
                episode_count=season_episode_counts.get(sk, 0),
                view_count=agg_view,
                last_viewed_at=season_last_viewed_at,
                added_at=season_added_at.get(sk),
                air_date=season_air_date.get(sk),
                service_season_id=season_ids.get(sk),
                has_hdr=True if season_has_hdr.get(sk) else None,
                has_dolby_vision=True if season_has_dv.get(sk) else None,
                max_video_width=season_max_width.get(sk),
                max_video_height=season_max_height.get(sk),
                video_codec_families=sorted(season_video_families.get(sk, set()))
                or None,
                audio_codec_families=sorted(season_audio_families.get(sk, set()))
                or None,
                audio_languages=sorted(season_audio_languages.get(sk, set())) or None,
                max_audio_channels=season_max_audio_channels.get(sk),
                subtitle_languages=sorted(season_subtitle_languages.get(sk, set()))
                or None,
                path=season_paths.get(sk),
                episode_paths=season_episode_paths.get(sk) or None,
                episode_data=season_episode_data.get(sk) or [],
            )

        return series_sizes, season_data

    async def get_all_watched_episodes_for_user(
        self, user_id: str
    ) -> dict[str, datetime]:
        """Get all watched episodes for a user and return a map of seriesId -> most recent watch date.

        This is much more efficient than querying each series individually.
        """
        series_watch_dates: dict[str, datetime] = {}

        try:
            # fetch in paginated batches to avoid timeout on large libraries
            start_index = 0
            limit = 500

            while True:
                params = {
                    "userId": user_id,
                    "includeItemTypes": "Episode",
                    "recursive": "true",
                    "Filters": "IsPlayed",
                    "Fields": "SeriesId,UserData,UserDataLastPlayedDate,UserDataPlayCount",
                    "SortBy": "DatePlayed",
                    "SortOrder": "Descending",
                    "StartIndex": str(start_index),
                    "Limit": str(limit),
                }
                get_data = await self._make_request("Items", params=params, timeout=60)
                if not isinstance(get_data, dict):
                    break

                raw_items_data = get_data.get("Items", [])
                if not isinstance(raw_items_data, list) or not raw_items_data:
                    break
                items_data = [item for item in raw_items_data if isinstance(item, dict)]
                if not items_data:
                    break

                # group by series and keep the most recent watch date
                for item in items_data:
                    series_id = item.get("SeriesId")
                    last_played = item.get("UserData", {}).get("LastPlayedDate")

                    if series_id and last_played:
                        watch_date = datetime.fromisoformat(last_played)

                        # keep the most recent date (items are already sorted by DatePlayed descending)
                        if series_id not in series_watch_dates:
                            series_watch_dates[series_id] = watch_date
                        elif watch_date > series_watch_dates[series_id]:
                            series_watch_dates[series_id] = watch_date

                # check if we've fetched all episodes
                total_record_count = int(get_data.get("TotalRecordCount", 0) or 0)
                start_index += len(items_data)
                if start_index >= total_record_count:
                    break

            return series_watch_dates
        except Exception:
            return {}

    @staticmethod
    def _normalized_user_key(user_name: str | None) -> str | None:
        key = str(user_name or "").strip()
        return key or None

    @staticmethod
    def _safe_tmdb_id(provider_ids: Mapping[str, object] | None) -> int | None:
        raw = (provider_ids or {}).get("Tmdb")
        if raw is None:
            return None
        text = str(raw).strip()
        if not text or not text.lstrip("-").isdigit():
            return None
        return int(text)

    @staticmethod
    def _safe_iso_datetime(raw: object) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw))
        except Exception:
            return None

    async def _get_series_tmdb_map_for_user(self, user_id: str) -> dict[str, int]:
        """Return service seriesId -> TMDB map for one user."""
        mapping: dict[str, int] = {}
        start_index = 0
        limit = 500
        while True:
            params = {
                "userId": user_id,
                "includeItemTypes": "Series",
                "recursive": "true",
                "Fields": "ProviderIds",
                "StartIndex": str(start_index),
                "Limit": str(limit),
            }
            data = await self._make_request("Items", params=params, timeout=60)
            if not isinstance(data, dict):
                break
            items = data.get("Items", [])
            if not items:
                break
            for item in items:
                series_id = str(item.get("Id", "")).strip()
                tmdb_id = self._safe_tmdb_id(item.get("ProviderIds", {}))
                if series_id and tmdb_id is not None:
                    mapping[series_id] = tmdb_id
            total_record_count = int(data.get("TotalRecordCount", 0) or 0)
            start_index += len(items)
            if len(items) < limit or (
                total_record_count and start_index >= total_record_count
            ):
                break
        return mapping

    async def get_watched_user_snapshots(
        self,
    ) -> list[tuple[MediaType, int, str, datetime, int | None]]:
        """Return per-user watched snapshots mapped to TMDB IDs."""
        users = await self.get_users()
        if not users:
            return []

        results: dict[tuple[MediaType, int, str], tuple[datetime, int | None]] = {}
        for user in users:
            watch_user_key = self._normalized_user_key(user.name)
            if not watch_user_key:
                continue

            movie_start = 0
            movie_limit = 500
            while True:
                movie_params = {
                    "userId": user.id,
                    "includeItemTypes": "Movie",
                    "recursive": "true",
                    "Filters": "IsPlayed",
                    "Fields": "ProviderIds,UserData,UserDataLastPlayedDate,UserDataPlayCount",
                    "StartIndex": str(movie_start),
                    "Limit": str(movie_limit),
                }
                movie_data = await self._make_request(
                    "Items", params=movie_params, timeout=60
                )
                if not isinstance(movie_data, dict):
                    break
                movie_items = movie_data.get("Items", [])
                if not movie_items:
                    break
                for item in movie_items:
                    tmdb_id = self._safe_tmdb_id(item.get("ProviderIds", {}))
                    if tmdb_id is None:
                        continue
                    user_data = (
                        item.get("UserData", {}) if isinstance(item, dict) else {}
                    )
                    last_played = self._safe_iso_datetime(
                        user_data.get("LastPlayedDate")
                    )
                    if last_played is None:
                        continue
                    play_count_raw = user_data.get("PlayCount", 0)
                    play_count = (
                        int(play_count_raw or 0)
                        if str(play_count_raw).isdigit()
                        else None
                    )
                    key = (MediaType.MOVIE, tmdb_id, watch_user_key)
                    prev = results.get(key)
                    if prev is None or last_played > prev[0]:
                        results[key] = (last_played, play_count)
                movie_total = int(movie_data.get("TotalRecordCount", 0) or 0)
                movie_start += len(movie_items)
                if len(movie_items) < movie_limit or (
                    movie_total and movie_start >= movie_total
                ):
                    break

            series_tmdb_map = await self._get_series_tmdb_map_for_user(user.id)
            watched_series = await self.get_all_watched_episodes_for_user(user.id)
            for series_id, last_watched_at in watched_series.items():
                tmdb_id = series_tmdb_map.get(series_id)
                if tmdb_id is None:
                    continue
                key = (MediaType.SERIES, tmdb_id, watch_user_key)
                prev = results.get(key)
                if prev is None or last_watched_at > prev[0]:
                    results[key] = (last_watched_at, None)

        return [
            (media_type, tmdb_id, user_key, last_watched_at, play_count)
            for (media_type, tmdb_id, user_key), (
                last_watched_at,
                play_count,
            ) in results.items()
        ]

    async def get_aggregated_movies(
        self, included_libraries: list[str] | None = None
    ) -> list[AggregatedMovieData]:
        """Get aggregated movie data across all users with optional library filters."""
        movie_data: dict[str, _MovieAggregate] = {}
        collection_names_by_item_id = await self._get_collection_names_by_item_id(
            include_item_types="Movie"
        )

        # get all movie libraries
        all_libraries = await self.get_movie_libraries()

        # filter to included libraries if specified
        if included_libraries:
            libraries_to_query = [
                lib for lib in all_libraries if lib["name"] in included_libraries
            ]
        else:
            libraries_to_query = all_libraries

        for library in libraries_to_query:
            library_id = library["id"]
            library_name = library["name"]
            LOG.debug(f"Processing movie library: {library_name} (ID: {library_id})")

            for user in await self.get_users():
                user_movies = await self.get_movies_for_user(
                    user.id,
                    library_id=library_id,
                    library_name=library_name,
                    collection_names_by_item_id=collection_names_by_item_id,
                )

                for movie in user_movies:
                    if movie.external_ids is None:
                        continue
                    if movie.id not in movie_data:
                        # first time seeing this movie - capture versions once (same files for all users)
                        movie_data[movie.id] = _MovieAggregate(
                            name=movie.name,
                            year=movie.year,
                            external_ids=movie.external_ids,
                            versions=list(movie.versions),
                            view_count=(
                                movie.user_data.play_count if movie.user_data else 0
                            ),
                            last_viewed_at=(
                                movie.user_data.last_played_date
                                if movie.user_data
                                else None
                            ),
                            played_by_user_count=(
                                1 if (movie.user_data and movie.user_data.played) else 0
                            ),
                        )
                    else:
                        # aggregate data
                        existing = movie_data[movie.id]
                        if movie.user_data:
                            existing.view_count += movie.user_data.play_count
                            if movie.user_data.last_played_date:
                                if (
                                    existing.last_viewed_at is None
                                    or movie.user_data.last_played_date
                                    > existing.last_viewed_at
                                ):
                                    existing.last_viewed_at = (
                                        movie.user_data.last_played_date
                                    )
                            if movie.user_data.played:
                                existing.played_by_user_count += 1

        # convert to final format
        return [
            AggregatedMovieData(
                name=data.name,
                year=data.year,
                external_ids=data.external_ids,
                versions=data.versions,
                view_count=data.view_count,
                last_viewed_at=data.last_viewed_at,
                played_by_user_count=data.played_by_user_count,
            )
            for data in movie_data.values()
        ]

    async def get_aggregated_series(
        self, included_libraries: list[str] | None = None
    ) -> list[AggregatedSeriesData]:
        """Get aggregated series data across all users with optional library inclusion.

        Note: Emby/Jellyfin doesn't populate LastPlayedDate at the series level, so we check
        episodes to get accurate watch dates. We optimize by getting all watched episodes
        per user in a single API call instead of querying each series individually.
        """
        series_data: dict[str, _SeriesAggregate] = {}
        collection_names_by_item_id = await self._get_collection_names_by_item_id(
            include_item_types="Series"
        )

        # get all TV libraries
        all_libraries = await self.get_series_libraries()

        # filter to included libraries if specified
        if included_libraries:
            libraries_to_query = [
                lib for lib in all_libraries if lib["name"] in included_libraries
            ]
        else:
            libraries_to_query = all_libraries

        for library in libraries_to_query:
            library_id = library["id"]
            library_name = library["name"]
            LOG.debug(f"Processing series library: {library_name} (ID: {library_id})")

            # get first user to fetch series sizes (sizes are same for all users)
            users = await self.get_users()
            if not users:
                continue

            # fetch series sizes once per library (not per user as this is expensive)
            (
                series_sizes,
                season_data_map,
            ) = await self.get_series_sizes_for_library(library_id, users[0].id)

            for user in users:
                # get all watched episodes for this user in one API call
                user_series_watch_dates = await self.get_all_watched_episodes_for_user(
                    user.id
                )

                # get series list for this user from this library
                user_series = await self.get_series_for_user(
                    user.id,
                    library_id=library_id,
                    library_name=library_name,
                    series_sizes=series_sizes,
                    collection_names_by_item_id=collection_names_by_item_id,
                )

                for series in user_series:
                    if series.external_ids is None:
                        continue
                    # get watch date from our pre-fetched data
                    episode_last_watched = user_series_watch_dates.get(series.id)

                    if series.id not in series_data:
                        # first time seeing this series
                        series_data[series.id] = _SeriesAggregate(
                            id=series.id,
                            name=series.name,
                            year=series.year,
                            service=self.service_type,
                            library_id=series.library_id,
                            library_name=series.library_name,
                            path=series.path,
                            added_at=series.date_created,
                            external_ids=series.external_ids,
                            size=series.size,
                            view_count=(
                                series.user_data.play_count if series.user_data else 0
                            ),
                            last_viewed_at=episode_last_watched,
                            played_by_user_count=1 if episode_last_watched else 0,
                            media_server_collection_names=(
                                series.media_server_collection_names
                            ),
                            season_data=[
                                v
                                for k, v in season_data_map.items()
                                if k[0] == series.id
                            ],
                        )
                    else:
                        # aggregate data
                        existing = series_data[series.id]
                        if series.user_data:
                            existing.view_count += series.user_data.play_count

                        # update last_viewed_at if this user's episodes were watched more recently
                        if episode_last_watched:
                            if (
                                existing.last_viewed_at is None
                                or episode_last_watched > existing.last_viewed_at
                            ):
                                existing.last_viewed_at = episode_last_watched
                            existing.played_by_user_count += 1

        # convert to final format
        return [
            AggregatedSeriesData(
                id=data.id,
                name=data.name,
                year=data.year,
                service=data.service,
                library_id=data.library_id,
                library_name=data.library_name,
                path=data.path,
                added_at=data.added_at,
                external_ids=data.external_ids,
                size=data.size,
                view_count=data.view_count,
                last_viewed_at=data.last_viewed_at,
                played_by_user_count=data.played_by_user_count,
                media_server_collection_names=data.media_server_collection_names,
                season_data=data.season_data,
            )
            for data in series_data.values()
        ]

    async def has_playback_reporting_plugin(self) -> bool:
        """Return True if the Jellyfin Playback Reporting plugin is installed and active."""
        try:
            plugins = await self._make_request("Plugins")
            STATUSES_TO_EXCLUDE = {
                "Disabled",
                "Deleted",
                "NotSupported",
                "Malfunctioned",
            }
            PLUGINS_TO_INCLUDE = {
                "playback_reporting.xml",
                "Jellyfin.Plugin.PlaybackReporting.xml",
            }
        except Exception:
            return False
        if not isinstance(plugins, list):
            return False
        for plugin in plugins:
            cfg = plugin.get("ConfigurationFileName", "")
            status = plugin.get("Status", "")
            if cfg in PLUGINS_TO_INCLUDE and status not in STATUSES_TO_EXCLUDE:
                return True
        return False

    async def get_playback_reporting_stats(
        self, min_play_duration: int, media_type: Literal["Movie", "Episode"]
    ) -> dict[str, int]:
        """Query the Playback Reporting plugin for per movie play counts.

        Filters by minimum play duration to exclude brief scrubs.

        Args:
            min_play_duration: Minimum play duration in seconds to count as a play.
            media_type: Movie or Episode.

        Returns:
            Mapping of Jellyfin/Emby item ID to play count.
        """
        query = (
            "SELECT ItemId, COUNT(*) AS total_plays FROM PlaybackActivity "
            f"WHERE ItemType = '{media_type}' AND PlayDuration >= {min_play_duration} "
            "GROUP BY ItemId"
        )
        return await self._submit_playback_custom_query(query)

    async def _submit_playback_custom_query(self, query: RawSQL) -> dict[str, int]:
        # cSpell: disable
        """Submit a raw SQL query to the Playback Reporting plugin endpoint.

        Note: The plugin response uses the key ``colums`` (not ``columns``), this is
        a known typo in the upstream plugin source and is handled intentionally here.

        Args:
            query: Raw SQL string to execute against the plugin's PlaybackActivity table.

        Returns:
            Mapping of ItemId to total_plays derived from the query result set.
        """
        try:
            response = await self.session.post(
                f"{self.service_url}/user_usage_stats/submit_custom_query",
                json={"CustomQueryString": query, "ReplaceUserId": False},
            )
            response.raise_for_status()
            data: dict[str, object] = response.json()
        except Exception as e:
            LOG.error(f"Playback Reporting plugin query failed: {e}")
            return {}

        # "colums" is an intentional typo in the plugin source
        raw_columns = data.get("colums")
        # in case some plugin builds use the correctly spelled key, we handle that too
        if raw_columns is None:
            raw_columns = data.get("columns")
        columns = (
            [str(column) for column in raw_columns]
            if isinstance(raw_columns, list)
            else []
        )
        # cSpell: enable
        raw_results = data.get("results", [])
        results: list[Sequence[object]] = (
            [row for row in raw_results if isinstance(row, (list, tuple))]
            if isinstance(raw_results, list)
            else []
        )

        # no row queries can legitimately return empty columns and results
        if not columns and not results:
            message = data.get("message")
            if message:
                LOG.debug(
                    f"Playback Reporting plugin query returned no rows: {message}"
                )
            return {}

        try:
            item_id_idx = columns.index("ItemId")
            play_count_idx = columns.index("total_plays")
        except ValueError:
            LOG.error(
                f"Unexpected column schema from Playback Reporting plugin: {columns}"
            )
            return {}

        parsed: dict[str, int] = {}
        for row in results:
            if len(row) <= max(item_id_idx, play_count_idx):
                continue
            item_id = row[item_id_idx]
            if not item_id:
                continue
            try:
                parsed[str(item_id)] = int(str(row[play_count_idx]))
            except (TypeError, ValueError):
                continue
        return parsed

    async def get_series_ids_for_episode_ids(
        self, episode_item_ids: list[str]
    ) -> dict[str, str]:
        """Batch-resolve Jellyfin episode item IDs to their parent series item IDs.

        Args:
            episode_item_ids: List of Jellyfin episode item ID strings.

        Returns:
            Mapping of episode item ID to series item ID.
        """
        parents = await self.get_parent_ids_for_episode_ids(episode_item_ids)
        return {
            episode_id: series_id
            for episode_id, (series_id, _) in parents.items()
            if series_id
        }

    async def get_parent_ids_for_episode_ids(
        self, episode_item_ids: list[str]
    ) -> dict[str, tuple[str | None, str | None]]:
        """Batch-resolve episode item IDs to parent series and season IDs."""
        if not episode_item_ids:
            return {}

        try:
            users = await self.get_users()
            if not users:
                return {}
        except Exception as e:
            LOG.error(f"Failed to fetch users for episode→series mapping: {e}")
            return {}

        _CHUNK = 200
        unresolved_ids: set[str] = set(episode_item_ids)
        mapping: dict[str, tuple[str | None, str | None]] = {}
        for user in users:
            if not unresolved_ids:
                break

            candidate_ids = list(unresolved_ids)
            for i in range(0, len(candidate_ids), _CHUNK):
                chunk = candidate_ids[i : i + _CHUNK]
                try:
                    data = await self._make_request(
                        f"Users/{user.id}/Items",
                        params={
                            "Ids": ",".join(chunk),
                            "Fields": "SeriesId,SeasonId",
                            "EnableUserData": "false",
                            "Recursive": "true",
                        },
                    )
                    items = data.get("Items", []) if isinstance(data, dict) else []
                    for item in items:
                        item_id = item.get("Id")
                        series_id = item.get("SeriesId")
                        season_id = item.get("SeasonId")
                        if item_id and (series_id or season_id):
                            mapping[item_id] = (series_id, season_id)
                            unresolved_ids.discard(item_id)
                except Exception as e:
                    LOG.warning(
                        "Failed to resolve episode IDs chunk to series IDs "
                        f"for user {user.id}: {e}"
                    )

        # fallback to non user scoped lookup for servers where user visibility
        # prevents `Users/{id}/Items` from returning complete parent metadata.
        if unresolved_ids:
            candidate_ids = list(unresolved_ids)
            for i in range(0, len(candidate_ids), _CHUNK):
                chunk = candidate_ids[i : i + _CHUNK]
                try:
                    data = await self._make_request(
                        "Items",
                        params={
                            "Ids": ",".join(chunk),
                            "Fields": "SeriesId,SeasonId",
                            "Recursive": "true",
                        },
                    )
                    items = data.get("Items", []) if isinstance(data, dict) else []
                    for item in items:
                        item_id = item.get("Id")
                        series_id = item.get("SeriesId")
                        season_id = item.get("SeasonId")
                        if item_id and (series_id or season_id):
                            mapping[item_id] = (series_id, season_id)
                            unresolved_ids.discard(item_id)
                except Exception as e:
                    LOG.warning(
                        "Failed non user fallback episode parent lookup "
                        f"in {self.service_type}: {e}"
                    )

        if unresolved_ids:
            LOG.debug(
                "Unable to resolve parent IDs for "
                f"{len(unresolved_ids)} episode IDs in {self.service_type}"
            )

        return mapping

    @staticmethod
    def _media_streams_by_type(
        media_source: JsonDict,
    ) -> tuple[JsonList, JsonList, JsonList]:
        streams_value = media_source.get("MediaStreams", [])
        streams: JsonList = (
            [stream for stream in streams_value if isinstance(stream, dict)]
            if isinstance(streams_value, list)
            else []
        )
        video = [s for s in streams if str(s.get("Type", "")).lower() == "video"]
        audio = [s for s in streams if str(s.get("Type", "")).lower() == "audio"]
        subtitle = [s for s in streams if str(s.get("Type", "")).lower() == "subtitle"]
        return video, audio, subtitle

    @staticmethod
    def _unique_languages(streams: JsonList) -> list[str] | None:
        return normalize_languages([stream.get("Language") for stream in streams])

    @staticmethod
    def _is_hdr(stream: JsonDict) -> bool:
        # bit depth (if less than 10 bits it's not hdr)
        try:
            if int(stream["BitDepth"]) < 10:
                return False
        except Exception:
            pass
        # dolby Vision
        if stream.get("DvProfile") or stream.get("DvLevel"):
            return True
        # HDR10/HLG transfer functions
        if str(stream.get("ColorTransfer", "")).lower() in (
            "smpte2084",
            "arib-std-b67",
        ):
            return True
        # BT.2020 color primaries (common for HDR)
        if str(stream.get("ColorSpace", "")).lower() in (
            "bt2020",
            "bt.2020",
            "bt2020nc",
        ):
            return True
        # title contains "hdr"
        if "hdr" in str(stream.get("DisplayTitle", "")).lower():
            return True
        return False

    @staticmethod
    async def test_service(url: str, api_key: str) -> bool:
        """Test Emby/Jellyfin service connection without full initialization."""
        async with niquests.AsyncSession() as session:
            response = await session.get(
                f"{url.rstrip('/')}/System/Info",
                headers={"X-Emby-Token": api_key},
            )
            response.raise_for_status()
            if response.status_code == 200:
                return True
            raise ValueError(f"Unexpected status code: {response.status_code}")
