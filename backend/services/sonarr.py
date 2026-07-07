from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import niquests
from niquests.exceptions import ReadTimeout
from tenacity import (
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from backend.core.utils.misc import as_int
from backend.core.utils.request import format_http_failure, should_retry_on_status
from backend.models.media import ArrTag
from backend.models.services.sonarr import SonarrSeason, SonarrSeries


@dataclass(slots=True, frozen=True)
class SonarrEpisodeSyncData:
    """Episode inventory and file dates returned by one Sonarr request."""

    file_dates: dict[tuple[int, int], datetime]
    episode_numbers_by_season: dict[int, set[int]]


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _as_int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    values: list[int] = []
    for item in value:
        parsed = as_int(item)
        if parsed is not None:
            values.append(parsed)
    return values


def _as_datetime(value: object) -> datetime | None:
    raw = _as_optional_str(value)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    items: list[Mapping[str, object]] = []
    for item in value:
        if isinstance(item, Mapping):
            items.append(item)
    return items


def _season_from_mapping(data: Mapping[str, object]) -> SonarrSeason:
    statistics = data.get("statistics")
    return SonarrSeason(
        season_number=as_int(data.get("seasonNumber")) or 0,
        monitored=_as_bool(data.get("monitored")),
        statistics=dict(statistics) if isinstance(statistics, Mapping) else None,
    )


def build_sonarr_series_from_dict(data: Mapping[str, object]) -> SonarrSeries:
    """Build SonarrSeries from API response dict."""
    seasons = [
        _season_from_mapping(season) for season in _mapping_list(data.get("seasons"))
    ]

    return SonarrSeries(
        id=as_int(data.get("id")) or 0,
        title=_as_optional_str(data.get("title")) or "",
        tvdb_id=as_int(data.get("tvdbId")),
        tmdb_id=as_int(data.get("tmdbId")),
        imdb_id=_as_optional_str(data.get("imdbId")),
        year=as_int(data.get("year")),
        path=_as_optional_str(data.get("path")) or "",
        monitored=_as_bool(data.get("monitored")),
        status=(
            raw_status.lower()
            if (raw_status := _as_optional_str(data.get("status")))
            else None
        ),
        season_count=len(seasons),
        seasons=seasons,
        tags=_as_int_list(data.get("tags")),
        raw=data,
    )


class SonarrClient:
    """Client for Sonarr API operations."""

    __slots__ = ("api_key", "base_url", "timeout", "session")

    def __init__(self, api_key: str, base_url: str, timeout: int = 300) -> None:
        """Initialize Sonarr client.

        Args:
            api_key: Sonarr API key
            base_url: Sonarr server URL
            timeout: Request timeout in seconds (default: 300)
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        self.session = niquests.AsyncSession()
        self.session.headers.update(
            {
                "X-Api-Key": self.api_key,
                "Content-Type": "application/json",
            }
        )

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=(
            retry_if_exception_type((ConnectionError, TimeoutError, ReadTimeout))
            | retry_if_exception(should_retry_on_status)
        ),
    )
    async def _make_request(
        self,
        method: str,
        endpoint: str,
        *,
        error_context: str | None = None,
        **kwargs: Any,
    ) -> tuple[int, Mapping[str, object] | list[Mapping[str, object]] | None]:
        """Make HTTP request to Sonarr API with automatic retry.

        Returns:
            Tuple of (status_code, response_data)
        """
        url = f"{self.base_url}/api/v3/{endpoint}"
        response = await self.session.request(method, url, **kwargs)
        try:
            response.raise_for_status()
        except niquests.HTTPError as exc:
            if error_context:
                raise ValueError(
                    format_http_failure(
                        action=error_context,
                        exception=exc,
                        response=response,
                        method=method,
                        endpoint=endpoint,
                    )
                ) from exc
            raise

        status_code = response.status_code
        if not status_code:
            raise ValueError("Status code should not be None")

        if response.content:
            return status_code, response.json()
        return status_code, None

    async def health(self) -> bool:
        """Check server health and API key."""
        try:
            await self._make_request("GET", "health")
            return True
        except Exception:
            return False

    async def get_series(self, series_id: int) -> SonarrSeries:
        """Get series by ID.

        Args:
            series_id: Series ID

        Returns:
            Series object
        """
        status_code, data = await self._make_request("GET", f"series/{series_id}")
        if not isinstance(data, Mapping):
            raise ValueError(
                f"Invalid response for series {series_id} (status: {status_code})"
            )
        return build_sonarr_series_from_dict(data)

    async def get_all_series(self) -> list[SonarrSeries]:
        """Get all series from Sonarr.

        Returns:
            List of all series
        """
        _, data = await self._make_request("GET", "series", timeout=self.timeout)
        if not isinstance(data, list):
            return []
        return [
            build_sonarr_series_from_dict(series)
            for series in data
            if isinstance(series, Mapping)
        ]

    async def get_disk_space(self) -> list[dict[str, int | str]]:
        """Get disk space stats from Sonarr (GET /diskspace).

        Returns a list of dicts with keys: path, free_space, total_space.
        These are reported by the Sonarr server itself, so they work correctly
        regardless of where Reclaimerr is running (Docker, remote machine, etc.).
        """
        _, data = await self._make_request("GET", "diskspace")
        if not isinstance(data, list):
            return []
        disk_space: list[dict[str, int | str]] = []
        for entry in data:
            if not isinstance(entry, Mapping):
                continue
            path = _as_optional_str(entry.get("path"))
            if path is None:
                continue
            disk_space.append(
                {
                    "path": path,
                    "free_space": as_int(entry.get("freeSpace")) or 0,
                    "total_space": as_int(entry.get("totalSpace")) or 0,
                }
            )
        return disk_space

    async def get_tags(self) -> list[ArrTag]:
        """Get all tags from Sonarr.

        Returns:
            List of all tags
        """
        _, data = await self._make_request("GET", "tag", timeout=60)
        if not isinstance(data, list):
            return []
        tags: list[ArrTag] = []
        for tag in data:
            if not isinstance(tag, Mapping):
                continue
            tag_id = as_int(tag.get("id"))
            label = _as_optional_str(tag.get("label"))
            if tag_id is None or label is None:
                continue
            tags.append(ArrTag(id=tag_id, label=label))
        return tags

    async def get_all_tag_details(self) -> dict[str, list[int]]:
        """Get all tags with their associated series IDs in a single call.

        Returns a dict mapping lowercase tag label → list of Sonarr series IDs.
        """
        _, data = await self._make_request("GET", "tag/detail", timeout=60)
        if not isinstance(data, list):
            return {}
        tag_details: dict[str, list[int]] = {}
        for tag in data:
            if not isinstance(tag, Mapping):
                continue
            label = _as_optional_str(tag.get("label"))
            if label is None:
                continue
            tag_details[label.lower()] = _as_int_list(tag.get("seriesIds"))
        return tag_details

    async def create_tag(self, label: str) -> ArrTag:
        """Create a new tag.

        Args:
            label: Tag name

        Returns:
            Created tag
        """
        status_code, data = await self._make_request(
            "POST", "tag", json={"label": label}
        )
        if not isinstance(data, Mapping):
            raise ValueError(
                f"Invalid response when creating tag {label} (status: {status_code})"
            )
        tag_id = as_int(data.get("id"))
        tag_label = _as_optional_str(data.get("label"))
        if tag_id is None or tag_label is None:
            raise ValueError(
                f"Invalid response when creating tag {label} (status: {status_code})"
            )
        return ArrTag(id=tag_id, label=tag_label)

    async def get_or_create_tag(self, label: str) -> ArrTag:
        """Get existing tag by label or create if doesn't exist.

        Args:
            label: Tag name

        Returns:
            Tag object
        """
        tags = await self.get_tags()
        for tag in tags:
            if tag.label.lower() == label.lower():
                return tag
        return await self.create_tag(label)

    async def add_tag_to_series(
        self, series_ids: list[int], tag_id: int
    ) -> list[SonarrSeries]:
        """Add a tag to multiple series (preserves existing tags).

        Args:
            series_ids: List of Series IDs
            tag_id: Tag ID to add

        Returns:
            List of updated series
        """
        if not series_ids:
            return []

        status_code, data = await self._make_request(
            "PUT",
            "series/editor",
            json={
                "seriesIds": series_ids,
                "tags": [tag_id],
                "applyTags": "add",
            },
            timeout=60,
        )

        if not isinstance(data, list):
            raise ValueError(
                f"Invalid response updating series {series_ids} (status: {status_code})"
            )

        return [
            build_sonarr_series_from_dict(updated_series) for updated_series in data
        ]

    async def remove_tag_from_series(
        self, series_ids: list[int], tag_id: int
    ) -> list[SonarrSeries]:
        """Remove a tag from multiple series (preserves other tags).

        Args:
            series_ids: List of Series IDs
            tag_id: Tag ID to remove

        Returns:
            List of updated series
        """
        if not series_ids:
            return []

        status_code, data = await self._make_request(
            "PUT",
            "series/editor",
            json={
                "seriesIds": series_ids,
                "tags": [tag_id],
                "applyTags": "remove",
            },
            timeout=60,
        )

        if not isinstance(data, list):
            raise ValueError(
                f"Invalid response removing tag from series {series_ids} (status: {status_code})"
            )

        return [
            build_sonarr_series_from_dict(updated_series) for updated_series in data
        ]

    async def delete_series(
        self,
        series_id: int,
        delete_files: bool = True,
        add_import_exclusion: bool = False,
    ) -> None:
        """Delete a single series.

        Args:
            series_id: Series ID to delete
            delete_files: Whether to delete series files from disk
            add_import_exclusion: Whether to add to import exclusion list
        """
        params = {
            "deleteFiles": str(delete_files).lower(),
            "addImportListExclusion": str(add_import_exclusion).lower(),
        }
        status_code, _ = await self._make_request(
            "DELETE",
            f"series/{series_id}",
            params=params,
            timeout=60,
            error_context=f"Failed to delete series {series_id} via Sonarr",
        )
        if status_code != 200:
            raise ValueError(
                f"Failed to delete series {series_id} (status: {status_code})"
            )

    async def delete_series_bulk(
        self,
        series_ids: list[int],
        delete_files: bool = True,
        add_import_exclusion: bool = False,
    ) -> None:
        """Delete multiple series at once."""
        status_code, _ = await self._make_request(
            "DELETE",
            "series/editor",
            json={
                "seriesIds": series_ids,
                "deleteFiles": delete_files,
                "addImportListExclusion": add_import_exclusion,
            },
            timeout=60,
            error_context=f"Failed to delete series {series_ids} via Sonarr",
        )
        if status_code != 200:
            raise ValueError(
                f"Failed to delete series {series_ids} (status: {status_code})"
            )

    async def unmonitor_series(self, series_ids: list[int]) -> None:
        """Set multiple series to unmonitored so Sonarr won't queue re-downloads."""
        if not series_ids:
            return
        await self._make_request(
            "PUT",
            "series/editor",
            json={"seriesIds": series_ids, "monitored": False},
            timeout=60,
            error_context=f"Failed to unmonitor series {series_ids} via Sonarr",
        )

    async def refresh_series(self, series_ids: list[int]) -> None:
        """Queue a RefreshSeries command so Sonarr re-checks metadata and file status.

        Uses RefreshSeries (not RescanSeries) because RescanSeries only adds newly found
        files; RefreshSeries also removes stale file records when files are deleted.
        Sonarr only accepts a single seriesId per command, so we issue one per series.
        """
        for series_id in series_ids:
            await self._make_request(
                "POST",
                "command",
                json={"name": "RefreshSeries", "seriesId": series_id},
            )

    async def update_season_monitoring(
        self,
        series_id: int,
        season_number: int,
        monitored: bool,
    ) -> SonarrSeries:
        """Update monitoring status for a specific season.

        Args:
            series_id: Series ID
            season_number: Season number
            monitored: Whether to monitor this season

        Returns:
            Updated series
        """
        # get current series data
        status_code, series_data = await self._make_request(
            "GET", f"series/{series_id}"
        )
        if not isinstance(series_data, Mapping):
            raise ValueError(
                f"Invalid response for series {series_id} (status: {status_code})"
            )

        # update the specific season's monitoring status
        series_payload = dict(series_data)
        seasons = series_payload.get("seasons", [])
        if not isinstance(seasons, list):
            raise ValueError(
                f"Invalid response for series {series_id} (status: {status_code})"
            )
        for season in seasons:
            if (
                isinstance(season, dict)
                and as_int(season.get("seasonNumber")) == season_number
            ):
                season["monitored"] = monitored
                break

        # update the series
        status_code, updated_data = await self._make_request(
            "PUT",
            f"series/{series_id}",
            json=series_payload,
        )
        if not isinstance(updated_data, Mapping):
            raise ValueError(
                f"Invalid response updating series {series_id} (status: {status_code})"
            )

        return build_sonarr_series_from_dict(updated_data)

    async def delete_season_files(
        self,
        series_id: int,
        season_number: int,
    ) -> None:
        """Delete all episode files for a specific season.

        Args:
            series_id: Series ID
            season_number: Season number to delete files from
        """
        # get all episodes for this series
        status_code, episodes = await self._make_request(
            "GET", "episode", params={"seriesId": series_id}, timeout=60
        )
        if not isinstance(episodes, list):
            raise ValueError(
                f"Invalid response getting episodes for series {series_id} (status: {status_code})"
            )

        # filter to episodes in the specified season that have files
        episode_file_ids: list[int] = []
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            if as_int(episode.get("seasonNumber")) != season_number:
                continue
            episode_file_id = as_int(episode.get("episodeFileId"))
            if episode_file_id is not None:
                episode_file_ids.append(episode_file_id)

        if not episode_file_ids:
            return

        # delete the episode files in bulk
        status_code, _ = await self._make_request(
            "DELETE",
            "episodefile/bulk",
            json={"episodeFileIds": episode_file_ids},
            timeout=120,
            error_context=(
                f"Failed to delete season {season_number} files for series {series_id} "
                "via Sonarr"
            ),
        )
        if status_code != 200:
            raise ValueError(
                f"Failed to delete season {season_number} files for series {series_id} (status: {status_code})"
            )

    async def get_episodes(
        self, series_id: int, season_number: int | None = None
    ) -> list[dict[str, object]]:
        """Get episodes for a series, optionally limited to one season.

        Args:
            series_id: Sonarr series ID
            season_number: Optional Sonarr season number

        Returns:
            List of episode dicts
        """
        params: dict[str, int] = {"seriesId": series_id}
        if season_number is not None:
            params["seasonNumber"] = season_number
        status_code, data = await self._make_request(
            "GET", "episode", params=params, timeout=60
        )
        if not isinstance(data, list):
            raise ValueError(
                "Invalid response getting episodes for "
                f"series {series_id}, season {season_number} (status: {status_code})"
            )
        return [dict(episode) for episode in data if isinstance(episode, Mapping)]

    async def get_episode_file_dates(
        self, series_id: int
    ) -> dict[tuple[int, int], datetime]:
        """Return Sonarr file-import dates keyed by season and episode number."""

        return (await self.get_episode_sync_data(series_id)).file_dates

    async def get_episode_sync_data(self, series_id: int) -> SonarrEpisodeSyncData:
        """Return Sonarr's canonical episode inventory and file-import dates."""

        status_code, data = await self._make_request(
            "GET",
            "episode",
            params={"seriesId": series_id, "includeEpisodeFile": True},
            timeout=60,
        )
        if not isinstance(data, list):
            raise ValueError(
                f"Invalid episode response for series {series_id} "
                f"(status: {status_code})"
            )

        dates: dict[tuple[int, int], datetime] = {}
        episode_numbers_by_season: dict[int, set[int]] = {}
        for episode in data:
            if not isinstance(episode, Mapping):
                continue
            season_number = as_int(episode.get("seasonNumber"))
            episode_number = as_int(episode.get("episodeNumber"))
            if (
                season_number is None
                or season_number < 0
                or episode_number is None
                or episode_number <= 0
            ):
                continue
            episode_numbers_by_season.setdefault(season_number, set()).add(
                episode_number
            )
            if not _as_bool(episode.get("hasFile")):
                continue
            raw_file = episode.get("episodeFile")
            episode_file = raw_file if isinstance(raw_file, Mapping) else None
            added_at = (
                _as_datetime(episode_file.get("dateAdded"))
                if episode_file is not None
                else None
            )
            if added_at is None:
                continue
            key = (season_number, episode_number)
            current = dates.get(key)
            if current is None or added_at > current:
                dates[key] = added_at
        return SonarrEpisodeSyncData(
            file_dates=dates,
            episode_numbers_by_season=episode_numbers_by_season,
        )

    async def delete_episode_file(self, episode_file_id: int) -> None:
        """Delete a single episode file by its episode file ID.

        Args:
            episode_file_id: Sonarr episode file ID
        """
        status_code, _ = await self._make_request(
            "DELETE",
            f"episodefile/{episode_file_id}",
            timeout=60,
            error_context=(
                f"Failed to delete episode file {episode_file_id} via Sonarr"
            ),
        )
        if status_code not in {200, 204}:
            raise ValueError(
                f"Failed to delete episode file {episode_file_id} (status: {status_code})"
            )

    async def unmonitor_episode(self, episode_id: int) -> None:
        """Set a single episode to unmonitored in Sonarr.

        Args:
            episode_id: Sonarr episode ID
        """
        status_code, _ = await self._make_request(
            "PUT",
            "episode/monitor",
            json={"episodeIds": [episode_id], "monitored": False},
            timeout=60,
            error_context=f"Failed to unmonitor episode {episode_id} via Sonarr",
        )
        if status_code not in {200, 202}:
            raise ValueError(
                f"Failed to unmonitor episode {episode_id} (status: {status_code})"
            )

    @staticmethod
    async def test_service(url: str, api_key: str) -> bool:
        """Test Sonarr service connection without full initialization."""
        async with niquests.AsyncSession() as session:
            response = await session.get(
                f"{url.rstrip('/')}/api/v3/health",
                headers={"X-Api-Key": api_key},
            )
            response.raise_for_status()
            if response.status_code == 200:
                return True
            raise ValueError(f"Unexpected status code: {response.status_code}")
