from __future__ import annotations

from datetime import datetime
from pathlib import PurePath
from typing import Any, Literal

import niquests
from niquests.exceptions import ReadTimeout
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
from backend.core.utils.misc import as_float, as_int
from backend.core.utils.request import should_retry_on_status
from backend.core.utils.resolution import guesstimate_resolution
from backend.enums import Service
from backend.models.media import (
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
        self.service_type = service_type
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
        self, endpoint: str, params: dict | None = None, **kwargs
    ) -> list | dict:
        """Make HTTP request to Emby's/Jellyfin's API with automatic retry."""
        response = await self.session.get(
            f"{self.service_url}/{endpoint}",
            params=params,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

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
                f"Failed to delete {self.service_type} item {item_id}: {e}"
            )

    async def delete_movie_version(self, item_id: str, _media_source_id: str) -> None:
        """Deletes one movie version for Emby/Jellyfin.

        Emby/Jellyfin libraries represented as separate items per version can delete
        a single version by deleting that specific item id.
        """
        await self.delete_item(item_id)

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
        media_libs = []
        for vf in virtual_folders:
            if vf.get("CollectionType") == media_type:
                item_id = vf.get("ItemId")
                name = vf.get("Name")
                if item_id and name:
                    media_libs.append({"id": item_id, "name": name})
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
        if not users_data:
            return []
        return [EmbyUserBase(name=user["Name"], id=user["Id"]) for user in users_data]

    async def get_movies_for_user(
        self,
        user_id: str,
        library_id: str,
        library_name: str,
        filters: dict | None = None,
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
        if not get_data:
            return []
        items_data = get_data.get("Items", [])  # pyright: ignore [reportAttributeAccessIssue]

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
                        service=self.service_type,  # type: ignore[reportArgumentType]
                        service_item_id=item["Id"],
                        service_media_id=source["Id"],
                        library_id=library_id,
                        library_name=library_name,
                        path=source.get("Path"),
                        size=source.get("Size", 0),
                        added_at=added_at,
                        file_name=PurePath(source["Path"]).name
                        if source.get("Path")
                        else source.get("Name"),
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
                        audio_language=(
                            str(first_audio.get("Language")).lower()
                            if first_audio.get("Language")
                            else None
                        ),
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
            )
            data.append(movie)
        return data

    async def get_series_for_user(
        self,
        user_id: str,
        library_id: str,
        library_name: str,
        series_sizes: dict[str, int] | None = None,
        filters: dict | None = None,
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
        if not get_data:
            return []
        items_data = get_data.get("Items", [])  # pyright: ignore [reportAttributeAccessIssue]

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
                path=item.get("Path"),
                external_ids=external_ids,
                size=total_size,
                user_data=user_data,
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
            if not get_data:
                break

            episodes = get_data.get("Items", [])  # pyright: ignore [reportAttributeAccessIssue]
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
                            season_paths[sk] = normalize_fpath(
                                PurePath(_ep_path).parent
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
                if play_count > 0 and user_data.get("LastPlayedDate"):
                    try:
                        lva = datetime.fromisoformat(user_data["LastPlayedDate"])
                        prev = season_last_viewed.get(sk)
                        if prev is None or lva > prev:
                            season_last_viewed[sk] = lva
                    except (TypeError, ValueError):
                        pass
                elif sk not in season_last_viewed:
                    season_last_viewed[sk] = None

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
                            if alang:
                                season_audio_languages.setdefault(sk, set()).add(
                                    str(alang).lower()
                                )
                            channels = as_int(stream.get("Channels"))
                            if channels is not None:
                                season_max_audio_channels[sk] = max(
                                    season_max_audio_channels.get(sk, 0), channels
                                )
                        elif stream_type == "subtitle":
                            lang = stream.get("Language")
                            if lang:
                                season_subtitle_languages.setdefault(sk, set()).add(
                                    str(lang).lower()
                                )

            total_record_count = get_data.get("TotalRecordCount", 0)  # pyright: ignore [reportAttributeAccessIssue]
            start_index += len(episodes)
            if start_index >= total_record_count:
                break

        # build AggregatedSeasonData objects
        season_data: dict[tuple[str, int], AggregatedSeasonData] = {}
        for sk, size in season_sizes.items():
            series_id, season_num = sk
            agg_view = season_view_counts.get(sk, 0)
            lva = season_last_viewed.get(sk)
            season_data[sk] = AggregatedSeasonData(
                service_series_id=series_id,
                season_number=season_num,
                size=size,
                episode_count=season_episode_counts.get(sk, 0),
                view_count=agg_view,
                last_viewed_at=lva,
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
                if not get_data:
                    break

                items_data = get_data.get("Items", [])  # pyright: ignore [reportAttributeAccessIssue]
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
                total_record_count = get_data.get("TotalRecordCount", 0)  # pyright: ignore [reportAttributeAccessIssue]
                start_index += len(items_data)
                if start_index >= total_record_count:
                    break

            return series_watch_dates
        except Exception:
            return {}

    async def get_aggregated_movies(
        self, included_libraries: list[str] | None = None
    ) -> list[AggregatedMovieData]:
        """Get aggregated movie data across all users with optional library filters."""
        movie_data: dict[str, dict[str, Any]] = {}

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
                    user.id, library_id=library_id, library_name=library_name
                )

                for movie in user_movies:
                    if movie.id not in movie_data:
                        # first time seeing this movie - capture versions once (same files for all users)
                        movie_data[movie.id] = {
                            "name": movie.name,
                            "year": movie.year,
                            "external_ids": movie.external_ids,
                            "versions": list(movie.versions),
                            "view_count": movie.user_data.play_count
                            if movie.user_data
                            else 0,
                            "last_viewed_at": movie.user_data.last_played_date
                            if movie.user_data
                            else None,
                            "played_by_user_count": 1
                            if (movie.user_data and movie.user_data.played)
                            else 0,
                        }
                    else:
                        # aggregate data
                        existing = movie_data[movie.id]
                        if movie.user_data:
                            existing["view_count"] += movie.user_data.play_count
                            if movie.user_data.last_played_date:
                                if (
                                    not existing["last_viewed_at"]
                                    or movie.user_data.last_played_date
                                    > existing["last_viewed_at"]
                                ):
                                    existing["last_viewed_at"] = (
                                        movie.user_data.last_played_date
                                    )
                            if movie.user_data.played:
                                existing["played_by_user_count"] += 1

        # convert to final format
        return [
            AggregatedMovieData(
                name=data["name"],
                year=data["year"],
                external_ids=data["external_ids"],
                versions=data["versions"],
                view_count=data["view_count"],
                last_viewed_at=data["last_viewed_at"],
                played_by_user_count=data["played_by_user_count"],
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
        series_data: dict[str, dict[str, Any]] = {}

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
                )

                for series in user_series:
                    # get watch date from our pre-fetched data
                    episode_last_watched = user_series_watch_dates.get(series.id)

                    if series.id not in series_data:
                        # first time seeing this series
                        series_data[series.id] = {
                            "id": series.id,
                            "name": series.name,
                            "year": series.year,
                            "service": self.service_type,
                            "library_id": series.library_id,
                            "library_name": series.library_name,
                            "path": series.path,
                            "added_at": series.date_created,
                            "external_ids": series.external_ids,
                            "size": series.size,
                            "view_count": series.user_data.play_count
                            if series.user_data
                            else 0,
                            "last_viewed_at": episode_last_watched,
                            "played_by_user_count": 1 if episode_last_watched else 0,
                            "season_data": [
                                v
                                for k, v in season_data_map.items()
                                if k[0] == series.id
                            ],
                        }
                    else:
                        # aggregate data
                        existing = series_data[series.id]
                        if series.user_data:
                            existing["view_count"] += series.user_data.play_count

                        # update last_viewed_at if this user's episodes were watched more recently
                        if episode_last_watched:
                            if (
                                not existing["last_viewed_at"]
                                or episode_last_watched > existing["last_viewed_at"]
                            ):
                                existing["last_viewed_at"] = episode_last_watched
                            existing["played_by_user_count"] += 1

        # convert to final format
        return [
            AggregatedSeriesData(
                **{k: v for k, v in data.items() if k != "season_data"},
                season_data=data.get("season_data", []),
            )
            for data in series_data.values()
        ]

    @staticmethod
    def _media_streams_by_type(
        media_source: dict[str, Any],
    ) -> tuple[list[dict], list[dict], list[dict]]:
        streams = media_source.get("MediaStreams", []) or []
        video = [s for s in streams if str(s.get("Type", "")).lower() == "video"]
        audio = [s for s in streams if str(s.get("Type", "")).lower() == "audio"]
        subtitle = [s for s in streams if str(s.get("Type", "")).lower() == "subtitle"]
        return video, audio, subtitle

    @staticmethod
    def _unique_languages(streams: list[dict]) -> list[str] | None:
        langs: list[str] = []
        seen: set[str] = set()
        for stream in streams:
            raw = stream.get("Language")
            if not raw:
                continue
            value = str(raw).strip().lower()
            if not value or value in seen:
                continue
            seen.add(value)
            langs.append(value)
        return langs or None

    @staticmethod
    def _is_hdr(stream: dict) -> bool:
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
