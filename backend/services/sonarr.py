from __future__ import annotations

import niquests
from niquests.exceptions import ReadTimeout
from tenacity import (
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from backend.core.utils.request import should_retry_on_status
from backend.models.media import ArrTag
from backend.models.services.sonarr import SonarrSeason, SonarrSeries


def build_sonarr_series_from_dict(data: dict) -> SonarrSeries:
    """Build SonarrSeries from API response dict."""
    seasons_data = data.get("seasons", [])
    seasons = [
        SonarrSeason(
            season_number=s.get("seasonNumber", 0),
            monitored=s.get("monitored", False),
            statistics=s.get("statistics"),
        )
        for s in seasons_data
    ]

    return SonarrSeries(
        id=data["id"],
        title=data.get("title", ""),
        tvdb_id=data.get("tvdbId"),
        tmdb_id=data.get("tmdbId"),
        imdb_id=data.get("imdbId"),
        year=data.get("year"),
        path=data.get("path", ""),
        monitored=data.get("monitored", False),
        season_count=len(seasons),
        seasons=seasons,
        tags=data.get("tags", []),
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
        self, method: str, endpoint: str, **kwargs
    ) -> tuple[int, dict | list | None]:
        """Make HTTP request to Sonarr API with automatic retry.

        Returns:
            Tuple of (status_code, response_data)
        """
        url = f"{self.base_url}/api/v3/{endpoint}"
        response = await self.session.request(method, url, **kwargs)
        response.raise_for_status()

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
        if not isinstance(data, dict):
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
        return [build_sonarr_series_from_dict(series) for series in data]

    async def get_tags(self) -> list[ArrTag]:
        """Get all tags from Sonarr.

        Returns:
            List of all tags
        """
        _, data = await self._make_request("GET", "tag", timeout=60)
        if not isinstance(data, list):
            return []
        return [ArrTag(id=tag["id"], label=tag["label"]) for tag in data]

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
        if not isinstance(data, dict):
            raise ValueError(
                f"Invalid response when creating tag {label} (status: {status_code})"
            )
        return ArrTag(id=data["id"], label=data["label"])

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
        """Delete multiple series at once.

        Args:
            series_ids: List of series IDs to delete
            delete_files: Whether to delete series files from disk
            add_import_exclusion: Whether to add to import exclusion list
        """
        status_code, _ = await self._make_request(
            "DELETE",
            "series/editor",
            json={
                "seriesIds": series_ids,
                "deleteFiles": delete_files,
                "addImportListExclusion": add_import_exclusion,
            },
            timeout=60,
        )
        if status_code != 200:
            raise ValueError(
                f"Failed to delete series {series_ids} (status: {status_code})"
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
        if not isinstance(series_data, dict):
            raise ValueError(
                f"Invalid response for series {series_id} (status: {status_code})"
            )

        # update the specific season's monitoring status
        seasons = series_data.get("seasons", [])
        for season in seasons:
            if season.get("seasonNumber") == season_number:
                season["monitored"] = monitored
                break

        # update the series
        status_code, updated_data = await self._make_request(
            "PUT",
            f"series/{series_id}",
            json=series_data,
        )
        if not isinstance(updated_data, dict):
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
        episode_file_ids = [
            ep.get("episodeFileId")
            for ep in episodes
            if ep.get("seasonNumber") == season_number
            and ep.get("episodeFileId") is not None
        ]

        if not episode_file_ids:
            return

        # delete the episode files in bulk
        status_code, _ = await self._make_request(
            "DELETE",
            "episodefile/bulk",
            json={"episodeFileIds": episode_file_ids},
            timeout=120,
        )
        if status_code != 200:
            raise ValueError(
                f"Failed to delete season {season_number} files for series {series_id} (status: {status_code})"
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
