from __future__ import annotations

import re
import shutil
from collections.abc import Iterable, Iterator, Mapping
from contextvars import ContextVar
from datetime import UTC, date, datetime
from typing import Any, Final, TypeAlias

from backend.core.utils.filesystem import normalize_fpath
from backend.core.utils.language import normalize_language
from backend.core.utils.misc import normalize_genre_names, normalize_name_list
from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    ReclaimRule,
    Season,
    Series,
)
from backend.enums import MediaType, Service

TARGET_MOVIE_VERSION = "movie_version"
TARGET_SERIES = "series"
TARGET_SEASON = "season"
TARGET_EPISODE = "episode"
VALID_TARGET_SCOPES = {
    TARGET_MOVIE_VERSION,
    TARGET_SERIES,
    TARGET_SEASON,
    TARGET_EPISODE,
}
RULE_OUTCOME_CANDIDATE = "candidate"
RULE_OUTCOME_PROTECT = "protect"
SONARR_RULE_FIELDS = {
    "sonarr.series_status",
    "sonarr.latest_season_has_unaired_episodes",
    "sonarr.latest_season_has_finale",
}
ARR_ID_RULE_FIELDS = {"arr.movie_ids", "arr.series_ids"}
FAVORITES_RULE_FIELDS = {
    "favorites.exists",
    "favorites.usernames",
    "favorites.user_count",
}
RANK_RULE_FIELDS = {
    "episode.position_by_air_date",
    "season.position_by_air_date",
}
COLLECTION_RULE_FIELDS = {
    "collection.latest_sibling_watched_at",
    "collection.days_since_latest_sibling_watched",
}
PLAYBACK_RULE_FIELDS = {
    "playback.has_activity",
    "playback.play_count",
    "playback.total_duration_minutes",
    "playback.longest_duration_minutes",
    "playback.unique_user_count",
    "playback.usernames",
    "playback.last_activity_at",
    "playback.days_since_last_activity",
}

RuleDefinition = dict[str, Any]


class UnavailableRuleValue:
    __slots__ = ()

    def __repr__(self) -> str:
        return "RULE_VALUE_UNAVAILABLE"


RULE_VALUE_UNAVAILABLE: Final[UnavailableRuleValue] = UnavailableRuleValue()
SonarrRuleValue: TypeAlias = bool | str | UnavailableRuleValue


def normalize_rule_outcome(rule: ReclaimRule) -> str:
    action = rule.action if isinstance(rule.action, dict) else {}
    return (
        RULE_OUTCOME_PROTECT
        if action.get("outcome") == RULE_OUTCOME_PROTECT
        else RULE_OUTCOME_CANDIDATE
    )


FIELD_LABELS: dict[str, str] = {
    "library.id": "Library",
    "media.title": "Title",
    "media.path": "Path",
    "media.file_name": "Filename",
    "media.size": "Size",
    "media.year": "Year",
    "media.container": "Container",
    "media.days_since_added": "Days since added",
    "arr.days_since_file_added": "Days since latest Arr file added",
    "watch.view_count": "Views",
    "watch.days_since_last_watched": "Days since watched",
    "watch.last_viewed_at": "Last watched",
    "tmdb.release_date": "TMDB release date",
    "tmdb.in_collection": "TMDB in collection",
    "tmdb.collection_name": "TMDB collection name",
    "tmdb.genres": "TMDB genres",
    "tmdb.original_language": "TMDB original language",
    "tmdb.origin_country": "TMDB origin country",
    "tmdb.runtime_minutes": "TMDB runtime (minutes)",
    "tmdb.first_air_date": "TMDB first air date",
    "tmdb.last_air_date": "TMDB last air date",
    "season.air_date": "Season air date",
    "tmdb.days_since_release": "Days since released",
    "tmdb.days_since_first_air_date": "Days since first aired",
    "tmdb.days_since_last_air_date": "Days since last aired",
    "season.days_since_air_date": "Days since season aired",
    "season.season_number": "Season number",
    "season.episode_count": "Episode count",
    "season.fully_watched": "Season fully watched",
    "season.watched_percent": "Season watched (%)",
    "season.is_latest_season": "Is latest season",
    "season.seasons_from_latest": "Seasons from latest",
    "episode.number": "Episode number",
    "episode.season_number": "Episode season number",
    "episode.air_date": "Episode air date",
    "episode.days_since_air_date": "Days since episode aired",
    "watch.never_watched": "Never watched",
    "playback.has_activity": "Imported playback activity",
    "playback.play_count": "Playback plays",
    "playback.total_duration_minutes": "Playback duration (minutes)",
    "playback.longest_duration_minutes": "Longest playback (minutes)",
    "playback.unique_user_count": "Playback user count",
    "playback.usernames": "Playback users",
    "playback.last_activity_at": "Last playback activity",
    "playback.days_since_last_activity": "Days since playback activity",
    "tmdb.popularity": "Popularity",
    "tmdb.vote_average": "TMDB rating",
    "tmdb.vote_count": "Vote count",
    "imdb.rating": "IMDb rating",
    "imdb.vote_count": "IMDb vote count",
    "anilist.score": "AniList score",
    "anilist.popularity": "AniList popularity",
    "anilist.favourites": "AniList favourites",
    "rottentomatoes.tomato_meter": "Rotten Tomatoes Tomatometer",
    "rottentomatoes.tomato_vote_count": "Rotten Tomatoes Tomatometer votes",
    "rottentomatoes.popcorn_meter": "Rotten Tomatoes Popcornmeter",
    "rottentomatoes.popcorn_vote_count": "Rotten Tomatoes Popcornmeter votes",
    "metacritic.metascore": "Metacritic metascore",
    "metacritic.vote_count": "Metacritic critic count",
    "metacritic.user_score": "Metacritic user score",
    "metacritic.user_vote_count": "Metacritic user votes",
    "trakt.rating": "Trakt rating",
    "trakt.vote_count": "Trakt votes",
    "letterboxd.score": "Letterboxd score",
    "letterboxd.vote_count": "Letterboxd votes",
    "series.status": "Series status",
    "series.tmdb_season_count": "TMDB season count",
    "series.library_season_count": "Library season count",
    "movie.version_count": "Movie version count",
    "video.codec_family": "Video codec",
    "audio.codec_family": "Audio codec",
    "video.hdr": "HDR",
    "video.dolby_vision": "Dolby Vision",
    "video.width": "Video width",
    "video.height": "Video height",
    "video.bitrate_kbps": "Video bitrate (kbps)",
    "video.bit_depth": "Video bit depth",
    "video.resolution": "Resolution",
    "audio.channels": "Audio channels",
    "audio.track_count": "Audio tracks",
    "audio.bitrate_kbps": "Audio bitrate (kbps)",
    "audio.languages": "Audio languages",
    "subtitle.languages": "Subtitle languages",
    "subtitle.track_count": "Subtitle tracks",
    "subtitle.has_forced": "Has forced subtitles",
    "video.color_space": "Color space",
    "video.color_transfer": "Color transfer",
    "video.color_primaries": "Color primaries",
    "media.duration": "Duration",
    "media_server.collections": "Media server collections",
    "arr.tags": "Arr tags",
    "arr.movie_ids": "Radarr movie IDs",
    "arr.series_ids": "Sonarr series IDs",
    "arr.monitored": "Arr monitored",
    "sonarr.latest_season_has_unaired_episodes": (
        "Sonarr latest season has unaired episodes"
    ),
    "sonarr.latest_season_has_finale": "Sonarr latest season has finale",
    "sonarr.series_status": "Sonarr series status",
    "seerr.requested": "Seerr requested",
    "seerr.requested_by_user_ids": "Seerr requested by user IDs",
    "seerr.requester_has_watched": "Seerr requester has watched",
    "seerr.last_requested_at": "Seerr latest active request",
    "seerr.days_since_last_requested": "Days since latest active Seerr request",
    "favorites.exists": "Favorited or watchlisted",
    "favorites.usernames": "Favorite/watchlist users",
    "favorites.user_count": "Favorite/watchlist user count",
    "episode.position_by_air_date": "Episode position by air date",
    "season.position_by_air_date": "Season position by air date",
    "collection.latest_sibling_watched_at": "Collection sibling last watched",
    "collection.days_since_latest_sibling_watched": (
        "Days since collection sibling watched"
    ),
    "media_server.user_rating": "Media server user rating",
    "disk.free_bytes": "Disk free (bytes)",
    "disk.free_percent": "Disk free (%)",
}

OPERATOR_LABELS: dict[str, str] = {
    "equals": "is",
    "not_equals": "is not",
    "greater_than": ">",
    "greater_than_or_equal": ">=",
    "less_than": "<",
    "less_than_or_equal": "<=",
    "before": "is before",
    "on_or_before": "is on or before",
    "after": "is after",
    "on_or_after": "is on or after",
    "in": "in",
    "not_in": "not in",
    "contains_any": "matches any",
    "not_contains_any": "matches none",
    "contains_all": "matches all",
    "not_contains_all": "does not match all",
    "contains_substring": "contains",
    "not_contains_substring": "does not contain",
    "exists": "exists",
    "not_exists": "missing",
    "is_true": "is true",
    "is_false": "is false",
    "matches_any_regex": "matches regex",
    "not_matches_any_regex": "does not match regex",
}

LIST_OPERATORS = {
    "in",
    "not_in",
    "contains_any",
    "not_contains_any",
    "contains_all",
    "not_contains_all",
    "matches_any_regex",
    "not_matches_any_regex",
}
VALUELESS_OPERATORS = {"exists", "not_exists", "is_true", "is_false"}
NUMERIC_FIELDS = {
    "media.size",
    "media.year",
    "media.days_since_added",
    "arr.days_since_file_added",
    "seerr.days_since_last_requested",
    "watch.view_count",
    "watch.days_since_last_watched",
    "playback.play_count",
    "playback.total_duration_minutes",
    "playback.longest_duration_minutes",
    "playback.unique_user_count",
    "playback.days_since_last_activity",
    "favorites.user_count",
    "episode.position_by_air_date",
    "season.position_by_air_date",
    "collection.days_since_latest_sibling_watched",
    "media_server.user_rating",
    "tmdb.days_since_release",
    "tmdb.days_since_first_air_date",
    "tmdb.days_since_last_air_date",
    "season.days_since_air_date",
    "season.season_number",
    "season.episode_count",
    "season.watched_percent",
    "season.seasons_from_latest",
    "episode.number",
    "episode.season_number",
    "episode.days_since_air_date",
    "tmdb.popularity",
    "tmdb.vote_average",
    "tmdb.vote_count",
    "tmdb.runtime_minutes",
    "imdb.rating",
    "imdb.vote_count",
    "anilist.score",
    "anilist.popularity",
    "anilist.favourites",
    "rottentomatoes.tomato_meter",
    "rottentomatoes.tomato_vote_count",
    "rottentomatoes.popcorn_meter",
    "rottentomatoes.popcorn_vote_count",
    "metacritic.metascore",
    "metacritic.vote_count",
    "metacritic.user_score",
    "metacritic.user_vote_count",
    "trakt.rating",
    "trakt.vote_count",
    "letterboxd.score",
    "letterboxd.vote_count",
    "video.width",
    "video.height",
    "audio.channels",
    "audio.track_count",
    "audio.bitrate_kbps",
    "subtitle.track_count",
    "video.bitrate_kbps",
    "video.bit_depth",
    "movie.version_count",
    "series.tmdb_season_count",
    "series.library_season_count",
    "media.duration",
    "disk.free_bytes",
    "disk.free_percent",
}
TEXT_FIELDS = {
    "tmdb.collection_name",
    "tmdb.genres",
    "media.title",
    "tmdb.original_language",
    "tmdb.origin_country",
    "media_server.collections",
    "media.container",
    "series.status",
    "sonarr.series_status",
    "video.codec_family",
    "audio.codec_family",
    "video.resolution",
    "audio.languages",
    "subtitle.languages",
    "video.color_space",
    "video.color_transfer",
    "video.color_primaries",
    "arr.tags",
    "arr.movie_ids",
    "arr.series_ids",
    "media_server.collections",
    "favorites.usernames",
    "seerr.requested_by_user_ids",
    "playback.usernames",
}
MULTI_VALUE_TEXT_FIELDS = {
    "arr.tags",
    "arr.movie_ids",
    "arr.series_ids",
    "favorites.usernames",
    "video.codec_family",
    "audio.codec_family",
    "audio.languages",
    "subtitle.languages",
    "tmdb.original_language",
    "tmdb.origin_country",
    "playback.usernames",
}
LANGUAGE_FIELDS = {
    "audio.languages",
    "subtitle.languages",
    "tmdb.original_language",
}
COUNTRY_FIELDS = {"tmdb.origin_country"}
FAIL_CLOSED_LIST_FIELDS = LANGUAGE_FIELDS | COUNTRY_FIELDS
LIBRARY_FIELDS = {"library.id"}
BOOLEAN_FIELDS = {
    "tmdb.in_collection",
    "video.hdr",
    "video.dolby_vision",
    "subtitle.has_forced",
    "season.fully_watched",
    "season.is_latest_season",
    "watch.never_watched",
    "favorites.exists",
    "playback.has_activity",
    "arr.monitored",
    "sonarr.latest_season_has_unaired_episodes",
    "sonarr.latest_season_has_finale",
    "seerr.requested",
    "seerr.requester_has_watched",
}
TEMPORAL_FIELDS = {
    "watch.last_viewed_at",
    "collection.latest_sibling_watched_at",
    "playback.last_activity_at",
    "tmdb.release_date",
    "tmdb.first_air_date",
    "tmdb.last_air_date",
    "season.air_date",
    "episode.air_date",
    "seerr.last_requested_at",
}
PATH_FIELDS = {"media.path", "media.file_name"}
NUMERIC_OPERATORS = {
    "equals",
    "not_equals",
    "greater_than",
    "greater_than_or_equal",
    "less_than",
    "less_than_or_equal",
    "exists",
    "not_exists",
}
TEXT_OPERATORS = {
    "equals",
    "not_equals",
    "in",
    "not_in",
    "contains_any",
    "not_contains_any",
    "contains_all",
    "not_contains_all",
    "exists",
    "not_exists",
}
LIBRARY_OPERATORS = {
    "in",
    "not_in",
    "contains_any",
    "not_contains_any",
    "contains_all",
    "not_contains_all",
    "exists",
    "not_exists",
}
BOOLEAN_OPERATORS = {"is_true", "is_false", "exists", "not_exists"}
SEERR_REQUESTER_ID_OPERATORS = {
    "in",
    "not_in",
    "contains_any",
    "not_contains_any",
    "contains_all",
    "not_contains_all",
    "exists",
    "not_exists",
}
MULTI_VALUE_TEXT_OPERATORS = {
    "contains_any",
    "not_contains_any",
    "contains_all",
    "not_contains_all",
    "exists",
    "not_exists",
}
TAG_SUBSTRING_OPERATORS = {
    "contains_substring",
    "not_contains_substring",
}
REGEX_OPERATORS = {"matches_any_regex", "not_matches_any_regex"}
TEMPORAL_OPERATORS = {
    "exists",
    "not_exists",
    "before",
    "on_or_before",
    "after",
    "on_or_after",
}
PATH_OPERATORS = TEXT_OPERATORS | {"matches_any_regex"}
PATH_LIBRARY_INCLUSION_OPERATORS = {"contains_any", "contains_all", "in", "equals"}
PATH_LIBRARY_UNSUPPORTED_OPERATORS = {
    "not_in",
    "not_contains_any",
    "not_contains_all",
    "not_equals",
    "exists",
    "not_exists",
}
FIELD_ALLOWED_OPERATORS: dict[str, set[str]] = {
    **{field: set(NUMERIC_OPERATORS) for field in NUMERIC_FIELDS},
    **{field: set(TEXT_OPERATORS) for field in TEXT_FIELDS},
    **{field: set(LIBRARY_OPERATORS) for field in LIBRARY_FIELDS},
    **{field: set(BOOLEAN_OPERATORS) for field in BOOLEAN_FIELDS},
    **{field: set(TEMPORAL_OPERATORS) for field in TEMPORAL_FIELDS},
    **{field: set(PATH_OPERATORS) for field in PATH_FIELDS},
    "tmdb.genres": set(MULTI_VALUE_TEXT_OPERATORS),
    "media_server.collections": set(MULTI_VALUE_TEXT_OPERATORS),
    "playback.usernames": set(MULTI_VALUE_TEXT_OPERATORS),
    "favorites.usernames": set(MULTI_VALUE_TEXT_OPERATORS),
    "seerr.requested_by_user_ids": set(SEERR_REQUESTER_ID_OPERATORS),
    "arr.movie_ids": set(SEERR_REQUESTER_ID_OPERATORS),
    "arr.series_ids": set(SEERR_REQUESTER_ID_OPERATORS),
    "arr.tags": set(TEXT_OPERATORS) | TAG_SUBSTRING_OPERATORS | REGEX_OPERATORS,
}

TARGET_SCOPE_ALLOWED_FIELDS: dict[str, set[str]] = {
    TARGET_MOVIE_VERSION: {
        "anilist.favourites",
        "anilist.popularity",
        "anilist.score",
        "arr.monitored",
        "arr.movie_ids",
        "arr.tags",
        "audio.channels",
        "audio.bitrate_kbps",
        "audio.codec_family",
        "audio.languages",
        "audio.track_count",
        "disk.free_bytes",
        "disk.free_percent",
        "imdb.rating",
        "imdb.vote_count",
        "letterboxd.score",
        "letterboxd.vote_count",
        "metacritic.metascore",
        "metacritic.user_score",
        "metacritic.user_vote_count",
        "metacritic.vote_count",
        "library.id",
        "media.title",
        "media.days_since_added",
        "arr.days_since_file_added",
        "media.container",
        "media.duration",
        "media.file_name",
        "media.path",
        "media.size",
        "media.year",
        "media_server.collections",
        "media_server.user_rating",
        "favorites.exists",
        "favorites.usernames",
        "favorites.user_count",
        "collection.latest_sibling_watched_at",
        "collection.days_since_latest_sibling_watched",
        "seerr.requested",
        "seerr.last_requested_at",
        "seerr.days_since_last_requested",
        "seerr.requested_by_user_ids",
        "seerr.requester_has_watched",
        "rottentomatoes.popcorn_meter",
        "rottentomatoes.popcorn_vote_count",
        "rottentomatoes.tomato_meter",
        "rottentomatoes.tomato_vote_count",
        "subtitle.languages",
        "subtitle.has_forced",
        "subtitle.track_count",
        "tmdb.days_since_release",
        "tmdb.in_collection",
        "tmdb.collection_name",
        "tmdb.genres",
        "tmdb.original_language",
        "tmdb.origin_country",
        "tmdb.popularity",
        "tmdb.release_date",
        "tmdb.runtime_minutes",
        "tmdb.vote_average",
        "tmdb.vote_count",
        "trakt.rating",
        "trakt.vote_count",
        "video.codec_family",
        "video.bitrate_kbps",
        "video.bit_depth",
        "video.color_primaries",
        "video.color_space",
        "video.color_transfer",
        "video.dolby_vision",
        "video.hdr",
        "video.height",
        "video.resolution",
        "video.width",
        "watch.days_since_last_watched",
        "watch.last_viewed_at",
        "watch.never_watched",
        "watch.view_count",
        *PLAYBACK_RULE_FIELDS,
        "movie.version_count",
    },
    TARGET_SERIES: {
        "anilist.favourites",
        "anilist.popularity",
        "anilist.score",
        "arr.monitored",
        "arr.series_ids",
        "arr.tags",
        "audio.channels",
        "audio.codec_family",
        "disk.free_bytes",
        "disk.free_percent",
        "imdb.rating",
        "imdb.vote_count",
        "letterboxd.score",
        "letterboxd.vote_count",
        "metacritic.metascore",
        "metacritic.user_score",
        "metacritic.user_vote_count",
        "metacritic.vote_count",
        "library.id",
        "media.title",
        "media.days_since_added",
        "arr.days_since_file_added",
        "media.file_name",
        "media.path",
        "media.size",
        "media.year",
        "media_server.collections",
        "media_server.user_rating",
        "favorites.exists",
        "favorites.usernames",
        "favorites.user_count",
        "seerr.requested",
        "seerr.last_requested_at",
        "seerr.days_since_last_requested",
        "seerr.requested_by_user_ids",
        "seerr.requester_has_watched",
        "rottentomatoes.popcorn_meter",
        "rottentomatoes.popcorn_vote_count",
        "rottentomatoes.tomato_meter",
        "rottentomatoes.tomato_vote_count",
        "series.status",
        "series.library_season_count",
        "series.tmdb_season_count",
        "sonarr.latest_season_has_unaired_episodes",
        "sonarr.latest_season_has_finale",
        "sonarr.series_status",
        "subtitle.languages",
        "tmdb.days_since_first_air_date",
        "tmdb.days_since_last_air_date",
        "tmdb.first_air_date",
        "tmdb.last_air_date",
        "tmdb.genres",
        "tmdb.original_language",
        "tmdb.origin_country",
        "tmdb.popularity",
        "tmdb.vote_average",
        "tmdb.vote_count",
        "trakt.rating",
        "trakt.vote_count",
        "video.codec_family",
        "video.dolby_vision",
        "video.hdr",
        "video.height",
        "video.width",
        "watch.days_since_last_watched",
        "watch.last_viewed_at",
        "watch.never_watched",
        "watch.view_count",
        *PLAYBACK_RULE_FIELDS,
    },
    TARGET_SEASON: {
        "anilist.favourites",
        "anilist.popularity",
        "anilist.score",
        "arr.monitored",
        "arr.series_ids",
        "arr.tags",
        "audio.channels",
        "audio.codec_family",
        "audio.languages",
        "disk.free_bytes",
        "disk.free_percent",
        "imdb.rating",
        "imdb.vote_count",
        "letterboxd.score",
        "letterboxd.vote_count",
        "metacritic.metascore",
        "metacritic.user_score",
        "metacritic.user_vote_count",
        "metacritic.vote_count",
        "library.id",
        "media.title",
        "media.days_since_added",
        "arr.days_since_file_added",
        "media.file_name",
        "media.path",
        "media.size",
        "media.year",
        "media_server.collections",
        "media_server.user_rating",
        "favorites.exists",
        "favorites.usernames",
        "favorites.user_count",
        "season.air_date",
        "season.days_since_air_date",
        "season.episode_count",
        "season.fully_watched",
        "season.is_latest_season",
        "season.season_number",
        "season.seasons_from_latest",
        "season.position_by_air_date",
        "season.watched_percent",
        "seerr.requested",
        "seerr.last_requested_at",
        "seerr.days_since_last_requested",
        "seerr.requested_by_user_ids",
        "seerr.requester_has_watched",
        "rottentomatoes.popcorn_meter",
        "rottentomatoes.popcorn_vote_count",
        "rottentomatoes.tomato_meter",
        "rottentomatoes.tomato_vote_count",
        "series.status",
        "sonarr.series_status",
        "series.library_season_count",
        "series.tmdb_season_count",
        "subtitle.languages",
        "tmdb.days_since_first_air_date",
        "tmdb.days_since_last_air_date",
        "tmdb.first_air_date",
        "tmdb.last_air_date",
        "tmdb.genres",
        "tmdb.original_language",
        "tmdb.origin_country",
        "tmdb.popularity",
        "tmdb.vote_average",
        "tmdb.vote_count",
        "trakt.rating",
        "trakt.vote_count",
        "video.codec_family",
        "video.dolby_vision",
        "video.hdr",
        "video.height",
        "video.width",
        "watch.days_since_last_watched",
        "watch.last_viewed_at",
        "watch.never_watched",
        "watch.view_count",
        *PLAYBACK_RULE_FIELDS,
    },
    TARGET_EPISODE: {
        "anilist.favourites",
        "anilist.popularity",
        "anilist.score",
        "arr.monitored",
        "arr.series_ids",
        "arr.tags",
        "disk.free_bytes",
        "disk.free_percent",
        "episode.air_date",
        "episode.days_since_air_date",
        "episode.number",
        "episode.position_by_air_date",
        "episode.season_number",
        "imdb.rating",
        "imdb.vote_count",
        "letterboxd.score",
        "letterboxd.vote_count",
        "metacritic.metascore",
        "metacritic.user_score",
        "metacritic.user_vote_count",
        "metacritic.vote_count",
        "library.id",
        "media.title",
        "media.days_since_added",
        "arr.days_since_file_added",
        "media.file_name",
        "media.path",
        "media.size",
        "media.year",
        "media_server.collections",
        "media_server.user_rating",
        "favorites.exists",
        "favorites.usernames",
        "favorites.user_count",
        "season.air_date",
        "season.days_since_air_date",
        "season.episode_count",
        "season.fully_watched",
        "season.is_latest_season",
        "season.season_number",
        "season.seasons_from_latest",
        "season.watched_percent",
        "seerr.requested",
        "seerr.last_requested_at",
        "seerr.days_since_last_requested",
        "seerr.requested_by_user_ids",
        "seerr.requester_has_watched",
        "rottentomatoes.popcorn_meter",
        "rottentomatoes.popcorn_vote_count",
        "rottentomatoes.tomato_meter",
        "rottentomatoes.tomato_vote_count",
        "series.status",
        "sonarr.series_status",
        "series.library_season_count",
        "series.tmdb_season_count",
        "tmdb.days_since_first_air_date",
        "tmdb.days_since_last_air_date",
        "tmdb.first_air_date",
        "tmdb.last_air_date",
        "tmdb.genres",
        "tmdb.original_language",
        "tmdb.origin_country",
        "tmdb.popularity",
        "tmdb.vote_average",
        "tmdb.vote_count",
        "trakt.rating",
        "trakt.vote_count",
        "watch.days_since_last_watched",
        "watch.last_viewed_at",
        "watch.never_watched",
        "watch.view_count",
        *PLAYBACK_RULE_FIELDS,
    },
}


class DiskStatsResolver:
    """Holds pre fetched disk stats for one scan run.

    Instantiate once at scan start and call ``activate()`` to install it for
    the current async context.  All rule engine code running in the same
    asyncio Task (or its sub tasks) can then call ``DiskStatsResolver.current()``
    to obtain the active instance.

    No lock is required (each scan creates its own instance with its own
    snapshot of arr data. The instance level dict cache avoids redundant
    lookups across the hundreds of media items evaluated in a single scan).
    """

    _ctx: ContextVar[DiskStatsResolver | None] = ContextVar(
        "disk_stats_resolver", default=None
    )

    __slots__ = ("_arr_entries", "_path_mappings", "_cache")

    def __init__(
        self,
        arr_entries: list[dict[str, Any]] | None = None,
        path_mappings: list[dict[str, Any]] | None = None,
    ) -> None:
        self._arr_entries: list[dict[str, Any]] = sorted(
            arr_entries or [], key=lambda e: -len(str(e.get("path") or ""))
        )
        self._path_mappings: list[dict[str, Any]] = sorted(
            path_mappings or [], key=lambda m: -len(str(m.get("source_prefix") or ""))
        )
        self._cache: dict[str, tuple[int, float] | None] = {}

    def activate(self) -> None:
        """Install this resolver for the current async context."""
        DiskStatsResolver._ctx.set(self)

    @classmethod
    def current(cls) -> DiskStatsResolver | None:
        """Return the resolver active in the current async context, or None."""
        return cls._ctx.get()

    def resolve(self, path: str) -> tuple[int, float] | None:
        """Return ``(free_bytes, free_percent)`` for the filesystem containing *path*.

        Results are cached for the lifetime of this instance (one scan run).
        Primary source: pre fetched arr /disk space entries.
        Fallback: shutil.disk_usage with path-mapping translation.
        """
        if path in self._cache:
            return self._cache[path]
        result = self._resolve_arr(path) or self._resolve_local(path)
        self._cache[path] = result
        return result

    def _resolve_arr(self, path: str) -> tuple[int, float] | None:
        """Look up disk stats in the pre fetched Radarr/Sonarr /disk space entries."""
        norm = path.replace("\\", "/")
        for entry in self._arr_entries:  # sorted longest first
            raw = (entry.get("path") or "").replace("\\", "/")
            if not raw:
                continue
            ep = raw.rstrip("/") or "/"  # preserve "/" (never collapse to "")
            if ep == "/":
                if not norm.startswith("/"):
                    continue
            elif norm != ep and not norm.startswith(ep + "/"):
                continue
            free = entry.get("free_space", 0) or 0
            total = entry.get("total_space", 0) or 0
            return free, (free / total * 100.0 if total else 0.0)
        return None

    def _resolve_local(self, path: str) -> tuple[int, float] | None:
        """Fall back to shutil.disk_usage with path mapping translation."""
        for m in self._path_mappings:  # sorted longest source first
            source = m.get("source_prefix") or ""
            if source and path.startswith(source):
                local = (m.get("local_prefix") or "") + path[len(source) :]
                try:
                    usage = shutil.disk_usage(local)
                    return usage.free, (
                        usage.free / usage.total * 100.0 if usage.total else 0.0
                    )
                except Exception:
                    return None
        try:
            usage = shutil.disk_usage(path)
            return usage.free, (
                usage.free / usage.total * 100.0 if usage.total else 0.0
            )
        except Exception:
            return None


class ArrRuleDataResolver:
    """Holds preloaded Radarr/Sonarr IDs for rule evaluation."""

    _ctx: ContextVar[ArrRuleDataResolver | None] = ContextVar(
        "arr_rule_data_resolver", default=None
    )

    __slots__ = ("_movie_ids_by_movie_id", "_series_ids_by_series_id")

    def __init__(
        self,
        *,
        movie_ids_by_movie_id: Mapping[int, Iterable[int]] | None = None,
        series_ids_by_series_id: Mapping[int, Iterable[int]] | None = None,
    ) -> None:
        self._movie_ids_by_movie_id = {
            movie_id: sorted({str(value) for value in values})
            for movie_id, values in (movie_ids_by_movie_id or {}).items()
        }
        self._series_ids_by_series_id = {
            series_id: sorted({str(value) for value in values})
            for series_id, values in (series_ids_by_series_id or {}).items()
        }

    def activate(self) -> None:
        ArrRuleDataResolver._ctx.set(self)

    @classmethod
    def current(cls) -> ArrRuleDataResolver | None:
        return cls._ctx.get()

    def movie_ids(self, movie_id: int | None) -> list[str] | UnavailableRuleValue:
        if movie_id is None:
            return []
        return self._movie_ids_by_movie_id.get(movie_id, [])

    def series_ids(self, series_id: int | None) -> list[str] | UnavailableRuleValue:
        if series_id is None:
            return []
        return self._series_ids_by_series_id.get(series_id, [])


class FavoritesRuleDataResolver:
    """Holds preloaded favorite/watchlist users keyed by media type and TMDB ID."""

    _ctx: ContextVar[FavoritesRuleDataResolver | None] = ContextVar(
        "favorites_rule_data_resolver", default=None
    )

    __slots__ = ("_usernames_by_key",)

    def __init__(
        self,
        usernames_by_key: Mapping[tuple[MediaType, int], Iterable[str]] | None = None,
    ) -> None:
        self._usernames_by_key = {
            key: sorted(
                {normalized for value in values if (normalized := str(value).strip())},
                key=str.casefold,
            )
            for key, values in (usernames_by_key or {}).items()
        }

    def activate(self) -> None:
        FavoritesRuleDataResolver._ctx.set(self)

    @classmethod
    def current(cls) -> FavoritesRuleDataResolver | None:
        return cls._ctx.get()

    def usernames(self, media_type: MediaType, tmdb_id: int | None) -> list[str]:
        if tmdb_id is None:
            return []
        return self._usernames_by_key.get((media_type, tmdb_id), [])


class RankRuleDataResolver:
    """Holds precomputed newest-first retention ranks for seasons and episodes."""

    _ctx: ContextVar[RankRuleDataResolver | None] = ContextVar(
        "rank_rule_data_resolver", default=None
    )

    __slots__ = ("_season_rank_by_id", "_episode_rank_by_id")

    def __init__(
        self,
        *,
        season_rank_by_id: Mapping[int, int] | None = None,
        episode_rank_by_id: Mapping[int, int] | None = None,
    ) -> None:
        self._season_rank_by_id = dict(season_rank_by_id or {})
        self._episode_rank_by_id = dict(episode_rank_by_id or {})

    def activate(self) -> None:
        RankRuleDataResolver._ctx.set(self)

    @classmethod
    def current(cls) -> RankRuleDataResolver | None:
        return cls._ctx.get()

    def season_rank(self, season_id: int | None) -> int | UnavailableRuleValue:
        if season_id is None:
            return RULE_VALUE_UNAVAILABLE
        return self._season_rank_by_id.get(season_id, RULE_VALUE_UNAVAILABLE)

    def episode_rank(self, episode_id: int | None) -> int | UnavailableRuleValue:
        if episode_id is None:
            return RULE_VALUE_UNAVAILABLE
        return self._episode_rank_by_id.get(episode_id, RULE_VALUE_UNAVAILABLE)


class CollectionSiblingRuleDataResolver:
    """Holds collection sibling watch activity by movie id."""

    _ctx: ContextVar[CollectionSiblingRuleDataResolver | None] = ContextVar(
        "collection_sibling_rule_data_resolver", default=None
    )

    __slots__ = ("_latest_by_movie_id",)

    def __init__(
        self,
        latest_watched_by_movie_id: Mapping[int, datetime | None] | None = None,
    ) -> None:
        self._latest_by_movie_id = dict(latest_watched_by_movie_id or {})

    def activate(self) -> None:
        CollectionSiblingRuleDataResolver._ctx.set(self)

    @classmethod
    def current(cls) -> CollectionSiblingRuleDataResolver | None:
        return cls._ctx.get()

    def latest(self, movie_id: int | None) -> datetime | None | UnavailableRuleValue:
        if movie_id is None:
            return RULE_VALUE_UNAVAILABLE
        return self._latest_by_movie_id.get(movie_id)


class SeerrRequestResolver:
    """Holds pre fetched Seerr request state for one scan run.

    State is keyed by ``(media_type, tmdb_id)`` and values are requester id sets.
    """

    _ctx: ContextVar[SeerrRequestResolver | None] = ContextVar(
        "seerr_request_resolver", default=None
    )

    __slots__ = (
        "_latest_active_request_at_by_key",
        "_latest_active_request_at_by_target",
        "_requester_ids_by_key",
        "_requester_ids_by_target",
        "_requester_has_watched_by_key",
        "_requester_has_watched_by_target",
    )

    def __init__(
        self,
        requester_ids_by_key: Mapping[tuple[MediaType, int], Iterable[int]]
        | None = None,
        requester_has_watched_by_key: Mapping[tuple[MediaType, int], bool]
        | None = None,
        requester_has_watched_by_target: Mapping[
            tuple[str, int, int | None, int | None], bool
        ]
        | None = None,
        latest_active_request_at_by_key: Mapping[tuple[MediaType, int], datetime]
        | None = None,
        requester_ids_by_target: Mapping[tuple[str, int, int | None], Iterable[int]]
        | None = None,
        latest_active_request_at_by_target: Mapping[
            tuple[str, int, int | None], datetime
        ]
        | None = None,
    ):
        self._requester_ids_by_key: dict[tuple[MediaType, int], set[int]] = {}
        for key, user_ids in (requester_ids_by_key or {}).items():
            self._requester_ids_by_key[key] = {int(v) for v in user_ids}
        self._requester_ids_by_target = {
            key: {int(value) for value in values}
            for key, values in (requester_ids_by_target or {}).items()
        }
        self._requester_has_watched_by_key: dict[tuple[MediaType, int], bool] = {
            key: bool(value)
            for key, value in (requester_has_watched_by_key or {}).items()
        }
        self._requester_has_watched_by_target = dict(
            requester_has_watched_by_target or {}
        )
        self._latest_active_request_at_by_key = dict(
            latest_active_request_at_by_key or {}
        )
        self._latest_active_request_at_by_target = dict(
            latest_active_request_at_by_target or {}
        )

    def activate(self) -> None:
        """Install this resolver for the current async context."""
        SeerrRequestResolver._ctx.set(self)

    @classmethod
    def current(cls) -> SeerrRequestResolver | None:
        """Return the resolver active in the current async context, or None."""
        return cls._ctx.get()

    def resolve_requester_ids(
        self,
        media_type: MediaType,
        tmdb_id: int | None,
        *,
        target_scope: str | None = None,
        season_number: int | None = None,
    ) -> list[int] | None:
        """Return Seerr requester IDs for the given media key if known."""
        if tmdb_id is None:
            return None
        ids: set[int] | None
        if media_type is MediaType.SERIES and target_scope in {
            TARGET_SEASON,
            TARGET_EPISODE,
        }:
            target_key = (target_scope, tmdb_id, season_number)
            if target_key in self._requester_ids_by_target:
                ids = self._requester_ids_by_target[target_key]
            else:
                ids = self._requester_ids_by_key.get((media_type, tmdb_id))
        else:
            ids = self._requester_ids_by_key.get((media_type, tmdb_id))
        if ids is None:
            return None
        return sorted(ids)

    def resolve(
        self,
        media_type: MediaType,
        tmdb_id: int | None,
        *,
        target_scope: str | None = None,
        season_number: int | None = None,
    ) -> bool | None:
        """Return Seerr requested state for the given media key if known."""
        requester_ids = self.resolve_requester_ids(
            media_type,
            tmdb_id,
            target_scope=target_scope,
            season_number=season_number,
        )
        if requester_ids is None:
            return None
        return bool(requester_ids)

    def resolve_requester_has_watched(
        self,
        media_type: MediaType,
        tmdb_id: int | None,
        *,
        target_scope: str | None = None,
        season_number: int | None = None,
        episode_number: int | None = None,
    ) -> bool | None:
        """Return requester watched state for the given media key if known."""
        if tmdb_id is None:
            return None
        if media_type is MediaType.SERIES and target_scope is not None:
            value = self._requester_has_watched_by_target.get(
                (target_scope, tmdb_id, season_number, episode_number)
            )
        else:
            value = self._requester_has_watched_by_key.get((media_type, tmdb_id))
        if value is None:
            return None
        return bool(value)

    def resolve_latest_active_request_at(
        self,
        media_type: MediaType,
        tmdb_id: int | None,
        *,
        target_scope: str | None = None,
        season_number: int | None = None,
    ) -> datetime | None:
        """Return the newest pending or approved Seerr request timestamp."""
        if tmdb_id is None:
            return None
        if media_type is MediaType.SERIES and target_scope in {
            TARGET_SEASON,
            TARGET_EPISODE,
        }:
            target_key = (target_scope, tmdb_id, season_number)
            if target_key in self._requester_ids_by_target:
                return self._latest_active_request_at_by_target.get(target_key)
        return self._latest_active_request_at_by_key.get((media_type, tmdb_id))


class SonarrRuleDataResolver:
    """Holds Sonarr-derived values for one rule evaluation run."""

    _ctx: ContextVar[SonarrRuleDataResolver | None] = ContextVar(
        "sonarr_rule_data_resolver", default=None
    )

    __slots__ = ("_values_by_series_id",)

    def __init__(
        self,
        values_by_series_id: Mapping[int, Mapping[str, object]] | None = None,
    ) -> None:
        self._values_by_series_id: dict[int, dict[str, object]] = {
            int(series_id): dict(values)
            for series_id, values in (values_by_series_id or {}).items()
        }

    def activate(self) -> None:
        """Install this resolver for the current async context."""
        SonarrRuleDataResolver._ctx.set(self)

    @classmethod
    def current(cls) -> SonarrRuleDataResolver | None:
        """Return the resolver active in the current async context, or None."""
        return cls._ctx.get()

    def resolve(self, series_id: int | None, field: str) -> object:
        """Return a Sonarr field value or the unavailable sentinel."""
        if series_id is None:
            return RULE_VALUE_UNAVAILABLE
        return self._values_by_series_id.get(series_id, {}).get(
            field, RULE_VALUE_UNAVAILABLE
        )


class PlaybackHistoryResolver:
    """Holds provider-neutral playback aggregates for one rule evaluation run."""

    _ctx: ContextVar[PlaybackHistoryResolver | None] = ContextVar(
        "playback_history_resolver", default=None
    )

    __slots__ = ("_values_by_target",)

    def __init__(
        self,
        values_by_target: Mapping[tuple[str, int], Mapping[str, object]] | None = None,
    ) -> None:
        self._values_by_target = {
            (str(scope), int(target_id)): dict(values)
            for (scope, target_id), values in (values_by_target or {}).items()
        }

    def activate(self) -> None:
        PlaybackHistoryResolver._ctx.set(self)

    @classmethod
    def current(cls) -> PlaybackHistoryResolver | None:
        return cls._ctx.get()

    def resolve(self, target_scope: str, target_id: int | None, field: str) -> object:
        if target_id is None:
            return RULE_VALUE_UNAVAILABLE
        return self._values_by_target.get((target_scope, target_id), {}).get(
            field, RULE_VALUE_UNAVAILABLE
        )


def _playback_context(
    resolver: PlaybackHistoryResolver | None,
    target_scope: str,
    target_id: int | None,
) -> dict[str, object]:
    return {
        field: (
            resolver.resolve(target_scope, target_id, field)
            if resolver
            else RULE_VALUE_UNAVAILABLE
        )
        for field in PLAYBACK_RULE_FIELDS
    }


def normalize_rule_target(rule: ReclaimRule) -> str:
    """Normalize the target scope of a rule, defaulting to movie version or series based on media
    type if not explicitly set or invalid."""
    if rule.target_scope in VALID_TARGET_SCOPES:
        return rule.target_scope
    return TARGET_MOVIE_VERSION if rule.media_type is MediaType.MOVIE else TARGET_SERIES


def normalize_rule_definition(rule: ReclaimRule) -> RuleDefinition | None:
    """Normalize the rule definition, ensuring it is a valid structure with a root group.
    Returns the normalized definition or None if invalid."""
    if _has_valid_definition(rule.definition):
        return rule.definition
    return None


def validate_rule_definition(
    definition: RuleDefinition | None, *, target_scope: str | None = None
) -> None:
    """Validate the structure of a rule definition, ensuring it includes a root group and valid nodes."""
    if not definition:
        raise ValueError("Rule definition is required")
    if not _has_valid_definition(definition):
        raise ValueError("Rule definition must include a root group")
    _validate_node(definition["root"])
    if target_scope is not None:
        _validate_scope_fields(definition, target_scope)


def collect_rule_conditions(
    definition: RuleDefinition | None, *, field: str | None = None
) -> list[dict[str, Any]]:
    """Collect all condition nodes from a rule definition, optionally filtering by field."""
    if not _has_valid_definition(definition):
        return []
    root = definition.get("root") if definition else None
    if not isinstance(root, dict):
        return []
    return list(_iter_condition_nodes(root, field=field))


def collect_rule_path_conditions(
    definition: RuleDefinition | None,
) -> list[dict[str, str]]:
    """Collect normalized path/filename conditions from a rule definition.

    Returns a deduplicated list of objects with ``field``, ``operator``, and ``value``.
    Valueless operators are skipped.
    """
    seen: set[tuple[str, str, str]] = set()
    conditions: list[dict[str, str]] = []
    for condition in collect_rule_conditions(definition):
        field = str(condition.get("field", ""))
        if field not in PATH_FIELDS:
            continue
        operator = str(condition.get("operator", "")).lower()
        if operator not in PATH_OPERATORS or operator in VALUELESS_OPERATORS:
            continue
        for value in _normalize_condition_values(condition.get("value")):
            key = (field, operator, value)
            if key in seen:
                continue
            seen.add(key)
            conditions.append(
                {
                    "field": field,
                    "operator": operator,
                    "value": value,
                }
            )
    return conditions


def collect_rule_path_patterns(definition: RuleDefinition | None) -> list[str]:
    """Collect all unique media.path values from a rule definition.

    This helper is kept for backward compatibility with older call sites that
    only consume path pattern strings.
    """
    patterns: list[str] = []
    seen: set[str] = set()
    for condition in collect_rule_path_conditions(definition):
        if condition["field"] != "media.path":
            continue
        value = condition["value"]
        if value in seen:
            continue
        seen.add(value)
        patterns.append(value)
    return patterns


def collect_rule_library_ids(definition: RuleDefinition | None) -> list[str]:
    """Collect all unique library ids from library.id conditions in the rule definition."""
    seen: set[str] = set()
    library_ids: list[str] = []
    for condition in collect_rule_conditions(definition, field="library.id"):
        for value in _normalize_condition_values(condition.get("value")):
            if value in seen:
                continue
            seen.add(value)
            library_ids.append(value)
    return library_ids


def derive_path_scope_library_ids(
    definition: RuleDefinition | None,
) -> list[str] | None:
    """Derive the set of library ids that should be considered in scope for path based
    conditions in the rule definition.  Returns a list of library ids if the path conditions
    are compatible with library scoping, or None if the rule definition includes unsupported
    operators or structures that prevent reliable derivation of library scope."""
    conditions = collect_rule_conditions(definition, field="library.id")
    if not conditions:
        return None

    library_ids: list[str] = []
    seen: set[str] = set()
    for condition in conditions:
        operator = str(condition.get("operator", "")).lower()
        if operator in PATH_LIBRARY_UNSUPPORTED_OPERATORS:
            return None
        if operator not in PATH_LIBRARY_INCLUSION_OPERATORS:
            return None
        values = _normalize_condition_values(condition.get("value"))
        if not values:
            return None
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            library_ids.append(value)
    return library_ids or None


def evaluate_advanced_rule(
    rule: ReclaimRule,
    *,
    target_scope: str,
    movie: Movie | None = None,
    version: MovieVersion | None = None,
    series: Series | None = None,
    season: Season | None = None,
    episode: Episode | None = None,
) -> tuple[bool, dict[str, Any], list[dict[str, Any]]]:
    """Evaluate an advanced rule against the provided media context, returning whether it
    matches, the matched field values, and the reasons for the match or failure."""
    definition = normalize_rule_definition(rule)
    if not definition:
        return False, {}, []
    root = definition.get("root")
    if not isinstance(root, dict):
        return False, {}, []

    compute_disk = _rule_uses_disk_fields(definition)
    context = _build_context(
        target_scope, movie, version, series, season, episode, compute_disk
    )
    matched: dict[str, Any] = {}
    reasons: list[dict[str, Any]] = []
    if not _evaluate_node(root, context, matched, reasons):
        return False, {}, []
    return True, matched, reasons


def evaluate_advanced_rule_state(
    rule: ReclaimRule,
    *,
    target_scope: str,
    movie: Movie | None = None,
    version: MovieVersion | None = None,
    series: Series | None = None,
    season: Season | None = None,
    episode: Episode | None = None,
) -> bool | None:
    """Evaluate a rule using three-valued logic for unavailable external data."""
    definition = normalize_rule_definition(rule)
    if not definition:
        return False
    root = definition.get("root")
    if not isinstance(root, dict):
        return False
    context = _build_context(
        target_scope,
        movie,
        version,
        series,
        season,
        episode,
        _rule_uses_disk_fields(definition),
    )
    return _evaluate_node_state(root, context)


def _evaluate_node_state(
    node: dict[str, Any],
    context: dict[str, Any],
) -> bool | None:
    """Return True, False, or None when unavailable values affect the result."""
    if node.get("type") == "group":
        op = str(node.get("op", "")).lower()
        children = node.get("children")
        if (
            op not in {"and", "or"}
            or not isinstance(children, list)
            or not children
            or not all(isinstance(child, dict) for child in children)
        ):
            return False
        child_states = [_evaluate_node_state(child, context) for child in children]
        if op == "and":
            if False in child_states:
                return False
            return None if None in child_states else True
        if True in child_states:
            return True
        return None if None in child_states else False

    if node.get("type") != "condition":
        return False
    field = str(node.get("field", ""))
    actual = context.get(field)
    if actual is RULE_VALUE_UNAVAILABLE:
        return None
    return _matches_operator(
        actual,
        str(node.get("operator", "")),
        node.get("value"),
        field=field,
    )


def _rule_uses_disk_fields(definition: RuleDefinition | None) -> bool:
    """Return True if the rule definition references any disk.* fields."""
    return bool(
        collect_rule_conditions(definition, field="disk.free_bytes")
        or collect_rule_conditions(definition, field="disk.free_percent")
    )


def _collection_names_from_series_refs(refs: Iterable[Any]) -> list[str]:
    names: list[str] = []
    for ref in refs:
        names.extend(ref.media_server_collection_names or [])
    return normalize_name_list(names) or []


def _has_valid_definition(definition: RuleDefinition | None) -> bool:
    """Check if the rule definition has a valid structure with a root group."""
    return isinstance(definition, dict) and isinstance(definition.get("root"), dict)


def _iter_condition_nodes(
    node: dict[str, Any], *, field: str | None = None
) -> Iterator[dict[str, Any]]:
    """Recursively iterate through the rule definition tree, yielding condition nodes
    that match the specified field if provided."""
    if node.get("type") == "condition":
        node_field = str(node.get("field", ""))
        if field is None or node_field == field:
            yield node
        return

    children = node.get("children")
    if not isinstance(children, list):
        return
    for child in children:
        if isinstance(child, dict):
            yield from _iter_condition_nodes(child, field=field)


def _normalize_condition_values(value: Any) -> list[str]:
    """Normalize the values of a condition node, ensuring they are returned as a list of non-empty strings."""
    values = value if isinstance(value, list) else [value]
    return [
        str(item).strip() for item in values if item is not None and str(item).strip()
    ]


def _validate_scope_fields(definition: RuleDefinition, target_scope: str) -> None:
    """Validate that all condition fields are available for the selected target scope."""
    normalized_scope = str(target_scope).strip().lower()
    if normalized_scope not in VALID_TARGET_SCOPES:
        raise ValueError(f"Unsupported target_scope '{target_scope}'")

    allowed_fields = TARGET_SCOPE_ALLOWED_FIELDS.get(normalized_scope, set())
    invalid_fields: set[str] = set()
    for condition in collect_rule_conditions(definition):
        field = str(condition.get("field", ""))
        if field not in allowed_fields:
            invalid_fields.add(field)

    if invalid_fields:
        sorted_invalid = ", ".join(f"'{field}'" for field in sorted(invalid_fields))
        raise ValueError(
            "Rule field(s) not available for target_scope "
            f"'{normalized_scope}': {sorted_invalid}"
        )


def _validate_node(node: dict[str, Any]) -> None:
    """Validate the structure and content of a rule node."""
    node_type = node.get("type")
    if node_type == "group":
        op = str(node.get("op", "")).lower()
        if op not in {"and", "or"}:
            raise ValueError("Rule group operator must be AND or OR")
        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise ValueError("Rule group must include at least one condition")
        for child in children:
            if not isinstance(child, dict):
                raise ValueError("Rule group child must be an object")
            _validate_node(child)
        return

    if node_type != "condition":
        raise ValueError("Rule node must be a group or condition")
    field = str(node.get("field", ""))
    operator = str(node.get("operator", ""))
    if field not in FIELD_LABELS:
        raise ValueError(f"Unsupported rule field: {node.get('field')}")
    if operator not in OPERATOR_LABELS:
        raise ValueError(f"Unsupported rule operator: {node.get('operator')}")
    allowed_operators = FIELD_ALLOWED_OPERATORS.get(field)
    if not allowed_operators or operator not in allowed_operators:
        raise ValueError(f"Unsupported rule operator '{operator}' for field '{field}'")
    if operator not in VALUELESS_OPERATORS and "value" not in node:
        raise ValueError("Rule condition requires a value")
    if field == "library.id" and operator in LIST_OPERATORS:
        raw_values = node.get("value")
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        normalized = [str(value).strip() for value in values if value is not None]
        if not any(normalized):
            raise ValueError("Library conditions require at least one library id")


def _build_context(
    target_scope: str,
    movie: Movie | None,
    version: MovieVersion | None,
    series: Series | None,
    season: Season | None,
    episode: Episode | None = None,
    compute_disk: bool = True,
) -> dict[str, Any]:
    """Build the context dictionary for evaluating a rule against a specific target scope."""
    now = datetime.now(UTC)
    _resolver = DiskStatsResolver.current() if compute_disk else None
    _seerr_resolver = SeerrRequestResolver.current()
    _sonarr_resolver = SonarrRuleDataResolver.current()
    _playback_resolver = PlaybackHistoryResolver.current()
    _arr_resolver = ArrRuleDataResolver.current()
    _favorites_resolver = FavoritesRuleDataResolver.current()
    _rank_resolver = RankRuleDataResolver.current()
    _collection_resolver = CollectionSiblingRuleDataResolver.current()
    if target_scope == TARGET_MOVIE_VERSION and movie and version:
        size = version.size if version.size and version.size > 0 else movie.size
        _disk = (
            _resolver.resolve(version.path) if (_resolver and version.path) else None
        )
        _added = version.added_at or movie.added_at
        _last_viewed = _effective_last_viewed(movie.last_viewed_at, _added)
        _file_name = version.file_name or _path_basename(version.path)
        _favorite_users = (
            _favorites_resolver.usernames(MediaType.MOVIE, movie.tmdb_id)
            if _favorites_resolver
            else RULE_VALUE_UNAVAILABLE
        )
        _favorite_user_count = (
            len(_favorite_users)
            if isinstance(_favorite_users, list)
            else RULE_VALUE_UNAVAILABLE
        )
        _collection_latest = (
            _collection_resolver.latest(movie.id)
            if _collection_resolver
            else RULE_VALUE_UNAVAILABLE
        )
        _collection_days_since_latest = (
            _days_between(_collection_latest, now)
            if isinstance(_collection_latest, datetime) or _collection_latest is None
            else RULE_VALUE_UNAVAILABLE
        )
        return {
            "library.id": [version.library_id],
            "media.title": movie.title,
            "media.path": [version.path] if version.path else [],
            "media.file_name": [_file_name] if _file_name else [],
            "media.size": size,
            "media.year": movie.year,
            "media.container": version.container,
            "media_server.collections": version.media_server_collection_names or [],
            "media.days_since_added": _days_between(
                version.added_at or movie.added_at, now
            ),
            "arr.days_since_file_added": _days_between(version.arr_added_at, now),
            "watch.view_count": movie.view_count,
            "watch.last_viewed_at": _last_viewed,
            "watch.days_since_last_watched": _days_between(_last_viewed, now),
            "watch.never_watched": movie.view_count == 0 or _last_viewed is None,
            **_playback_context(_playback_resolver, TARGET_MOVIE_VERSION, version.id),
            "tmdb.release_date": movie.tmdb_release_date,
            "tmdb.in_collection": (
                movie.tmdb_collection_id is not None
                if movie.tmdb_collection_checked
                else None
            ),
            "tmdb.collection_name": movie.tmdb_collection_name,
            "tmdb.genres": normalize_genre_names(movie.genres) or [],
            "tmdb.original_language": normalize_language(movie.original_language),
            "tmdb.origin_country": _normalized_country_list(movie.origin_country),
            "tmdb.runtime_minutes": movie.runtime,
            "tmdb.days_since_release": _days_between(movie.tmdb_release_date, now),
            "tmdb.popularity": movie.popularity,
            "tmdb.vote_average": movie.vote_average,
            "tmdb.vote_count": movie.vote_count,
            "imdb.rating": movie.imdb_rating,
            "imdb.vote_count": movie.imdb_vote_count,
            "anilist.score": movie.anilist_score,
            "anilist.popularity": movie.anilist_popularity,
            "anilist.favourites": movie.anilist_favourites,
            "rottentomatoes.tomato_meter": movie.rottentomatoes_tomato_meter,
            "rottentomatoes.tomato_vote_count": (
                movie.rottentomatoes_tomato_vote_count
            ),
            "rottentomatoes.popcorn_meter": movie.rottentomatoes_popcorn_meter,
            "rottentomatoes.popcorn_vote_count": (
                movie.rottentomatoes_popcorn_vote_count
            ),
            "metacritic.metascore": movie.metacritic_metascore,
            "metacritic.vote_count": movie.metacritic_vote_count,
            "metacritic.user_score": movie.metacritic_user_score,
            "metacritic.user_vote_count": movie.metacritic_user_vote_count,
            "trakt.rating": movie.trakt_rating,
            "trakt.vote_count": movie.trakt_vote_count,
            "letterboxd.score": movie.letterboxd_score,
            "letterboxd.vote_count": movie.letterboxd_vote_count,
            "video.codec_family": version.video_codec_family,
            "audio.codec_family": version.audio_codec_family,
            "video.hdr": version.video_hdr,
            "video.dolby_vision": version.video_dolby_vision,
            "video.width": version.video_width,
            "video.height": version.video_height,
            "video.bitrate_kbps": _bitrate_kbps(version.video_bitrate, version.service),
            "video.bit_depth": version.video_bit_depth,
            "video.resolution": version.video_resolution,
            "audio.channels": version.audio_channels,
            "audio.track_count": version.audio_count,
            "audio.bitrate_kbps": _bitrate_kbps(version.audio_bitrate, version.service),
            "audio.languages": version.audio_languages,
            "subtitle.languages": version.subtitle_languages,
            "subtitle.track_count": version.subtitle_count,
            "subtitle.has_forced": version.subtitle_has_forced,
            "video.color_space": version.video_color_space,
            "video.color_transfer": version.video_color_transfer,
            "video.color_primaries": version.video_color_primaries,
            "media.duration": version.duration,
            "arr.tags": movie.arr_tags or [],
            "arr.movie_ids": (
                _arr_resolver.movie_ids(movie.id)
                if _arr_resolver
                else RULE_VALUE_UNAVAILABLE
            ),
            "arr.monitored": movie.is_monitored,
            "movie.version_count": len(movie.versions or []),
            "media_server.user_rating": (
                version.media_server_user_rating
                if version.media_server_user_rating is not None
                else movie.media_server_user_rating
            ),
            "favorites.exists": (
                bool(_favorite_users)
                if _favorite_users is not RULE_VALUE_UNAVAILABLE
                else RULE_VALUE_UNAVAILABLE
            ),
            "favorites.usernames": _favorite_users,
            "favorites.user_count": (_favorite_user_count),
            "collection.latest_sibling_watched_at": _collection_latest,
            "collection.days_since_latest_sibling_watched": _collection_days_since_latest,
            "seerr.requested": (
                _seerr_resolver.resolve(MediaType.MOVIE, movie.tmdb_id)
                if _seerr_resolver
                else None
            ),
            "seerr.last_requested_at": (
                _seerr_resolver.resolve_latest_active_request_at(
                    MediaType.MOVIE, movie.tmdb_id
                )
                if _seerr_resolver
                else None
            ),
            "seerr.days_since_last_requested": (
                _days_between(
                    _seerr_resolver.resolve_latest_active_request_at(
                        MediaType.MOVIE, movie.tmdb_id
                    ),
                    now,
                )
                if _seerr_resolver
                else None
            ),
            "seerr.requested_by_user_ids": (
                _seerr_resolver.resolve_requester_ids(MediaType.MOVIE, movie.tmdb_id)
                if _seerr_resolver
                else None
            ),
            "seerr.requester_has_watched": (
                _seerr_resolver.resolve_requester_has_watched(
                    MediaType.MOVIE, movie.tmdb_id
                )
                if _seerr_resolver
                else None
            ),
            "disk.free_bytes": _disk[0] if _disk else None,
            "disk.free_percent": _disk[1] if _disk else None,
        }

    if target_scope == TARGET_SERIES and series:
        refs = series.service_refs or []
        _collections = _collection_names_from_series_refs(refs)
        _series_path = next((ref.path for ref in refs if ref.path), None)
        _series_file_names = [
            file_name
            for file_name in (_path_basename(ref.path) for ref in refs)
            if file_name
        ]
        _disk = (
            _resolver.resolve(_series_path) if (_resolver and _series_path) else None
        )
        _last_viewed = _effective_last_viewed(series.last_viewed_at, series.added_at)
        _favorite_users = (
            _favorites_resolver.usernames(MediaType.SERIES, series.tmdb_id)
            if _favorites_resolver
            else RULE_VALUE_UNAVAILABLE
        )
        _favorite_user_count = (
            len(_favorite_users)
            if isinstance(_favorite_users, list)
            else RULE_VALUE_UNAVAILABLE
        )
        return {
            "library.id": [ref.library_id for ref in refs if ref.library_id],
            "media.title": series.title,
            "media.path": [ref.path for ref in refs if ref.path],
            "media.file_name": _series_file_names,
            "media.size": series.size,
            "media.year": series.year,
            "media_server.collections": _collections,
            "media.days_since_added": _days_between(series.added_at, now),
            "arr.days_since_file_added": _days_between(series.arr_added_at, now),
            "watch.view_count": series.view_count,
            "watch.last_viewed_at": _last_viewed,
            "watch.days_since_last_watched": _days_between(_last_viewed, now),
            "watch.never_watched": series.view_count == 0 or _last_viewed is None,
            **_playback_context(_playback_resolver, TARGET_SERIES, series.id),
            "tmdb.first_air_date": series.tmdb_first_air_date,
            "tmdb.last_air_date": series.tmdb_last_air_date,
            "tmdb.days_since_first_air_date": _days_between(
                series.tmdb_first_air_date, now
            ),
            "tmdb.days_since_last_air_date": _days_between(
                series.tmdb_last_air_date, now
            ),
            "tmdb.popularity": series.popularity,
            "tmdb.vote_average": series.vote_average,
            "tmdb.vote_count": series.vote_count,
            "tmdb.genres": normalize_genre_names(series.genres) or [],
            "tmdb.original_language": normalize_language(series.original_language),
            "tmdb.origin_country": _normalized_country_list(series.origin_country),
            "imdb.rating": series.imdb_rating,
            "imdb.vote_count": series.imdb_vote_count,
            "anilist.score": series.anilist_score,
            "anilist.popularity": series.anilist_popularity,
            "anilist.favourites": series.anilist_favourites,
            "rottentomatoes.tomato_meter": series.rottentomatoes_tomato_meter,
            "rottentomatoes.tomato_vote_count": (
                series.rottentomatoes_tomato_vote_count
            ),
            "rottentomatoes.popcorn_meter": series.rottentomatoes_popcorn_meter,
            "rottentomatoes.popcorn_vote_count": (
                series.rottentomatoes_popcorn_vote_count
            ),
            "metacritic.metascore": series.metacritic_metascore,
            "metacritic.vote_count": series.metacritic_vote_count,
            "metacritic.user_score": series.metacritic_user_score,
            "metacritic.user_vote_count": series.metacritic_user_vote_count,
            "trakt.rating": series.trakt_rating,
            "trakt.vote_count": series.trakt_vote_count,
            "letterboxd.score": series.letterboxd_score,
            "letterboxd.vote_count": series.letterboxd_vote_count,
            "series.status": series.status,
            "series.tmdb_season_count": series.season_count,
            "series.library_season_count": _library_season_count(series),
            "video.codec_family": series.video_codec_families,
            "audio.codec_family": series.audio_codec_families,
            "video.hdr": series.has_hdr,
            "video.dolby_vision": series.has_dolby_vision,
            "video.width": series.max_video_width,
            "video.height": series.max_video_height,
            "audio.channels": series.max_audio_channels,
            "subtitle.languages": series.subtitle_languages,
            "arr.tags": series.arr_tags or [],
            "arr.series_ids": (
                _arr_resolver.series_ids(series.id)
                if _arr_resolver
                else RULE_VALUE_UNAVAILABLE
            ),
            "arr.monitored": series.is_monitored,
            "media_server.user_rating": series.media_server_user_rating,
            "favorites.exists": (
                bool(_favorite_users)
                if _favorite_users is not RULE_VALUE_UNAVAILABLE
                else RULE_VALUE_UNAVAILABLE
            ),
            "favorites.usernames": _favorite_users,
            "favorites.user_count": (_favorite_user_count),
            "sonarr.latest_season_has_unaired_episodes": (
                _sonarr_resolver.resolve(
                    series.id, "sonarr.latest_season_has_unaired_episodes"
                )
                if _sonarr_resolver
                else RULE_VALUE_UNAVAILABLE
            ),
            "sonarr.latest_season_has_finale": (
                _sonarr_resolver.resolve(series.id, "sonarr.latest_season_has_finale")
                if _sonarr_resolver
                else RULE_VALUE_UNAVAILABLE
            ),
            "sonarr.series_status": (
                _sonarr_resolver.resolve(series.id, "sonarr.series_status")
                if _sonarr_resolver
                else RULE_VALUE_UNAVAILABLE
            ),
            "seerr.requested": (
                _seerr_resolver.resolve(MediaType.SERIES, series.tmdb_id)
                if _seerr_resolver
                else None
            ),
            "seerr.last_requested_at": (
                _seerr_resolver.resolve_latest_active_request_at(
                    MediaType.SERIES, series.tmdb_id
                )
                if _seerr_resolver
                else None
            ),
            "seerr.days_since_last_requested": (
                _days_between(
                    _seerr_resolver.resolve_latest_active_request_at(
                        MediaType.SERIES, series.tmdb_id
                    ),
                    now,
                )
                if _seerr_resolver
                else None
            ),
            "seerr.requested_by_user_ids": (
                _seerr_resolver.resolve_requester_ids(MediaType.SERIES, series.tmdb_id)
                if _seerr_resolver
                else None
            ),
            "seerr.requester_has_watched": (
                _seerr_resolver.resolve_requester_has_watched(
                    MediaType.SERIES,
                    series.tmdb_id,
                    target_scope=TARGET_SERIES,
                )
                if _seerr_resolver
                else None
            ),
            "disk.free_bytes": _disk[0] if _disk else None,
            "disk.free_percent": _disk[1] if _disk else None,
        }

    if target_scope == TARGET_SEASON and series and season:
        refs = series.service_refs or []
        _collections = _collection_names_from_series_refs(refs)
        _season_file_name = _path_basename(season.path)
        non_special_nums = sorted(
            s.season_number for s in (series.seasons or []) if s.season_number > 0
        )
        season_fully_watched, season_watched_percent = _season_watch_progress(season)
        max_season = non_special_nums[-1] if non_special_nums else 0
        if season.season_number > 0 and season.season_number in non_special_nums:
            seasons_from_latest: int | None = (
                len(non_special_nums) - 1 - non_special_nums.index(season.season_number)
            )
            is_latest_season = season.season_number == max_season
        else:
            seasons_from_latest = None
            is_latest_season = False
        _disk = _resolver.resolve(season.path) if (_resolver and season.path) else None
        _last_viewed = _effective_last_viewed(season.last_viewed_at, season.added_at)
        _favorite_users = (
            _favorites_resolver.usernames(MediaType.SERIES, series.tmdb_id)
            if _favorites_resolver
            else RULE_VALUE_UNAVAILABLE
        )
        _favorite_user_count = (
            len(_favorite_users)
            if isinstance(_favorite_users, list)
            else RULE_VALUE_UNAVAILABLE
        )
        return {
            "library.id": [ref.library_id for ref in refs if ref.library_id],
            "media.title": series.title,
            "media.path": [ref.path for ref in refs if ref.path],
            "media.file_name": [_season_file_name] if _season_file_name else [],
            "media.size": season.size,
            "media.year": series.year,
            "media_server.collections": _collections,
            "media.days_since_added": _days_between(season.added_at, now),
            "arr.days_since_file_added": _days_between(season.arr_added_at, now),
            "watch.view_count": season.view_count,
            "watch.last_viewed_at": _last_viewed,
            "watch.days_since_last_watched": _days_between(_last_viewed, now),
            "watch.never_watched": (season.view_count or 0) == 0
            or _last_viewed is None,
            **_playback_context(_playback_resolver, TARGET_SEASON, season.id),
            "season.air_date": season.air_date,
            "season.days_since_air_date": _days_between(season.air_date, now),
            "season.season_number": season.season_number,
            "season.episode_count": season.episode_count,
            "season.fully_watched": (
                season_fully_watched
                if season_fully_watched is not None
                else RULE_VALUE_UNAVAILABLE
            ),
            "season.watched_percent": (
                season_watched_percent
                if season_watched_percent is not None
                else RULE_VALUE_UNAVAILABLE
            ),
            "season.is_latest_season": is_latest_season,
            "season.seasons_from_latest": seasons_from_latest,
            "season.position_by_air_date": (
                _rank_resolver.season_rank(season.id)
                if _rank_resolver
                else RULE_VALUE_UNAVAILABLE
            ),
            "tmdb.first_air_date": series.tmdb_first_air_date,
            "tmdb.last_air_date": series.tmdb_last_air_date,
            "tmdb.days_since_first_air_date": _days_between(
                series.tmdb_first_air_date, now
            ),
            "tmdb.days_since_last_air_date": _days_between(
                series.tmdb_last_air_date, now
            ),
            "tmdb.popularity": series.popularity,
            "tmdb.vote_average": series.vote_average,
            "tmdb.vote_count": series.vote_count,
            "tmdb.genres": normalize_genre_names(series.genres) or [],
            "tmdb.original_language": normalize_language(series.original_language),
            "tmdb.origin_country": _normalized_country_list(series.origin_country),
            "imdb.rating": series.imdb_rating,
            "imdb.vote_count": series.imdb_vote_count,
            "anilist.score": series.anilist_score,
            "anilist.popularity": series.anilist_popularity,
            "anilist.favourites": series.anilist_favourites,
            "rottentomatoes.tomato_meter": series.rottentomatoes_tomato_meter,
            "rottentomatoes.tomato_vote_count": (
                series.rottentomatoes_tomato_vote_count
            ),
            "rottentomatoes.popcorn_meter": series.rottentomatoes_popcorn_meter,
            "rottentomatoes.popcorn_vote_count": (
                series.rottentomatoes_popcorn_vote_count
            ),
            "metacritic.metascore": series.metacritic_metascore,
            "metacritic.vote_count": series.metacritic_vote_count,
            "metacritic.user_score": series.metacritic_user_score,
            "metacritic.user_vote_count": series.metacritic_user_vote_count,
            "trakt.rating": series.trakt_rating,
            "trakt.vote_count": series.trakt_vote_count,
            "letterboxd.score": series.letterboxd_score,
            "letterboxd.vote_count": series.letterboxd_vote_count,
            "series.status": series.status,
            "series.tmdb_season_count": series.season_count,
            "series.library_season_count": _library_season_count(series),
            "video.codec_family": season.video_codec_families,
            "audio.codec_family": season.audio_codec_families,
            "video.hdr": season.has_hdr,
            "video.dolby_vision": season.has_dolby_vision,
            "video.width": season.max_video_width,
            "video.height": season.max_video_height,
            "audio.channels": season.max_audio_channels,
            "audio.languages": season.audio_languages,
            "subtitle.languages": season.subtitle_languages,
            "arr.tags": series.arr_tags or [],
            "arr.series_ids": (
                _arr_resolver.series_ids(series.id)
                if _arr_resolver
                else RULE_VALUE_UNAVAILABLE
            ),
            "arr.monitored": season.is_monitored,
            "media_server.user_rating": (
                season.media_server_user_rating
                if season.media_server_user_rating is not None
                else series.media_server_user_rating
            ),
            "favorites.exists": (
                bool(_favorite_users)
                if _favorite_users is not RULE_VALUE_UNAVAILABLE
                else RULE_VALUE_UNAVAILABLE
            ),
            "favorites.usernames": _favorite_users,
            "favorites.user_count": (_favorite_user_count),
            "sonarr.series_status": (
                _sonarr_resolver.resolve(series.id, "sonarr.series_status")
                if _sonarr_resolver
                else RULE_VALUE_UNAVAILABLE
            ),
            "seerr.requested": (
                _seerr_resolver.resolve(
                    MediaType.SERIES,
                    series.tmdb_id,
                    target_scope=TARGET_SEASON,
                    season_number=season.season_number,
                )
                if _seerr_resolver
                else None
            ),
            "seerr.last_requested_at": (
                _seerr_resolver.resolve_latest_active_request_at(
                    MediaType.SERIES,
                    series.tmdb_id,
                    target_scope=TARGET_SEASON,
                    season_number=season.season_number,
                )
                if _seerr_resolver
                else None
            ),
            "seerr.days_since_last_requested": (
                _days_between(
                    _seerr_resolver.resolve_latest_active_request_at(
                        MediaType.SERIES,
                        series.tmdb_id,
                        target_scope=TARGET_SEASON,
                        season_number=season.season_number,
                    ),
                    now,
                )
                if _seerr_resolver
                else None
            ),
            "seerr.requested_by_user_ids": (
                _seerr_resolver.resolve_requester_ids(
                    MediaType.SERIES,
                    series.tmdb_id,
                    target_scope=TARGET_SEASON,
                    season_number=season.season_number,
                )
                if _seerr_resolver
                else None
            ),
            "seerr.requester_has_watched": (
                _seerr_resolver.resolve_requester_has_watched(
                    MediaType.SERIES,
                    series.tmdb_id,
                    target_scope=TARGET_SEASON,
                    season_number=season.season_number,
                )
                if _seerr_resolver
                else None
            ),
            "disk.free_bytes": _disk[0] if _disk else None,
            "disk.free_percent": _disk[1] if _disk else None,
        }

    if target_scope == TARGET_EPISODE and series and season and episode:
        refs = series.service_refs or []
        _collections = _collection_names_from_series_refs(refs)
        _episode_file_name = _path_basename(episode.path)
        season_fully_watched_ep, season_watched_percent_ep = _season_watch_progress(
            season
        )
        non_special_nums_ep = sorted(
            s.season_number for s in (series.seasons or []) if s.season_number > 0
        )
        max_season_ep = non_special_nums_ep[-1] if non_special_nums_ep else 0
        if season.season_number > 0 and season.season_number in non_special_nums_ep:
            seasons_from_latest_ep: int | None = (
                len(non_special_nums_ep)
                - 1
                - non_special_nums_ep.index(season.season_number)
            )
            is_latest_season_ep = season.season_number == max_season_ep
        else:
            seasons_from_latest_ep = None
            is_latest_season_ep = False
        _disk = (
            _resolver.resolve(episode.path) if (_resolver and episode.path) else None
        )
        _last_viewed_ep = _effective_last_viewed(
            episode.last_viewed_at, season.added_at
        )
        _favorite_users = (
            _favorites_resolver.usernames(MediaType.SERIES, series.tmdb_id)
            if _favorites_resolver
            else RULE_VALUE_UNAVAILABLE
        )
        _favorite_user_count = (
            len(_favorite_users)
            if isinstance(_favorite_users, list)
            else RULE_VALUE_UNAVAILABLE
        )
        return {
            "library.id": [ref.library_id for ref in refs if ref.library_id],
            "media.title": series.title,
            "media.path": [episode.path] if episode.path else [],
            "media.file_name": [_episode_file_name] if _episode_file_name else [],
            "media.size": episode.size,
            "media.year": series.year,
            "media_server.collections": _collections,
            "media.days_since_added": _days_between(season.added_at, now),
            "arr.days_since_file_added": _days_between(episode.arr_added_at, now),
            "watch.view_count": episode.view_count,
            "watch.last_viewed_at": _last_viewed_ep,
            "watch.days_since_last_watched": _days_between(_last_viewed_ep, now),
            "watch.never_watched": episode.view_count == 0 or _last_viewed_ep is None,
            **_playback_context(_playback_resolver, TARGET_EPISODE, episode.id),
            "episode.number": episode.episode_number,
            "episode.position_by_air_date": (
                _rank_resolver.episode_rank(episode.id)
                if _rank_resolver
                else RULE_VALUE_UNAVAILABLE
            ),
            "episode.season_number": season.season_number,
            "episode.air_date": episode.air_date,
            "episode.days_since_air_date": _days_between(episode.air_date, now),
            "season.season_number": season.season_number,
            "season.episode_count": season.episode_count,
            "season.fully_watched": (
                season_fully_watched_ep
                if season_fully_watched_ep is not None
                else RULE_VALUE_UNAVAILABLE
            ),
            "season.watched_percent": (
                season_watched_percent_ep
                if season_watched_percent_ep is not None
                else RULE_VALUE_UNAVAILABLE
            ),
            "season.is_latest_season": is_latest_season_ep,
            "season.seasons_from_latest": seasons_from_latest_ep,
            "season.position_by_air_date": (
                _rank_resolver.season_rank(season.id)
                if _rank_resolver
                else RULE_VALUE_UNAVAILABLE
            ),
            "season.air_date": season.air_date,
            "season.days_since_air_date": _days_between(season.air_date, now),
            "tmdb.first_air_date": series.tmdb_first_air_date,
            "tmdb.last_air_date": series.tmdb_last_air_date,
            "tmdb.days_since_first_air_date": _days_between(
                series.tmdb_first_air_date, now
            ),
            "tmdb.days_since_last_air_date": _days_between(
                series.tmdb_last_air_date, now
            ),
            "tmdb.popularity": series.popularity,
            "tmdb.vote_average": series.vote_average,
            "tmdb.vote_count": series.vote_count,
            "tmdb.genres": normalize_genre_names(series.genres) or [],
            "tmdb.original_language": normalize_language(series.original_language),
            "tmdb.origin_country": _normalized_country_list(series.origin_country),
            "imdb.rating": series.imdb_rating,
            "imdb.vote_count": series.imdb_vote_count,
            "anilist.score": series.anilist_score,
            "anilist.popularity": series.anilist_popularity,
            "anilist.favourites": series.anilist_favourites,
            "rottentomatoes.tomato_meter": series.rottentomatoes_tomato_meter,
            "rottentomatoes.tomato_vote_count": (
                series.rottentomatoes_tomato_vote_count
            ),
            "rottentomatoes.popcorn_meter": series.rottentomatoes_popcorn_meter,
            "rottentomatoes.popcorn_vote_count": (
                series.rottentomatoes_popcorn_vote_count
            ),
            "metacritic.metascore": series.metacritic_metascore,
            "metacritic.vote_count": series.metacritic_vote_count,
            "metacritic.user_score": series.metacritic_user_score,
            "metacritic.user_vote_count": series.metacritic_user_vote_count,
            "trakt.rating": series.trakt_rating,
            "trakt.vote_count": series.trakt_vote_count,
            "letterboxd.score": series.letterboxd_score,
            "letterboxd.vote_count": series.letterboxd_vote_count,
            "series.status": series.status,
            "series.tmdb_season_count": series.season_count,
            "series.library_season_count": _library_season_count(series),
            "arr.tags": series.arr_tags or [],
            "arr.series_ids": (
                _arr_resolver.series_ids(series.id)
                if _arr_resolver
                else RULE_VALUE_UNAVAILABLE
            ),
            "arr.monitored": season.is_monitored,
            "media_server.user_rating": (
                episode.media_server_user_rating
                if episode.media_server_user_rating is not None
                else season.media_server_user_rating
                if season.media_server_user_rating is not None
                else series.media_server_user_rating
            ),
            "favorites.exists": (
                bool(_favorite_users)
                if _favorite_users is not RULE_VALUE_UNAVAILABLE
                else RULE_VALUE_UNAVAILABLE
            ),
            "favorites.usernames": _favorite_users,
            "favorites.user_count": (_favorite_user_count),
            "sonarr.series_status": (
                _sonarr_resolver.resolve(series.id, "sonarr.series_status")
                if _sonarr_resolver
                else RULE_VALUE_UNAVAILABLE
            ),
            "seerr.requested": (
                _seerr_resolver.resolve(
                    MediaType.SERIES,
                    series.tmdb_id,
                    target_scope=TARGET_EPISODE,
                    season_number=season.season_number,
                )
                if _seerr_resolver
                else None
            ),
            "seerr.last_requested_at": (
                _seerr_resolver.resolve_latest_active_request_at(
                    MediaType.SERIES,
                    series.tmdb_id,
                    target_scope=TARGET_EPISODE,
                    season_number=season.season_number,
                )
                if _seerr_resolver
                else None
            ),
            "seerr.days_since_last_requested": (
                _days_between(
                    _seerr_resolver.resolve_latest_active_request_at(
                        MediaType.SERIES,
                        series.tmdb_id,
                        target_scope=TARGET_EPISODE,
                        season_number=season.season_number,
                    ),
                    now,
                )
                if _seerr_resolver
                else None
            ),
            "seerr.requested_by_user_ids": (
                _seerr_resolver.resolve_requester_ids(
                    MediaType.SERIES,
                    series.tmdb_id,
                    target_scope=TARGET_EPISODE,
                    season_number=season.season_number,
                )
                if _seerr_resolver
                else None
            ),
            "seerr.requester_has_watched": (
                _seerr_resolver.resolve_requester_has_watched(
                    MediaType.SERIES,
                    series.tmdb_id,
                    target_scope=TARGET_EPISODE,
                    season_number=season.season_number,
                    episode_number=episode.episode_number,
                )
                if _seerr_resolver
                else None
            ),
            "disk.free_bytes": _disk[0] if _disk else None,
            "disk.free_percent": _disk[1] if _disk else None,
        }

    return {}


def _evaluate_node(
    node: dict[str, Any],
    context: dict[str, Any],
    matched: dict[str, Any],
    reasons: list[dict[str, Any]],
) -> bool:
    """Recursively evaluate a rule node against the provided context, updating the matched
    fields and reasons for the evaluation.
    """
    if node.get("type") == "group":
        op = str(node.get("op", "")).lower()
        if op not in {"and", "or"}:
            return False
        children = node.get("children")
        if not isinstance(children, list) or not children:
            return False
        if not all(isinstance(child, dict) for child in children):
            return False
        if op == "or":
            branch_matches: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
            for child in children:
                child_matched: dict[str, Any] = {}
                child_reasons: list[dict[str, Any]] = []
                if _evaluate_node(child, context, child_matched, child_reasons):
                    branch_matches.append((child_matched, child_reasons))
            if not branch_matches:
                return False
            for child_matched, child_reasons in branch_matches:
                matched.update(child_matched)
                reasons.extend(child_reasons)
            return True

        for child in children:
            if not _evaluate_node(child, context, matched, reasons):
                return False
        return True

    if node.get("type") != "condition":
        return False

    return _evaluate_condition(node, context, matched, reasons)


def _evaluate_condition(
    condition: dict[str, Any],
    context: dict[str, Any],
    matched: dict[str, Any],
    reasons: list[dict[str, Any]],
) -> bool:
    """Evaluate a single condition node against the provided context, updating the matched
    fields and reasons for the evaluation.
    """
    field = str(condition.get("field", ""))
    operator = str(condition.get("operator", ""))
    expected = condition.get("value")
    actual = context.get(field)
    if not _matches_operator(actual, operator, expected, field=field):
        return False
    matched[field] = actual.isoformat() if isinstance(actual, datetime) else actual
    reasons.append(_build_reason_condition(field, operator, expected, actual))
    return True


def _matches_operator(
    actual: Any, operator: str, expected: Any, *, field: str | None = None
) -> bool:
    """Evaluate a single condition operator against the provided actual and expected values."""
    if actual is RULE_VALUE_UNAVAILABLE:
        return False
    if operator == "exists":
        if field in LANGUAGE_FIELDS:
            return bool(_normalized_language_values(actual))
        return _exists(actual)
    if operator == "not_exists":
        if field in LANGUAGE_FIELDS:
            return not _normalized_language_values(actual)
        return not _exists(actual)
    if operator == "is_true":
        return actual is True
    if operator == "is_false":
        return actual is False
    if operator in REGEX_OPERATORS:
        matched = _matches_any_regex(_as_list(actual), _as_list(expected), field=field)
        if matched is None:  # no valid pattern: fail closed for both operators
            return False
        return matched if operator == "matches_any_regex" else not matched
    if operator in LIST_OPERATORS:
        return _matches_list_operator(actual, operator, expected, field=field)
    if operator in TAG_SUBSTRING_OPERATORS:
        needles = [_normalize(item) for item in _as_list(expected) if _exists(item)]
        if not needles:
            return False
        haystacks = [_normalize(item) for item in _as_list(actual) if _exists(item)]
        found = any(needle in haystack for haystack in haystacks for needle in needles)
        return found if operator == "contains_substring" else not found
    if field in MULTI_VALUE_TEXT_FIELDS and operator in {"equals", "not_equals"}:
        list_operator = "contains_any" if operator == "equals" else "not_contains_any"
        return _matches_list_operator(actual, list_operator, expected, field=field)
    if operator in {"before", "on_or_before", "after", "on_or_after"}:
        left_date = _date_value(_first_scalar(actual))
        right_date = _date_value(_first_scalar(expected))
        if left_date is None or right_date is None:
            return False
        if operator == "before":
            return left_date < right_date
        if operator == "on_or_before":
            return left_date <= right_date
        if operator == "after":
            return left_date > right_date
        if operator == "on_or_after":
            return left_date >= right_date
        return False

    left = _first_scalar(actual)
    right = _first_scalar(expected)
    if left is None or right is None:
        return False
    if operator == "equals":
        if field == "media.path":
            return _matches_path_prefix(left, right)
        left_number = _number(left)
        right_number = _number(right)
        if left_number is not None and right_number is not None:
            return left_number == right_number
        return _normalize(left) == _normalize(right)
    if operator == "not_equals":
        if field == "media.path":
            return not _matches_path_prefix(left, right)
        left_number = _number(left)
        right_number = _number(right)
        if left_number is not None and right_number is not None:
            return left_number != right_number
        return _normalize(left) != _normalize(right)

    left_number = _number(left)
    right_number = _number(right)
    if left_number is None or right_number is None:
        return False
    if operator == "greater_than":
        return left_number > right_number
    if operator == "greater_than_or_equal":
        return left_number >= right_number
    if operator == "less_than":
        return left_number < right_number
    if operator == "less_than_or_equal":
        return left_number <= right_number
    return False


def _matches_list_operator(
    actual: Any,
    operator: str,
    expected: Any,
    *,
    field: str | None = None,
) -> bool:
    """Evaluate a list operator against the provided actual and expected values."""
    if field == "media.path":
        actual_paths = [
            normalize_fpath(value, lower=True)
            for value in _as_list(actual)
            if _exists(value)
        ]
        expected_paths = [
            normalize_fpath(value, lower=True, strip_ending_slash=True)
            for value in _as_list(expected)
            if _exists(value)
        ]
        if not expected_paths:
            return False
        has_any = any(
            _matches_path_prefix(actual_path, expected_path)
            for actual_path in actual_paths
            for expected_path in expected_paths
        )
        has_all = all(
            any(
                _matches_path_prefix(actual_path, expected_path)
                for actual_path in actual_paths
            )
            for expected_path in expected_paths
        )
        if operator in {"in", "contains_any"}:
            return has_any
        if operator in {"not_in", "not_contains_any"}:
            return not has_any
        if operator == "contains_all":
            return has_all
        if operator == "not_contains_all":
            return not has_all
        return False

    if field in LANGUAGE_FIELDS:
        actual_values = _normalized_language_values(actual)
        expected_values = _normalized_language_values(expected)
    elif field in COUNTRY_FIELDS:
        actual_values = _normalized_country_values(actual)
        expected_values = _normalized_country_values(expected)
    else:
        actual_values = {
            _normalize(value) for value in _as_list(actual) if _exists(value)
        }
        expected_values = {
            _normalize(value) for value in _as_list(expected) if _exists(value)
        }
    if field in FAIL_CLOSED_LIST_FIELDS and (not actual_values or not expected_values):
        return False
    if not expected_values:
        return False
    has_any = bool(actual_values & expected_values)
    has_all = expected_values.issubset(actual_values)
    if operator in {"in", "contains_any"}:
        return has_any
    if operator in {"not_in", "not_contains_any"}:
        return not has_any
    if operator == "contains_all":
        return has_all
    if operator == "not_contains_all":
        return not has_all
    return False


def _normalized_language_values(value: Any) -> set[str]:
    return {
        normalized
        for item in _as_list(value)
        if (normalized := normalize_language(item)) is not None
    }


def _normalized_country_values(value: Any) -> set[str]:
    return {
        normalized
        for item in _as_list(value)
        if (normalized := str(item or "").strip().upper())
    }


def _normalized_country_list(value: Any) -> list[str]:
    return sorted(_normalized_country_values(value))


def _library_season_count(series: Series) -> int:
    return sum(1 for season in (series.seasons or []) if season.season_number > 0)


def _bitrate_kbps(value: Any, service: Service) -> float | int | None:
    bitrate = _number(value)
    if bitrate is None:
        return None
    if service in {Service.JELLYFIN, Service.EMBY}:
        bitrate /= 1000
    return int(bitrate) if bitrate.is_integer() else bitrate


def _matches_path_prefix(actual: Any, expected: Any) -> bool:
    """Return True when ``actual`` is exactly ``expected`` or a child path of it."""
    actual_path = normalize_fpath(actual, lower=True)
    expected_path = normalize_fpath(expected, lower=True, strip_ending_slash=True)
    if not actual_path or not expected_path:
        return False
    return actual_path == expected_path or actual_path.startswith(f"{expected_path}/")


def _matches_any_regex(
    values: list[Any], patterns: list[Any], *, field: str | None = None
) -> bool | None:
    """Return whether any value matches any regex pattern.

    Returns ``None`` when no supplied pattern compiles, so callers can fail
    closed for both the positive and negated operators. Path fields are
    path-normalized; all other fields (e.g. ``arr.tags``) use plain string
    normalization.
    """
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        if not _exists(pattern):
            continue
        try:
            compiled.append(re.compile(str(pattern), re.IGNORECASE))
        except re.error:
            continue
    if not compiled:
        return None
    if field in PATH_FIELDS:
        normalized_values = [
            normalize_fpath(value, lower=True) for value in values if _exists(value)
        ]
    else:
        normalized_values = [_normalize(value) for value in values if _exists(value)]
    return any(regex.search(value) for regex in compiled for value in normalized_values)


def _path_basename(path: str | None) -> str | None:
    """Return the basename component of a path, normalized for slash style."""
    if not path:
        return None
    normalized = normalize_fpath(path)
    if not normalized:
        return None
    name = normalized.rsplit("/", 1)[-1].strip()
    return name or None


def _effective_last_viewed(
    last_viewed_at: datetime | None,
    added_at: datetime | None,
) -> datetime | None:
    """Return None if the item was re added after its last watch.

    When a file is deleted and re added the media server preserves the old
    watch timestamp, making days_since_last_watched appear artificially low
    for the current copy. Returning None causes the date based watch fields
    to evaluate as if the current copy was never watched, which is correct.
    """
    if last_viewed_at and added_at and added_at > last_viewed_at:
        return None
    return last_viewed_at


def _season_watch_progress(season: Season) -> tuple[bool | None, float | None]:
    """Return season watch completion as (fully_watched, watched_percent).

    Sonarr's canonical episode inventory is the denominator. This prevents a season
    with every downloaded episode watched from appearing complete while Sonarr knows
    about missing or future episodes.
    """
    expected_episode_numbers = set(season.sonarr_episode_numbers or [])
    if not expected_episode_numbers:
        return None, None

    episodes = season.episodes or []
    watched_episode_numbers: set[int] = set()
    for episode in episodes:
        if episode.episode_number not in expected_episode_numbers:
            continue
        effective_last_viewed = _effective_last_viewed(
            episode.last_viewed_at, season.added_at
        )
        if effective_last_viewed is not None:
            watched_episode_numbers.add(episode.episode_number)
            continue
        if episode.last_viewed_at is None and (episode.view_count or 0) > 0:
            watched_episode_numbers.add(episode.episode_number)

    watched_percent = round(
        (len(watched_episode_numbers) / len(expected_episode_numbers)) * 100,
        2,
    )
    return expected_episode_numbers.issubset(watched_episode_numbers), watched_percent


def _days_between(value: datetime | None, now: datetime) -> int | None:
    """Calculate the number of days between the provided datetime value and now,
    returning None if the value is not a valid datetime."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max((now - value).days, 0)


def _exists(value: Any) -> bool:
    """Check if the provided value exists (is not None, empty, or whitespace)."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_exists(item) for item in value)
    return True


def _as_list(value: Any) -> list[Any]:
    """Convert the provided value to a list, ensuring it is returned as a list of non-empty values."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _first_scalar(value: Any) -> Any:
    """Return the first scalar value from a list or the value itself if not a list."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _number(value: Any) -> float | None:
    """Convert the provided value to a float, returning None if the conversion fails."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_value(value: Any) -> date | None:
    """Convert the provided value to a date for temporal rule comparisons."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed_datetime = value
        if value.tzinfo is None:
            parsed_datetime = value.replace(tzinfo=UTC)
        else:
            parsed_datetime = value.astimezone(UTC)
        return parsed_datetime.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return date.fromisoformat(cleaned)
        except ValueError:
            try:
                return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).date()
            except ValueError:
                return None
    return None


def _normalize(value: Any) -> str:
    """Normalize the provided value by converting it to a lowercase string and stripping whitespace."""
    return str(value).strip().lower()


def _format_reason(field: str, operator: str, expected: Any, actual: Any) -> str:
    """Format the reason for a rule evaluation, including the field, operator, expected, and actual values."""
    label = FIELD_LABELS.get(field, field)
    op = OPERATOR_LABELS.get(operator, operator)
    if operator in VALUELESS_OPERATORS:
        return f"{label} {op}"
    value = ", ".join(str(item) for item in _as_list(expected))
    return f"{label} {op} {value} ({_format_actual(actual)})"


def _format_actual(actual: Any) -> str:
    """Format the actual value for a rule evaluation, returning a string representation."""
    values = _as_list(actual)
    if not values:
        return "missing"
    return ", ".join(str(value) for value in values[:4])


def _build_reason_condition(
    field: str, operator: str, expected: Any, actual: Any
) -> dict[str, Any]:
    """Build a reason condition dictionary for a rule evaluation."""
    return {
        "field": field,
        "field_label": FIELD_LABELS.get(field, field),
        "operator": operator,
        "operator_label": OPERATOR_LABELS.get(operator, operator),
        "expected": _json_safe(expected),
        "actual": _json_safe(actual),
        "display": _format_reason(field, operator, expected, actual),
    }


def _json_safe(value: Any) -> Any:
    """Convert the provided value to a JSON-safe format."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)
