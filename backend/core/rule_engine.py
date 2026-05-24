from __future__ import annotations

import re
import shutil
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from datetime import UTC, date, datetime
from typing import Any

from backend.core.utils.filesystem import normalize_fpath
from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    ReclaimRule,
    Season,
    Series,
)
from backend.enums import MediaType

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

RuleDefinition = dict[str, Any]

FIELD_LABELS: dict[str, str] = {
    "library.id": "Library",
    "media.path": "Path",
    "media.file_name": "Filename",
    "media.size": "Size",
    "media.days_since_added": "Days since added",
    "watch.view_count": "Views",
    "watch.days_since_last_watched": "Days since watched",
    "watch.last_viewed_at": "Last watched",
    "tmdb.release_date": "TMDB release date",
    "tmdb.first_air_date": "TMDB first air date",
    "tmdb.last_air_date": "TMDB last air date",
    "season.air_date": "Season air date",
    "tmdb.days_since_release": "Days since released",
    "tmdb.days_since_first_air_date": "Days since first aired",
    "tmdb.days_since_last_air_date": "Days since last aired",
    "season.days_since_air_date": "Days since season aired",
    "season.season_number": "Season number",
    "season.episode_count": "Episode count",
    "season.is_latest_season": "Is latest season",
    "season.seasons_from_latest": "Seasons from latest",
    "episode.number": "Episode number",
    "episode.season_number": "Episode season number",
    "episode.air_date": "Episode air date",
    "episode.days_since_air_date": "Days since episode aired",
    "watch.never_watched": "Never watched",
    "tmdb.popularity": "Popularity",
    "tmdb.vote_average": "Rating",
    "tmdb.vote_count": "Vote count",
    "imdb.rating": "IMDb rating",
    "imdb.vote_count": "IMDb vote count",
    "anilist.score": "AniList score",
    "anilist.popularity": "AniList popularity",
    "anilist.favourites": "AniList favourites",
    "series.status": "Series status",
    "video.codec_family": "Video codec",
    "audio.codec_family": "Audio codec",
    "video.hdr": "HDR",
    "video.dolby_vision": "Dolby Vision",
    "video.width": "Video width",
    "video.height": "Video height",
    "video.resolution": "Resolution",
    "audio.channels": "Audio channels",
    "audio.track_count": "Audio tracks",
    "audio.languages": "Audio languages",
    "subtitle.languages": "Subtitle languages",
    "video.color_space": "Color space",
    "video.color_transfer": "Color transfer",
    "video.color_primaries": "Color primaries",
    "media.duration": "Duration",
    "arr.tags": "Arr tags",
    "arr.monitored": "Arr monitored",
    "seerr.requested": "Seerr requested",
    "seerr.requested_by_user_ids": "Seerr requested by user IDs",
    "seerr.requester_has_watched": "Seerr requester has watched",
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
    "contains_any": "contains",
    "not_contains_any": "does not contain",
    "exists": "exists",
    "not_exists": "missing",
    "is_true": "is true",
    "is_false": "is false",
    "matches_any_regex": "matches path",
}

LIST_OPERATORS = {
    "in",
    "not_in",
    "contains_any",
    "not_contains_any",
    "matches_any_regex",
}
VALUELESS_OPERATORS = {"exists", "not_exists", "is_true", "is_false"}
NUMERIC_FIELDS = {
    "media.size",
    "media.days_since_added",
    "watch.view_count",
    "watch.days_since_last_watched",
    "tmdb.days_since_release",
    "tmdb.days_since_first_air_date",
    "tmdb.days_since_last_air_date",
    "season.days_since_air_date",
    "season.season_number",
    "season.episode_count",
    "season.seasons_from_latest",
    "episode.number",
    "episode.season_number",
    "episode.days_since_air_date",
    "tmdb.popularity",
    "tmdb.vote_average",
    "tmdb.vote_count",
    "imdb.rating",
    "imdb.vote_count",
    "anilist.score",
    "anilist.popularity",
    "anilist.favourites",
    "video.width",
    "video.height",
    "audio.channels",
    "audio.track_count",
    "media.duration",
    "disk.free_bytes",
    "disk.free_percent",
}
TEXT_FIELDS = {
    "series.status",
    "video.codec_family",
    "audio.codec_family",
    "video.resolution",
    "audio.languages",
    "subtitle.languages",
    "video.color_space",
    "video.color_transfer",
    "video.color_primaries",
    "arr.tags",
    "seerr.requested_by_user_ids",
}
LIBRARY_FIELDS = {"library.id"}
BOOLEAN_FIELDS = {
    "video.hdr",
    "video.dolby_vision",
    "season.is_latest_season",
    "watch.never_watched",
    "arr.monitored",
    "seerr.requested",
    "seerr.requester_has_watched",
}
TEMPORAL_FIELDS = {
    "watch.last_viewed_at",
    "tmdb.release_date",
    "tmdb.first_air_date",
    "tmdb.last_air_date",
    "season.air_date",
    "episode.air_date",
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
    "exists",
    "not_exists",
}
LIBRARY_OPERATORS = {
    "in",
    "not_in",
    "contains_any",
    "not_contains_any",
    "exists",
    "not_exists",
}
BOOLEAN_OPERATORS = {"is_true", "is_false", "exists", "not_exists"}
SEERR_REQUESTER_ID_OPERATORS = {
    "in",
    "not_in",
    "contains_any",
    "not_contains_any",
    "exists",
    "not_exists",
}
TEMPORAL_OPERATORS = {
    "exists",
    "not_exists",
    "before",
    "on_or_before",
    "after",
    "on_or_after",
}
PATH_OPERATORS = TEXT_OPERATORS | {"matches_any_regex"}
PATH_LIBRARY_INCLUSION_OPERATORS = {"contains_any", "in", "equals"}
PATH_LIBRARY_UNSUPPORTED_OPERATORS = {
    "not_in",
    "not_contains_any",
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
    "seerr.requested_by_user_ids": set(SEERR_REQUESTER_ID_OPERATORS),
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
        arr_entries: list[dict] | None = None,
        path_mappings: list[dict] | None = None,
    ) -> None:
        self._arr_entries: list[dict] = sorted(
            arr_entries or [], key=lambda e: -len(str(e.get("path") or ""))
        )
        self._path_mappings: list[dict] = sorted(
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


class SeerrRequestResolver:
    """Holds pre fetched Seerr request state for one scan run.

    State is keyed by ``(media_type, tmdb_id)`` and values are requester id sets.
    """

    _ctx: ContextVar[SeerrRequestResolver | None] = ContextVar(
        "seerr_request_resolver", default=None
    )

    __slots__ = ("_requester_ids_by_key", "_requester_has_watched_by_key")

    def __init__(
        self,
        requester_ids_by_key: Mapping[tuple[MediaType, int], Iterable[int]]
        | None = None,
        requester_has_watched_by_key: Mapping[tuple[MediaType, int], bool]
        | None = None,
    ):
        self._requester_ids_by_key: dict[tuple[MediaType, int], set[int]] = {}
        for key, user_ids in (requester_ids_by_key or {}).items():
            self._requester_ids_by_key[key] = {int(v) for v in user_ids}
        self._requester_has_watched_by_key: dict[tuple[MediaType, int], bool] = {
            key: bool(value)
            for key, value in (requester_has_watched_by_key or {}).items()
        }

    def activate(self) -> None:
        """Install this resolver for the current async context."""
        SeerrRequestResolver._ctx.set(self)

    @classmethod
    def current(cls) -> SeerrRequestResolver | None:
        """Return the resolver active in the current async context, or None."""
        return cls._ctx.get()

    def resolve_requester_ids(
        self, media_type: MediaType, tmdb_id: int | None
    ) -> list[int] | None:
        """Return Seerr requester IDs for the given media key if known."""
        if tmdb_id is None:
            return None
        ids = self._requester_ids_by_key.get((media_type, tmdb_id))
        if ids is None:
            return None
        return sorted(ids)

    def resolve(self, media_type: MediaType, tmdb_id: int | None) -> bool | None:
        """Return Seerr requested state for the given media key if known."""
        requester_ids = self.resolve_requester_ids(media_type, tmdb_id)
        if requester_ids is None:
            return None
        return bool(requester_ids)

    def resolve_requester_has_watched(
        self, media_type: MediaType, tmdb_id: int | None
    ) -> bool | None:
        """Return requester watched state for the given media key if known."""
        if tmdb_id is None:
            return None
        value = self._requester_has_watched_by_key.get((media_type, tmdb_id))
        if value is None:
            return None
        return bool(value)


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


def validate_rule_definition(definition: RuleDefinition | None) -> None:
    """Validate the structure of a rule definition, ensuring it includes a root group and valid nodes."""
    if not definition:
        raise ValueError("Rule definition is required")
    if not _has_valid_definition(definition):
        raise ValueError("Rule definition must include a root group")
    _validate_node(definition["root"])


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


def collect_rule_path_patterns(definition: RuleDefinition | None) -> list[str]:
    """Collect all unique path patterns from media.path conditions in the rule definition."""
    seen: set[str] = set()
    patterns: list[str] = []
    for condition in collect_rule_conditions(definition, field="media.path"):
        for value in _normalize_condition_values(condition.get("value")):
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


def _rule_uses_disk_fields(definition: RuleDefinition | None) -> bool:
    """Return True if the rule definition references any disk.* fields."""
    return bool(
        collect_rule_conditions(definition, field="disk.free_bytes")
        or collect_rule_conditions(definition, field="disk.free_percent")
    )


def _has_valid_definition(definition: RuleDefinition | None) -> bool:
    """Check if the rule definition has a valid structure with a root group."""
    return isinstance(definition, dict) and isinstance(definition.get("root"), dict)


def _iter_condition_nodes(node: dict[str, Any], *, field: str | None = None):
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
    if target_scope == TARGET_MOVIE_VERSION and movie and version:
        size = version.size if version.size and version.size > 0 else movie.size
        _disk = (
            _resolver.resolve(version.path) if (_resolver and version.path) else None
        )
        _added = version.added_at or movie.added_at
        _last_viewed = _effective_last_viewed(movie.last_viewed_at, _added)
        return {
            "library.id": [version.library_id],
            "media.path": [version.path] if version.path else [],
            "media.file_name": [version.file_name] if version.file_name else [],
            "media.size": size,
            "media.days_since_added": _days_between(
                version.added_at or movie.added_at, now
            ),
            "watch.view_count": movie.view_count,
            "watch.last_viewed_at": _last_viewed,
            "watch.days_since_last_watched": _days_between(_last_viewed, now),
            "tmdb.release_date": movie.tmdb_release_date,
            "tmdb.days_since_release": _days_between(movie.tmdb_release_date, now),
            "tmdb.popularity": movie.popularity,
            "tmdb.vote_average": movie.vote_average,
            "tmdb.vote_count": movie.vote_count,
            "imdb.rating": movie.imdb_rating,
            "imdb.vote_count": movie.imdb_vote_count,
            "anilist.score": movie.anilist_score,
            "anilist.popularity": movie.anilist_popularity,
            "anilist.favourites": movie.anilist_favourites,
            "video.codec_family": version.video_codec_family,
            "audio.codec_family": version.audio_codec_family,
            "video.hdr": version.video_hdr,
            "video.dolby_vision": version.video_dolby_vision,
            "video.width": version.video_width,
            "video.height": version.video_height,
            "video.resolution": version.video_resolution,
            "audio.channels": version.audio_channels,
            "audio.track_count": version.audio_count,
            "audio.languages": version.audio_languages,
            "subtitle.languages": version.subtitle_languages,
            "video.color_space": version.video_color_space,
            "video.color_transfer": version.video_color_transfer,
            "video.color_primaries": version.video_color_primaries,
            "media.duration": version.duration,
            "arr.tags": movie.arr_tags or [],
            "arr.monitored": movie.is_monitored,
            "seerr.requested": (
                _seerr_resolver.resolve(MediaType.MOVIE, movie.tmdb_id)
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
        _series_path = next((ref.path for ref in refs if ref.path), None)
        _disk = (
            _resolver.resolve(_series_path) if (_resolver and _series_path) else None
        )
        _last_viewed = _effective_last_viewed(series.last_viewed_at, series.added_at)
        return {
            "library.id": [ref.library_id for ref in refs if ref.library_id],
            "media.path": [ref.path for ref in refs if ref.path],
            "media.file_name": [],
            "media.size": series.size,
            "media.days_since_added": _days_between(series.added_at, now),
            "watch.view_count": series.view_count,
            "watch.last_viewed_at": _last_viewed,
            "watch.days_since_last_watched": _days_between(_last_viewed, now),
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
            "imdb.rating": series.imdb_rating,
            "imdb.vote_count": series.imdb_vote_count,
            "anilist.score": series.anilist_score,
            "anilist.popularity": series.anilist_popularity,
            "anilist.favourites": series.anilist_favourites,
            "series.status": series.status,
            "video.codec_family": series.video_codec_families,
            "audio.codec_family": series.audio_codec_families,
            "video.hdr": series.has_hdr,
            "video.dolby_vision": series.has_dolby_vision,
            "video.width": series.max_video_width,
            "video.height": series.max_video_height,
            "audio.channels": series.max_audio_channels,
            "subtitle.languages": series.subtitle_languages,
            "arr.tags": series.arr_tags or [],
            "arr.monitored": series.is_monitored,
            "seerr.requested": (
                _seerr_resolver.resolve(MediaType.SERIES, series.tmdb_id)
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
                    MediaType.SERIES, series.tmdb_id
                )
                if _seerr_resolver
                else None
            ),
            "disk.free_bytes": _disk[0] if _disk else None,
            "disk.free_percent": _disk[1] if _disk else None,
        }

    if target_scope == TARGET_SEASON and series and season:
        refs = series.service_refs or []
        non_special_nums = sorted(
            s.season_number for s in (series.seasons or []) if s.season_number > 0
        )
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
        return {
            "library.id": [ref.library_id for ref in refs if ref.library_id],
            "media.path": [ref.path for ref in refs if ref.path],
            "media.file_name": [season.path.rsplit("/", 1)[-1]] if season.path else [],
            "media.size": season.size,
            "media.days_since_added": _days_between(season.added_at, now),
            "watch.view_count": season.view_count,
            "watch.last_viewed_at": _last_viewed,
            "watch.days_since_last_watched": _days_between(_last_viewed, now),
            "season.air_date": season.air_date,
            "season.days_since_air_date": _days_between(season.air_date, now),
            "season.season_number": season.season_number,
            "season.episode_count": season.episode_count,
            "season.is_latest_season": is_latest_season,
            "season.seasons_from_latest": seasons_from_latest,
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
            "imdb.rating": series.imdb_rating,
            "imdb.vote_count": series.imdb_vote_count,
            "anilist.score": series.anilist_score,
            "anilist.popularity": series.anilist_popularity,
            "anilist.favourites": series.anilist_favourites,
            "series.status": series.status,
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
            "arr.monitored": season.is_monitored,
            "seerr.requested": (
                _seerr_resolver.resolve(MediaType.SERIES, series.tmdb_id)
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
                    MediaType.SERIES, series.tmdb_id
                )
                if _seerr_resolver
                else None
            ),
            "disk.free_bytes": _disk[0] if _disk else None,
            "disk.free_percent": _disk[1] if _disk else None,
        }

    if target_scope == TARGET_EPISODE and series and season and episode:
        refs = series.service_refs or []
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
        return {
            "library.id": [ref.library_id for ref in refs if ref.library_id],
            "media.path": [episode.path] if episode.path else [],
            "media.file_name": [episode.path.rsplit("/", 1)[-1]]
            if episode.path
            else [],
            "media.size": episode.size,
            "media.days_since_added": _days_between(season.added_at, now),
            "watch.view_count": episode.view_count,
            "watch.last_viewed_at": _last_viewed_ep,
            "watch.days_since_last_watched": _days_between(_last_viewed_ep, now),
            "watch.never_watched": episode.view_count == 0 or _last_viewed_ep is None,
            "episode.number": episode.episode_number,
            "episode.season_number": season.season_number,
            "episode.air_date": episode.air_date,
            "episode.days_since_air_date": _days_between(episode.air_date, now),
            "season.season_number": season.season_number,
            "season.episode_count": season.episode_count,
            "season.is_latest_season": is_latest_season_ep,
            "season.seasons_from_latest": seasons_from_latest_ep,
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
            "imdb.rating": series.imdb_rating,
            "imdb.vote_count": series.imdb_vote_count,
            "anilist.score": series.anilist_score,
            "anilist.popularity": series.anilist_popularity,
            "anilist.favourites": series.anilist_favourites,
            "series.status": series.status,
            "arr.tags": series.arr_tags or [],
            "arr.monitored": season.is_monitored,
            "seerr.requested": (
                _seerr_resolver.resolve(MediaType.SERIES, series.tmdb_id)
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
                    MediaType.SERIES, series.tmdb_id
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
        children = [
            child for child in node.get("children", []) if isinstance(child, dict)
        ]
        if not children:
            return False
        op = str(node.get("op", "and")).lower()
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
    if not _matches_operator(actual, operator, expected):
        return False
    matched[field] = actual.isoformat() if isinstance(actual, datetime) else actual
    reasons.append(_build_reason_condition(field, operator, expected, actual))
    return True


def _matches_operator(actual: Any, operator: str, expected: Any) -> bool:
    """Evaluate a single condition operator against the provided actual and expected values."""
    if operator == "exists":
        return _exists(actual)
    if operator == "not_exists":
        return not _exists(actual)
    if operator == "is_true":
        return actual is True
    if operator == "is_false":
        return actual is False
    if operator == "matches_any_regex":
        return _matches_any_regex(_as_list(actual), _as_list(expected))
    if operator in LIST_OPERATORS:
        return _matches_list_operator(actual, operator, expected)
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
        return _normalize(left) == _normalize(right)
    if operator == "not_equals":
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


def _matches_list_operator(actual: Any, operator: str, expected: Any) -> bool:
    """Evaluate a list operator against the provided actual and expected values."""
    actual_values = {_normalize(value) for value in _as_list(actual) if _exists(value)}
    expected_values = {
        _normalize(value) for value in _as_list(expected) if _exists(value)
    }
    if not expected_values:
        return False
    has_any = bool(actual_values & expected_values)
    if operator in {"in", "contains_any"}:
        return has_any
    if operator in {"not_in", "not_contains_any"}:
        return not has_any
    return False


def _matches_any_regex(values: list[Any], patterns: list[Any]) -> bool:
    """Evaluate whether any of the provided values match any of the provided regex patterns."""
    normalized_values = [
        normalize_fpath(value, lower=True) for value in values if _exists(value)
    ]
    for pattern in patterns:
        try:
            regex = re.compile(str(pattern), re.IGNORECASE)
        except re.error:
            continue
        if any(regex.search(value) for value in normalized_values):
            return True
    return False


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
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        else:
            value = value.astimezone(UTC)
        return value.date()
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
