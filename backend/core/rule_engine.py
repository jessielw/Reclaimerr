from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any

from backend.database.models import Movie, MovieVersion, ReclaimRule, Season, Series
from backend.enums import MediaType

TARGET_MOVIE_VERSION = "movie_version"
TARGET_SERIES = "series"
TARGET_SEASON = "season"
VALID_TARGET_SCOPES = {TARGET_MOVIE_VERSION, TARGET_SERIES, TARGET_SEASON}

RuleDefinition = dict[str, Any]

FIELD_LABELS: dict[str, str] = {
    "library.id": "Library",
    "media.path": "Path",
    "media.size": "Size",
    "media.days_since_added": "Days since added",
    "watch.view_count": "Views",
    "watch.days_since_last_watched": "Days since watched",
    "watch.last_viewed_at": "Last watched",
    "tmdb.popularity": "Popularity",
    "tmdb.vote_average": "Rating",
    "tmdb.vote_count": "Vote count",
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
}

OPERATOR_LABELS: dict[str, str] = {
    "equals": "is",
    "not_equals": "is not",
    "greater_than": ">",
    "greater_than_or_equal": ">=",
    "less_than": "<",
    "less_than_or_equal": "<=",
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
    "tmdb.popularity",
    "tmdb.vote_average",
    "tmdb.vote_count",
    "video.width",
    "video.height",
    "audio.channels",
    "audio.track_count",
    "media.duration",
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
}
LIBRARY_FIELDS = {"library.id"}
BOOLEAN_FIELDS = {"video.hdr", "video.dolby_vision"}
TEMPORAL_FIELDS = {"watch.last_viewed_at"}
PATH_FIELDS = {"media.path"}
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
TEMPORAL_OPERATORS = {"exists", "not_exists"}
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
) -> tuple[bool, dict[str, Any], list[dict[str, Any]]]:
    """Evaluate an advanced rule against the provided media context, returning whether it
    matches, the matched field values, and the reasons for the match or failure."""
    definition = normalize_rule_definition(rule)
    if not definition:
        return False, {}, []
    root = definition.get("root")
    if not isinstance(root, dict):
        return False, {}, []

    context = _build_context(target_scope, movie, version, series, season)
    matched: dict[str, Any] = {}
    reasons: list[dict[str, Any]] = []
    if not _evaluate_node(root, context, matched, reasons):
        return False, {}, []
    return True, matched, reasons


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
) -> dict[str, Any]:
    """Build the context dictionary for evaluating a rule against a specific target scope."""
    now = datetime.now(UTC)
    if target_scope == TARGET_MOVIE_VERSION and movie and version:
        size = version.size if version.size and version.size > 0 else movie.size
        return {
            "library.id": [version.library_id],
            "media.path": [version.path] if version.path else [],
            "media.size": size,
            "media.days_since_added": _days_between(
                version.added_at or movie.added_at, now
            ),
            "watch.view_count": movie.view_count,
            "watch.last_viewed_at": movie.last_viewed_at,
            "watch.days_since_last_watched": _days_between(movie.last_viewed_at, now),
            "tmdb.popularity": movie.popularity,
            "tmdb.vote_average": movie.vote_average,
            "tmdb.vote_count": movie.vote_count,
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
        }

    if target_scope == TARGET_SERIES and series:
        refs = series.service_refs or []
        return {
            "library.id": [ref.library_id for ref in refs if ref.library_id],
            "media.path": [ref.path for ref in refs if ref.path],
            "media.size": series.size,
            "media.days_since_added": _days_between(series.added_at, now),
            "watch.view_count": series.view_count,
            "watch.last_viewed_at": series.last_viewed_at,
            "watch.days_since_last_watched": _days_between(series.last_viewed_at, now),
            "tmdb.popularity": series.popularity,
            "tmdb.vote_average": series.vote_average,
            "tmdb.vote_count": series.vote_count,
            "series.status": series.status,
            "video.codec_family": series.video_codec_families,
            "audio.codec_family": series.audio_codec_families,
            "video.hdr": series.has_hdr,
            "video.dolby_vision": series.has_dolby_vision,
            "video.width": series.max_video_width,
            "video.height": series.max_video_height,
            "audio.channels": series.max_audio_channels,
            "subtitle.languages": series.subtitle_languages,
        }

    if target_scope == TARGET_SEASON and series and season:
        refs = series.service_refs or []
        return {
            "library.id": [ref.library_id for ref in refs if ref.library_id],
            "media.path": [ref.path for ref in refs if ref.path],
            "media.size": season.size,
            "media.days_since_added": _days_between(season.added_at, now),
            "watch.view_count": season.view_count,
            "watch.last_viewed_at": season.last_viewed_at,
            "watch.days_since_last_watched": _days_between(season.last_viewed_at, now),
            "tmdb.popularity": series.popularity,
            "tmdb.vote_average": series.vote_average,
            "tmdb.vote_count": series.vote_count,
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
    matched[field] = actual
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
        PurePath(str(value)).as_posix().lower() for value in values if _exists(value)
    ]
    for pattern in patterns:
        try:
            regex = re.compile(str(pattern), re.IGNORECASE)
        except re.error:
            continue
        if any(regex.search(value) for value in normalized_values):
            return True
    return False


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
