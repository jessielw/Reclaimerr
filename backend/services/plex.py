from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import PurePath
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

from backend.core.codecs import (
    normalize_audio_codec_family,
    normalize_video_codec_family,
)
from backend.core.logger import LOG
from backend.core.tmdb import AsyncTMDBClient
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
from backend.models.services.plex import PlexMovie, PlexSeries

# history tuple (total_view_count, max_last_viewed_at, distinct_user_count)
_HistEntry = tuple[int, datetime | None, int]
_METADATA_BATCH_SIZE = 50
_EPISODE_METADATA_BATCH_SIZE = 100


class PlexService:
    """Plex media server backend."""

    __slots__ = ("token", "plex_url", "session")

    def __init__(self, token: str, plex_url: str) -> None:
        self.token = token
        self.plex_url = plex_url.rstrip("/")

        self.session = niquests.AsyncSession(timeout=300)
        self.session.headers.update(
            {
                "X-Plex-Token": self.token,
                "Accept": "application/json",
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
        self, endpoint: str, params: dict | None = None, **kwargs
    ) -> tuple[dict | list, int | None]:
        """Make HTTP request to Plex API with automatic retry."""
        response = await self.session.get(
            f"{self.plex_url}/{endpoint}", params=params, **kwargs
        )
        response.raise_for_status()
        return response.json(), response.status_code

    @staticmethod
    def _fromtimestamp(ts: Any) -> datetime | None:
        """Safe wrapper around datetime.fromtimestamp (returns None for invalid/out of range values)."""
        if not ts:
            return None
        try:
            return datetime.fromtimestamp(int(ts), tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None

    async def health(self) -> bool:
        """Check server health and API key."""
        try:
            response, status_code = await self._make_request("identity")
            if status_code == 200 and self._check_plex_healthy(response):
                return True
        except Exception:
            pass
        return False

    async def delete_item(self, rating_key: str) -> None:
        """Delete an item (movie or series) from Plex.

        Args:
            rating_key: Plex rating key (item ID)
        """
        try:
            response = await self.session.delete(
                f"{self.plex_url}/library/metadata/{rating_key}"
            )
            response.raise_for_status()
            LOG.debug(f"Deleted Plex item {rating_key}")
        except Exception as e:
            raise ValueError(f"Failed to delete Plex item {rating_key}: {e}")

    async def scan_item_path(self, item_path: str) -> bool:
        """Scan a specific item path in Plex library.

        This triggers Plex to scan the specific path, similar to how Radarr/Sonarr
        notify Plex to update specific movie/series paths after deletion.

        Args:
            item_path: Filesystem path to the item (e.g., '/movies/The Matrix (1999)')

        Returns:
            True if scan was triggered successfully, False otherwise
        """
        try:
            # plex's scan endpoint with path parameter scans the specific path
            # we need to find the section that contains this path first
            sections = await self.get_library_sections()

            # try to find which section this path belongs to by checking location paths
            for section in sections:
                section_id = section.get("key")
                if not section_id:
                    continue

                # get section details to check locations
                section_details, _ = await self._make_request(
                    f"library/sections/{section_id}",
                    timeout=60,
                )
                directories = section_details.get("MediaContainer", {}).get(  # pyright: ignore [reportAttributeAccessIssue]
                    "Directory", []
                )
                if not directories:
                    continue

                locations = (
                    directories[0].get("Location", [])
                    if isinstance(directories, list) and directories
                    else []
                )

                # check if item_path is within any of this section's locations
                for location in locations:
                    location_path = location.get("path", "")
                    if item_path.startswith(location_path):
                        # found the right section, trigger scan with specific path
                        response = await self.session.get(
                            f"{self.plex_url}/library/sections/{section_id}/refresh",
                            params={"path": item_path},
                        )
                        response.raise_for_status()
                        LOG.debug(
                            f"Triggered Plex scan for path: {item_path} in section {section_id}"
                        )
                        return True

            LOG.warning(f"Could not find Plex section for path: {item_path}")
            return False
        except Exception as e:
            LOG.error(f"Failed to scan Plex path {item_path}: {e}")
            return False

    async def _get_media_libraries(self, media_type: str) -> list[dict[str, str]]:
        """Get list of media libraries of a specific type with their IDs and names."""
        virtual_folders, _ = await self._make_request("library/sections/all")  # pyright: ignore [reportAttributeAccessIssue]
        media_libs = []
        for section in virtual_folders.get("MediaContainer", {}).get("Directory", []):  # pyright: ignore [reportAttributeAccessIssue]
            if section.get("type") == media_type:
                item_id = section.get("uuid")
                name = section.get("title")
                if item_id and name:
                    media_libs.append({"id": item_id, "name": name})
        return media_libs

    async def get_movie_libraries(self) -> list[dict[str, str]]:
        """Get list of movie libraries with their IDs and names."""
        return await self._get_media_libraries("movie")

    async def get_series_libraries(self) -> list[dict[str, str]]:
        """Get list of TV series libraries with their IDs and names."""
        return await self._get_media_libraries("show")

    async def get_library_sections(self) -> list[dict]:
        """Get all library sections."""
        data, _ = await self._make_request("library/sections")
        if not isinstance(data, dict):
            return []
        return data.get("MediaContainer", {}).get("Directory", [])  # pyright: ignore [reportAttributeAccessIssue]

    async def get_series_sizes_for_section(self, section_id: str) -> dict[str, int]:
        """Get total sizes for all series in a library section by fetching all episodes in one call.

        Args:
            section_id: The Plex library section ID

        Returns:
            Dictionary mapping series rating key to total size in bytes
        """
        sizes, _, _ = await self._get_episode_data_for_section(section_id)
        return sizes

    async def get_series_paths_for_section(self, section_id: str) -> dict[str, str]:
        """Get paths for all series in a library section by extracting from first episode.

        Args:
            section_id: The Plex library section ID

        Returns:
            Dictionary mapping series rating key to series directory path
        """
        _, paths, _ = await self._get_episode_data_for_section(section_id)
        return paths

    async def _get_episode_data_for_section(
        self, section_id: str
    ) -> tuple[
        dict[str, int], dict[str, str], dict[tuple[str, int], AggregatedSeasonData]
    ]:
        """Fetch all episodes for a section once and extract sizes, paths, and season data.

        Returns:
            (series_sizes, series_paths, season_data)
            - series_sizes: series ratingKey -> total bytes
            - series_paths: series ratingKey -> series dir path
            - season_data: (series ratingKey, season_number) -> AggregatedSeasonData
        """
        episodes_data, _ = await self._make_request(
            f"library/sections/{section_id}/all",
            params={"type": 4},
            timeout=300,
        )
        if not episodes_data:
            return {}, {}, {}

        episodes = episodes_data.get("MediaContainer", {}).get("Metadata", [])  # pyright: ignore [reportAttributeAccessIssue]

        series_sizes: dict[str, int] = {}
        series_paths: dict[str, str] = {}
        # (series_key, season_number) -> accumulated size + episode count
        season_sizes: dict[tuple[str, int], int] = {}
        season_episode_counts: dict[tuple[str, int], int] = {}
        season_keys: dict[tuple[str, int], str] = {}  # season ratingKey
        season_last_viewed: dict[tuple[str, int], datetime | None] = {}
        season_view_counts: dict[tuple[str, int], int] = {}
        season_air_date: dict[tuple[str, int], datetime | None] = {}
        season_added_at: dict[tuple[str, int], datetime | None] = {}
        season_has_hdr: dict[tuple[str, int], bool] = {}
        season_has_dv: dict[tuple[str, int], bool] = {}
        season_max_width: dict[tuple[str, int], int] = {}
        season_max_height: dict[tuple[str, int], int] = {}
        season_video_families: dict[tuple[str, int], set[str]] = {}
        season_audio_families: dict[tuple[str, int], set[str]] = {}
        season_max_audio_channels: dict[tuple[str, int], int] = {}
        season_subtitle_languages: dict[tuple[str, int], set[str]] = {}

        # section-level episode list responses can omit per-stream details.
        # collect only episodes that need enrichment and fetch them in batches.
        needs_detail_keys: list[str] = []
        for episode in episodes:
            ep_key = str(episode.get("ratingKey", ""))
            if not ep_key:
                continue
            medias = episode.get("Media", [])
            if any(
                not (m.get("Part") and m["Part"] and m["Part"][0].get("Stream"))
                for m in medias
            ):
                needs_detail_keys.append(ep_key)
        detailed_episodes = await self._get_episodes_metadata_batch(needs_detail_keys)

        for episode in episodes:
            if not isinstance(episode, dict):
                continue

            source_episode = detailed_episodes.get(str(episode.get("ratingKey", "")))
            if not isinstance(source_episode, dict):
                source_episode = episode
            series_key = episode.get("grandparentRatingKey")
            season_key = episode.get("parentRatingKey")
            season_num_raw = episode.get("parentIndex")
            if not series_key or season_num_raw is None:
                continue
            try:
                season_num = int(season_num_raw)
            except (TypeError, ValueError):
                continue

            # compute episode file size
            episode_size = 0
            for media in source_episode.get("Media", []) or episode.get("Media", []):
                for part in media.get("Part", []):
                    episode_size += part.get("size", 0)

            # accumulate series totals
            series_sizes[series_key] = series_sizes.get(series_key, 0) + episode_size

            # series path (first episode of each series)
            if series_key not in series_paths:
                media_list = source_episode.get("Media", []) or episode.get("Media", [])
                if media_list and media_list[0].get("Part"):
                    ep_file = media_list[0]["Part"][0].get("file")
                    if ep_file:
                        series_paths[series_key] = str(PurePath(ep_file).parent.parent)

            # accumulate season totals
            sk = (series_key, season_num)
            season_sizes[sk] = season_sizes.get(sk, 0) + episode_size
            season_episode_counts[sk] = season_episode_counts.get(sk, 0) + 1
            if season_key and sk not in season_keys:
                season_keys[sk] = season_key

            # season air date = earliest episode air date
            ep_air_date_raw = source_episode.get(
                "originallyAvailableAt"
            ) or episode.get("originallyAvailableAt")
            if ep_air_date_raw:
                try:
                    ep_air_date = datetime.strptime(ep_air_date_raw, "%Y-%m-%d")
                    prev_air = season_air_date.get(sk)
                    if prev_air is None or ep_air_date < prev_air:
                        season_air_date[sk] = ep_air_date
                except (TypeError, ValueError):
                    pass

            # season added_at = earliest episode addedAt
            ep_added_at = self._fromtimestamp(
                source_episode.get("addedAt") or episode.get("addedAt")
            )
            if ep_added_at:
                prev_added = season_added_at.get(sk)
                if prev_added is None or ep_added_at < prev_added:
                    season_added_at[sk] = ep_added_at

            # media aggregate signals per season
            for media in source_episode.get("Media", []) or episode.get("Media", []):
                video_codec_raw = media.get("videoCodec")
                audio_codec_raw = media.get("audioCodec")
                if video_codec_raw:
                    vf = normalize_video_codec_family(str(video_codec_raw))
                    if vf:
                        season_video_families.setdefault(sk, set()).add(vf)
                if audio_codec_raw:
                    af = normalize_audio_codec_family(str(audio_codec_raw))
                    if af:
                        season_audio_families.setdefault(sk, set()).add(af)
                media_audio_channels = as_int(media.get("audioChannels"))
                if media_audio_channels is not None:
                    season_max_audio_channels[sk] = max(
                        season_max_audio_channels.get(sk, 0), media_audio_channels
                    )
                media_width = as_int(media.get("width"))
                if media_width is not None:
                    season_max_width[sk] = max(season_max_width.get(sk, 0), media_width)
                media_height = as_int(media.get("height"))
                if media_height is not None:
                    season_max_height[sk] = max(
                        season_max_height.get(sk, 0), media_height
                    )
                resolution = str(media.get("videoResolution", "")).lower()
                if resolution:
                    if resolution in {"4k", "uhd"}:
                        season_max_width[sk] = max(season_max_width.get(sk, 0), 3840)
                        season_max_height[sk] = max(season_max_height.get(sk, 0), 2160)
                    elif resolution in {"1080", "1080p"}:
                        season_max_width[sk] = max(season_max_width.get(sk, 0), 1920)
                        season_max_height[sk] = max(season_max_height.get(sk, 0), 1080)
                    elif resolution in {"720", "720p"}:
                        season_max_width[sk] = max(season_max_width.get(sk, 0), 1280)
                        season_max_height[sk] = max(season_max_height.get(sk, 0), 720)
                    elif resolution in {"576", "576p"}:
                        season_max_width[sk] = max(season_max_width.get(sk, 0), 1024)
                        season_max_height[sk] = max(season_max_height.get(sk, 0), 576)
                    elif resolution in {"480", "480p"}:
                        season_max_width[sk] = max(season_max_width.get(sk, 0), 720)
                        season_max_height[sk] = max(season_max_height.get(sk, 0), 480)

                for part in media.get("Part", []):
                    for stream in part.get("Stream", []):
                        stream_type = str(stream.get("streamType", ""))
                        if stream_type == "1":
                            vcodec = stream.get("codec") or stream.get("codecID")
                            if vcodec:
                                vf = normalize_video_codec_family(str(vcodec))
                                if vf:
                                    season_video_families.setdefault(sk, set()).add(vf)
                            width = as_int(stream.get("width"))
                            if width is not None:
                                season_max_width[sk] = max(
                                    season_max_width.get(sk, 0), width
                                )
                            height = as_int(stream.get("height"))
                            if height is not None:
                                season_max_height[sk] = max(
                                    season_max_height.get(sk, 0), height
                                )
                            color_trc = str(stream.get("colorTrc", "")).lower()
                            extended_title = str(
                                stream.get("extendedDisplayTitle", "")
                            ).lower()
                            has_dv = bool(stream.get("DOVIPresent"))
                            has_hdr = has_dv or (
                                "hdr" in extended_title
                                or color_trc in {"smpte2084", "arib-std-b67"}
                            )
                            if has_dv:
                                season_has_dv[sk] = True
                            if has_hdr:
                                season_has_hdr[sk] = True
                        elif stream_type == "2":
                            acodec = stream.get("codec") or stream.get("codecID")
                            if acodec:
                                af = normalize_audio_codec_family(str(acodec))
                                if af:
                                    season_audio_families.setdefault(sk, set()).add(af)
                            channels = as_int(stream.get("channels"))
                            if channels is not None:
                                season_max_audio_channels[sk] = max(
                                    season_max_audio_channels.get(sk, 0), channels
                                )
                        elif stream_type == "3":
                            lang = (
                                stream.get("languageCode")
                                or stream.get("languageTag")
                                or stream.get("language")
                            )
                            if lang:
                                season_subtitle_languages.setdefault(sk, set()).add(
                                    str(lang).lower()
                                )

            # watch data per season
            ep_view_count = episode.get("viewCount", 0) or 0
            season_view_counts[sk] = season_view_counts.get(sk, 0) + ep_view_count
            if episode.get("lastViewedAt"):
                try:
                    lva = datetime.fromtimestamp(int(episode["lastViewedAt"]), tz=UTC)
                    prev = season_last_viewed.get(sk)
                    if prev is None or lva > prev:
                        season_last_viewed[sk] = lva
                except (TypeError, ValueError, OSError):
                    if sk not in season_last_viewed:
                        season_last_viewed[sk] = None
            elif sk not in season_last_viewed:
                season_last_viewed[sk] = None

        # build AggregatedSeasonData objects
        season_data: dict[tuple[str, int], AggregatedSeasonData] = {}
        for sk, size in season_sizes.items():
            series_key, season_num = sk
            agg_view = season_view_counts.get(sk, 0)
            lva = season_last_viewed.get(sk)
            season_data[sk] = AggregatedSeasonData(
                service_series_id=series_key,
                season_number=season_num,
                size=size,
                episode_count=season_episode_counts.get(sk, 0),
                view_count=agg_view,
                last_viewed_at=lva,
                added_at=season_added_at.get(sk),
                air_date=season_air_date.get(sk),
                service_season_id=season_keys.get(sk),
                has_hdr=True if season_has_hdr.get(sk) else None,
                has_dolby_vision=True if season_has_dv.get(sk) else None,
                max_video_width=season_max_width.get(sk),
                max_video_height=season_max_height.get(sk),
                video_codec_families=sorted(season_video_families.get(sk, set()))
                or None,
                audio_codec_families=sorted(season_audio_families.get(sk, set()))
                or None,
                max_audio_channels=season_max_audio_channels.get(sk),
                subtitle_languages=sorted(season_subtitle_languages.get(sk, set()))
                or None,
            )

        return series_sizes, series_paths, season_data

    async def get_movies(
        self, included_libraries: list[str] | None = None
    ) -> list[PlexMovie]:
        """Get all movies from all movie libraries.

        Args:
            included_libraries: List of library names to include (None for all)
        """
        sections = await self.get_library_sections()
        movie_sections = [s for s in sections if s.get("type") == "movie"]

        if included_libraries:
            movie_sections = [
                s for s in movie_sections if s.get("title") in included_libraries
            ]

        all_movies = []
        for section in movie_sections:
            section_id = section["key"]
            section_uuid = section.get("uuid")
            if not section_uuid:
                LOG.warning(f"Section {section_id} missing UUID, skipping")
                continue
            section_name = section.get("title", "Unknown")
            LOG.debug(f"Processing movie library: {section_name} (ID: {section_id})")

            # type=1 to only fetch movies, not collections
            # includeGuids=1 to get external IDs
            items_data, _ = await self._make_request(
                f"library/sections/{section_id}/all",
                params={"type": 1, "includeGuids": 1},
                timeout=300,
            )
            if not items_data:
                continue

            items = items_data.get("MediaContainer", {}).get("Metadata", [])  # pyright: ignore [reportAttributeAccessIssue]

            needs_detail_keys: list[str] = []
            for item in items:
                if item.get("type") != "movie":
                    continue
                media_entries = item.get("Media", [])
                if any(
                    not (m.get("Part") and m["Part"] and m["Part"][0].get("Stream"))
                    for m in media_entries
                ):
                    rk = str(item.get("ratingKey", ""))
                    if rk:
                        needs_detail_keys.append(rk)

            details_by_key = await self._get_movies_metadata_batch(needs_detail_keys)

            for item in items:
                # only include actual movies, not collections or other types
                if item.get("type") != "movie":
                    continue

                ext_ids = self._parse_external_ids(item)
                if not ext_ids:
                    continue

                # library section list responses can omit detailed stream metadata.
                # when Part/Stream is missing in section list responses, use pre-fetched
                # batched details from /library/metadata/{id1,id2,...}.
                source_item = details_by_key.get(str(item["ratingKey"]), item)
                media_entries = (
                    source_item.get("Media", [])
                    if source_item
                    else item.get("Media", [])
                )

                # build one MovieVersionData per Media entry (each = one physical file/version)
                added_at = self._fromtimestamp(item.get("addedAt"))
                versions = []
                for media in media_entries:
                    media_id = str(media.get("id", ""))
                    if not media_id:
                        continue
                    part = media.get("Part", [{}])[0] if media.get("Part") else {}
                    streams = part.get("Stream", []) if isinstance(part, dict) else []
                    video_streams = [
                        s for s in streams if str(s.get("streamType")) == "1"
                    ]
                    audio_streams = [
                        s for s in streams if str(s.get("streamType")) == "2"
                    ]
                    subtitle_streams = [
                        s for s in streams if str(s.get("streamType")) == "3"
                    ]
                    first_video = video_streams[0] if video_streams else {}
                    first_audio = audio_streams[0] if audio_streams else {}
                    video_codec_raw = (
                        first_video.get("codec")
                        or media.get("videoCodec")
                        or first_video.get("codecID")
                    )
                    audio_codec_raw = (
                        first_audio.get("codec")
                        or media.get("audioCodec")
                        or first_audio.get("codecID")
                    )
                    dolby_vision_profile = (
                        first_video.get("DOVIProfile")
                        or first_video.get("doviProfile")
                        or first_video.get("dolbyVisionProfile")
                    )
                    dolby_vision_profile = (
                        f"{dolby_vision_profile}.{first_video['DOVIBLCompatID']}"
                        if dolby_vision_profile and first_video.get("DOVIBLCompatID")
                        else str(dolby_vision_profile)
                        if dolby_vision_profile
                        else None
                    )
                    version_size = sum(p.get("size", 0) for p in media.get("Part", []))
                    width = as_int(first_video.get("Width"))
                    height = as_int(first_video.get("Height"))
                    versions.append(
                        MovieVersionData(
                            service=Service.PLEX,
                            service_item_id=item["ratingKey"],
                            service_media_id=media_id,
                            library_id=section_uuid,
                            library_name=section_name,
                            path=part.get("file"),
                            size=version_size,
                            added_at=added_at,
                            file_name=PurePath(str(part.get("file"))).name
                            if part.get("file")
                            else None,
                            container=media.get("container"),
                            duration=as_float(media.get("duration")),
                            video_track_count=len(video_streams) or None,
                            video_codec=video_codec_raw,
                            video_codec_family=normalize_video_codec_family(
                                video_codec_raw
                            ),
                            video_hdr=self._is_hdr(first_video),
                            video_dolby_vision=(
                                bool(first_video.get("DOVIPresent"))
                                if first_video.get("DOVIPresent") is not None
                                else None
                            ),
                            video_dolby_vision_profile=str(dolby_vision_profile)
                            if dolby_vision_profile is not None
                            else None,
                            video_bitrate=as_int(
                                first_video.get("bitrate") or media.get("bitrate")
                            ),
                            video_bit_depth=as_int(first_video.get("bitDepth")),
                            video_width=width,
                            video_height=height,
                            video_resolution=media.get("videoResolution")
                            or guesstimate_resolution(width, height)
                            if width and height
                            else None,
                            video_color_primaries=first_video.get("colorPrimaries"),
                            video_color_space=first_video.get("colorSpace"),
                            video_color_transfer=first_video.get("colorTrc"),
                            video_fps=as_float(
                                first_video.get("frameRate")
                                or first_video.get("frameRateMode")
                            ),
                            audio_count=len(audio_streams) or None,
                            audio_languages=self._unique_languages(audio_streams),
                            audio_codec=audio_codec_raw,
                            audio_codec_family=normalize_audio_codec_family(
                                audio_codec_raw
                            ),
                            audio_title=first_audio.get("displayTitle"),
                            audio_language=(
                                str(
                                    first_audio.get("languageCode")
                                    or first_audio.get("languageTag")
                                    or first_audio.get("language")
                                ).lower()
                                if (
                                    first_audio.get("languageCode")
                                    or first_audio.get("languageTag")
                                    or first_audio.get("language")
                                )
                                else None
                            ),
                            audio_channels=as_int(
                                first_audio.get("channels")
                                or media.get("audioChannels")
                            ),
                            audio_channel_layout=first_audio.get("audioChannelLayout"),
                            audio_bitrate=as_int(first_audio.get("bitrate")),
                            audio_sample_rate=as_int(first_audio.get("samplingRate")),
                            subtitle_count=len(subtitle_streams) or None,
                            subtitle_has_forced=(
                                any(bool(s.get("forced")) for s in subtitle_streams)
                                if subtitle_streams
                                else None
                            ),
                            subtitle_languages=self._unique_languages(subtitle_streams),
                            has_chapters=(
                                True
                                if source_item
                                and isinstance(source_item.get("Chapter"), list)
                                and len(source_item.get("Chapter", [])) > 0
                                else None
                            ),
                        )
                    )

                movie = PlexMovie(
                    id=item["ratingKey"],
                    name=item.get("title", ""),
                    year=item.get("year"),
                    library_id=section_uuid,
                    library_name=section_name,
                    added_at=added_at,
                    updated_at=self._fromtimestamp(item.get("updatedAt")),
                    last_viewed_at=self._fromtimestamp(item.get("lastViewedAt")),
                    view_count=item.get("viewCount", 0),
                    external_ids=ext_ids,
                    versions=versions,
                )
                all_movies.append(movie)

        return all_movies

    async def _get_movies_metadata_batch(
        self, rating_keys: list[str]
    ) -> dict[str, dict]:
        """Fetch full movie metadata in batches via /library/metadata/{id1,id2,...}."""
        if not rating_keys:
            return {}

        detailed: dict[str, dict] = {}
        for i in range(0, len(rating_keys), _METADATA_BATCH_SIZE):
            batch = rating_keys[i : i + _METADATA_BATCH_SIZE]
            if not batch:
                continue
            ids = ",".join(batch)
            try:
                data, _ = await self._make_request(
                    f"library/metadata/{ids}",
                    params={"includeGuids": 1, "includeChapters": 1},
                    timeout=120,
                )
                if not isinstance(data, dict):
                    continue
                metadata = data.get("MediaContainer", {}).get("Metadata", [])
                if not isinstance(metadata, list):
                    continue
                for item in metadata:
                    key = str(item.get("ratingKey", ""))
                    if key:
                        detailed[key] = item
            except Exception:
                LOG.debug(f"Failed to fetch batched Plex movie metadata for ids={ids}")
        return detailed

    async def _get_episodes_metadata_batch(
        self, rating_keys: list[str]
    ) -> dict[str, dict]:
        """Fetch full episode metadata in batches via /library/metadata/{id1,id2,...}."""
        if not rating_keys:
            return {}

        detailed: dict[str, dict] = {}
        for i in range(0, len(rating_keys), _EPISODE_METADATA_BATCH_SIZE):
            batch = rating_keys[i : i + _EPISODE_METADATA_BATCH_SIZE]
            if not batch:
                continue
            ids = ",".join(batch)
            try:
                data, _ = await self._make_request(
                    f"library/metadata/{ids}",
                    timeout=120,
                )
                if not isinstance(data, dict):
                    continue
                metadata = data.get("MediaContainer", {}).get("Metadata", [])
                if not isinstance(metadata, list):
                    continue
                for item in metadata:
                    key = str(item.get("ratingKey", ""))
                    if key:
                        detailed[key] = item
            except Exception:
                LOG.debug(
                    f"Failed to fetch batched Plex episode metadata for ids={ids}"
                )
        return detailed

    async def get_series(
        self, included_libraries: list[str] | None = None
    ) -> list[PlexSeries]:
        """Get all TV series from all show libraries.

        Args:
            included_libraries: List of library names to include (None for all)
        """
        sections = await self.get_library_sections()
        show_sections = [s for s in sections if s.get("type") == "show"]

        if included_libraries:
            show_sections = [
                s for s in show_sections if s.get("title") in included_libraries
            ]

        all_series = []
        for section in show_sections:
            section_id = section["key"]
            section_uuid = section.get("uuid")
            if not section_uuid:
                LOG.warning(f"Section {section_id} missing UUID, skipping")
                continue
            section_name = section.get("title", "Unknown")
            LOG.debug(f"Processing series library: {section_name} (ID: {section_id})")

            # fetch all episode sizes and paths for this section in one API call
            (
                series_sizes,
                series_paths,
                season_data_map,
            ) = await self._get_episode_data_for_section(section_id)

            # type=2 to only fetch shows, not collections
            # includeGuids=1 to get external IDs
            items_data, _ = await self._make_request(
                f"library/sections/{section_id}/all",
                params={"type": 2, "includeGuids": 1},
                timeout=300,
            )
            if not items_data:
                continue
            items = items_data.get("MediaContainer", {}).get("Metadata", [])  # pyright: ignore [reportAttributeAccessIssue]

            for item in items:
                # only include actual shows, not collections or other types
                if item.get("type") != "show":
                    continue

                rating_key = str(item["ratingKey"])
                # get size and path from pre-calculated data
                total_size = series_sizes.get(rating_key, 0)
                series_path = series_paths.get(rating_key)

                ext_ids = self._parse_external_ids(item)
                if not ext_ids:
                    # fallback (legacy TVDB agent, we'll resolve to TMDB via API)
                    tvdb_id = self._extract_legacy_tvdb_id(item)
                    if tvdb_id:
                        tmdb_id = await self._resolve_tvdb_to_tmdb(tvdb_id)
                        if tmdb_id:
                            ext_ids = ExternalIDs(
                                tmdb=tmdb_id,
                                imdb=None,
                                tmdb_collection=None,
                                tvdb=tvdb_id,
                            )
                if not ext_ids:
                    continue

                series = PlexSeries(
                    id=item["ratingKey"],
                    name=item.get("title", ""),
                    year=item.get("year"),
                    library_id=section_uuid,
                    library_name=section_name,
                    path=series_path,
                    added_at=self._fromtimestamp(item.get("addedAt")),
                    updated_at=self._fromtimestamp(item.get("updatedAt")),
                    last_viewed_at=self._fromtimestamp(item.get("lastViewedAt")),
                    view_count=item.get("viewCount", 0),
                    external_ids=ext_ids,
                    size=total_size,
                    season_data=list(
                        v for k, v in season_data_map.items() if k[0] == rating_key
                    ),
                )
                all_series.append(series)

        return all_series

    async def _get_all_history(
        self,
        rating_keys: set[str] | None = None,
        library_section_ids: list[str] | None = None,
        key_field: str = "ratingKey",
        page_size: int = 1000,
    ) -> dict[str, _HistEntry]:
        """Fetch watch history for ALL users from /status/sessions/history/all.

        Requires an admin token so records from every account on the server are returned.
        Iterates each library section separately (if provided) and paginates fully
        through each section's history before moving to the next.

        Args:
            rating_keys: Optional set of key values to keep. Records whose extracted
                key (via key_field) is not in this set are skipped.
            library_section_ids: Numeric section IDs (the ``key`` from library sections).
                If provided, history is fetched per section. If None, a single
                server-wide call is made (no librarySectionID filter).
            key_field: Which field on each history record to use as the dict key.
                Movies: "ratingKey".
                Season-level episode roll-up: "parentRatingKey".
                Series-level episode roll-up: "grandparentRatingKey".

        Returns:
            Dict mapping the chosen key field value to
            (total_view_count, max_last_viewed_at, distinct_user_count).
        """
        # key -> [view_count, max_lva, set(accountIDs)]
        aggregated: dict[str, list] = {}

        # iterate each section separately, or do one global pass if no sections given
        sections_to_fetch: Sequence[str | None] = (
            library_section_ids if library_section_ids else [None]
        )

        for section_id in sections_to_fetch:
            container_start = 0
            while True:
                params: dict = {
                    "X-Plex-Container-Start": container_start,
                    "X-Plex-Container-Size": page_size,
                    "sort": "viewedAt:desc",
                }
                if section_id is not None:
                    params["librarySectionID"] = section_id

                try:
                    data, _ = await self._make_request(
                        "status/sessions/history/all", params=params, timeout=300
                    )
                except Exception as e:
                    LOG.warning(
                        f"Plex history fetch failed at offset {container_start} "
                        f"section={section_id}: {e}"
                    )
                    break

                if not isinstance(data, dict):
                    break

                container = data.get("MediaContainer", {})
                records = container.get("Metadata", [])
                if not records:
                    break

                for record in records:
                    key = str(record.get(key_field, ""))
                    if not key:
                        continue
                    if rating_keys is not None and key not in rating_keys:
                        continue

                    viewed_at = self._fromtimestamp(record.get("viewedAt"))
                    account_id = str(record.get("accountID", ""))

                    if key not in aggregated:
                        aggregated[key] = [0, None, set()]

                    aggregated[key][0] += 1  # each history record = one play
                    if viewed_at:
                        if aggregated[key][1] is None or viewed_at > aggregated[key][1]:
                            aggregated[key][1] = viewed_at
                    if account_id:
                        aggregated[key][2].add(account_id)

                # paginate until all records for this section are consumed
                total_size = container.get("totalSize", len(records))
                container_start += len(records)
                if container_start >= total_size:
                    break

        return {k: (v[0], v[1], len(v[2])) for k, v in aggregated.items()}

    async def get_aggregated_movies(
        self, included_libraries: list[str] | None = None
    ) -> list[AggregatedMovieData]:
        """Get aggregated movie data with optional section inclusion."""
        movies = await self.get_movies(included_libraries=included_libraries)

        # collect the numeric section IDs that were actually used so history is
        # scoped to the same libraries (avoids pulling all server history)
        sections = await self.get_library_sections()
        movie_sections = [s for s in sections if s.get("type") == "movie"]
        if included_libraries:
            movie_sections = [
                s for s in movie_sections if s.get("title") in included_libraries
            ]
        movie_section_ids = [s["key"] for s in movie_sections if s.get("key")]

        movie_keys = {m.id for m in movies}
        history = await self._get_all_history(
            rating_keys=movie_keys,
            library_section_ids=movie_section_ids,
        )

        return [
            AggregatedMovieData(
                name=m.name,
                year=m.year,
                external_ids=m.external_ids,
                versions=m.versions,
                view_count=self._merge_view_count(m.view_count, history.get(m.id)),
                last_viewed_at=self._merge_last_viewed(
                    m.last_viewed_at, history.get(m.id)
                ),
                played_by_user_count=history[m.id][2] if m.id in history else None,
            )
            for m in movies
        ]

    async def get_aggregated_series(
        self, included_libraries: list[str] | None = None
    ) -> list[AggregatedSeriesData]:
        """Get aggregated series data with optional section inclusion."""
        series = await self.get_series(included_libraries=included_libraries)

        # collect all series and season rating keys to scope the history fetch
        series_keys: set[str] = set()
        season_keys: set[str] = set()
        for s in series:
            series_keys.add(s.id)
            for sd in s.season_data:
                if sd.service_season_id:
                    season_keys.add(sd.service_season_id)

        # collect the numeric section IDs for show libraries
        sections = await self.get_library_sections()
        show_sections = [s for s in sections if s.get("type") == "show"]
        if included_libraries:
            show_sections = [
                s for s in show_sections if s.get("title") in included_libraries
            ]
        show_section_ids = [s["key"] for s in show_sections if s.get("key")]

        # fetch cross user episode history keyed by season/series ratingKey
        # episodes: ratingKey=episode, parentRatingKey=season, grandparentRatingKey=series
        season_history = await self._get_all_history(
            rating_keys=season_keys,
            library_section_ids=show_section_ids,
            key_field="parentRatingKey",
        )
        series_history = await self._get_all_history(
            rating_keys=series_keys,
            library_section_ids=show_section_ids,
            key_field="grandparentRatingKey",
        )

        result = []
        for s in series:
            merged_seasons = []
            for sd in s.season_data:
                hist = (
                    season_history.get(sd.service_season_id or "")
                    if sd.service_season_id
                    else None
                )
                merged_seasons.append(
                    AggregatedSeasonData(
                        service_series_id=sd.service_series_id,
                        season_number=sd.season_number,
                        size=sd.size,
                        episode_count=sd.episode_count,
                        view_count=self._merge_view_count(sd.view_count, hist),
                        last_viewed_at=self._merge_last_viewed(sd.last_viewed_at, hist),
                        added_at=sd.added_at,
                        air_date=sd.air_date,
                        service_season_id=sd.service_season_id,
                        has_hdr=sd.has_hdr,
                        has_dolby_vision=sd.has_dolby_vision,
                        max_video_width=sd.max_video_width,
                        max_video_height=sd.max_video_height,
                        video_codec_families=sd.video_codec_families,
                        audio_codec_families=sd.audio_codec_families,
                        max_audio_channels=sd.max_audio_channels,
                        subtitle_languages=sd.subtitle_languages,
                    )
                )

            s_hist = series_history.get(s.id)
            result.append(
                AggregatedSeriesData(
                    id=s.id,
                    name=s.name,
                    year=s.year,
                    service=Service.PLEX,
                    library_id=s.library_id,
                    library_name=s.library_name,
                    path=s.path,
                    added_at=s.added_at,
                    external_ids=s.external_ids,
                    size=s.size,
                    view_count=self._merge_view_count(s.view_count, s_hist),
                    last_viewed_at=self._merge_last_viewed(s.last_viewed_at, s_hist),
                    played_by_user_count=s_hist[2] if s_hist else None,
                    season_data=merged_seasons,
                )
            )
        return result

    async def _resolve_tvdb_to_tmdb(self, tvdb_id: str) -> int | None:
        """Resolve a TVDB series ID to a TMDB series ID via the TMDB /find endpoint."""
        try:
            async with AsyncTMDBClient() as tmdb:
                data = await tmdb.find_by_external_id(tvdb_id, "tvdb_id")
            if not data or data is False:
                return None
            results = data.get("tv_results", [])  # type: ignore[reportAttributeAccessIssue]
            if results:
                return int(results[0]["id"])
        except Exception:
            LOG.warning(f"Failed to resolve TVDB ID {tvdb_id!r} to TMDB ID")
        return None

    @staticmethod
    def _parse_external_ids(media: dict) -> ExternalIDs | None:
        imdb_id = None
        tmdb_id = None
        tvdb_id = None

        guids = media.get("Guid", [])
        if guids:
            # newer plex agent format
            for guid in guids:
                guid_id = guid.get("id", "")
                if guid_id.startswith("imdb://"):
                    imdb_id = guid_id.replace("imdb://", "")
                elif guid_id.startswith("tmdb://"):
                    raw = guid_id.replace("tmdb://", "")
                    if not raw.isdigit():
                        LOG.warning(
                            f"Skipping media item: invalid TMDb ID '{raw}' in Plex GUID"
                        )
                        continue
                    tmdb_id = int(raw)
                elif guid_id.startswith("tvdb://"):
                    tvdb_id = guid_id.replace("tvdb://", "")
        else:
            # legacy plex agent format (single top level guid attribute)
            legacy_guid = media.get("guid", "")
            if "agents.themoviedb://" in legacy_guid:
                raw = legacy_guid.split("://", 1)[1].split("?")[0].strip("/")
                if raw.isdigit():
                    tmdb_id = int(raw)
                else:
                    LOG.warning(
                        f"Skipping media item: invalid legacy TMDb ID '{raw}' in Plex GUID"
                    )

        if not tmdb_id:
            return None

        if tmdb_id or imdb_id or tvdb_id:
            return ExternalIDs(
                tmdb=tmdb_id,
                imdb=imdb_id,
                tmdb_collection=None,
                tvdb=tvdb_id,
            )
        return None

    @staticmethod
    def _is_hdr(stream: dict) -> bool:
        # bit depth (if less than 10 bits it's not hdr)
        try:
            if int(stream["bitDepth"]) < 10:
                return False
        except Exception:
            pass
        # dolby Vision
        if stream.get("DOVIPresent") or stream.get("DOVIProfile"):
            return True
        # HDR10/HLG transfer functions
        if str(stream.get("colorTrc", "")).lower() in ("smpte2084", "arib-std-b67"):
            return True
        # BT.2020 color primaries (common for HDR)
        if str(stream.get("colorPrimaries", "")).lower() in (
            "bt2020",
            "bt.2020",
            "bt2020nc",
        ):
            return True
        # title contains "hdr"
        if "hdr" in str(stream.get("extendedDisplayTitle", "")).lower():
            return True
        return False

    @staticmethod
    def _unique_languages(streams: list[dict]) -> list[str] | None:
        """Extract unique language codes from Plex stream data.

        We maintain the order and only keep the unique values.
        """
        langs: list[str] = []
        seen: set[str] = set()
        for stream in streams:
            raw = (
                stream.get("languageCode")
                or stream.get("languageTag")
                or stream.get("language")
            )
            if not raw:
                continue
            value = str(raw).strip().lower()
            if not value or value in seen:
                continue
            seen.add(value)
            langs.append(value)
        return langs or None

    @staticmethod
    def _extract_legacy_tvdb_id(media: dict) -> str | None:
        """Extract a TVDB ID from a legacy Plex agent GUID attribute.

        Handles both show level and episode level GUIDs:
          com.plexapp.agents.thetvdb://301824?lang=en       -> "301824"
          com.plexapp.agents.thetvdb://301824/1/1?lang=en   -> "301824"
        """
        guid = media.get("guid", "")
        if "agents.thetvdb://" not in guid:
            return None
        raw = guid.split("://", 1)[1].split("?")[0].split("/")[0]
        return raw if raw.isdigit() else None

    @staticmethod
    def _merge_view_count(scan_count: int, hist: _HistEntry | None) -> int:
        """Return the higher of the library-scan view count and the history count."""
        if hist is None:
            return scan_count
        return max(scan_count, hist[0])

    @staticmethod
    def _merge_last_viewed(
        scan_lva: datetime | None, hist: _HistEntry | None
    ) -> datetime | None:
        """Return the most recent last-viewed timestamp between scan and history."""
        if hist is None:
            return scan_lva
        hist_lva = hist[1]
        if scan_lva is None:
            return hist_lva
        if hist_lva is None:
            return scan_lva
        return max(scan_lva, hist_lva)

    @staticmethod
    def _check_plex_healthy(response: Any) -> bool:
        """Check if the Plex /identity response indicates a healthy connection.

        We'll verify the response is a dict and has the expected structure:
        ```python
        {'MediaContainer': {'size': 0, 'apiVersion': '1.2.0', 'claimed': True,
        'machineIdentifier': 'someMachineID', 'version': '1.43.1.10611-1e34174b1'}}
        ```
        """
        return (
            isinstance(response, dict)
            and "MediaContainer" in response
            and isinstance(response["MediaContainer"], dict)
            and "machineIdentifier" in response["MediaContainer"]
        )

    @staticmethod
    async def test_service(url: str, api_key: str) -> bool:
        """Test Plex service connection without full initialization."""
        async with niquests.AsyncSession() as session:
            response = await session.get(
                f"{url.rstrip('/')}/identity",
                headers={"X-Plex-Token": api_key, "Accept": "application/json"},
            )
            response.raise_for_status()
            try:
                data = response.json()
                if PlexService._check_plex_healthy(data):
                    return True
            except Exception:
                pass  # we can just pass and raise the ValueError
            raise ValueError(
                f"Unexpected response (status code: {response.status_code})"
            )
