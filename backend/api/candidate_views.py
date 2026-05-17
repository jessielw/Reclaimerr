from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    Season,
    Series,
    SeriesServiceRef,
    ServiceMediaLibrary,
)
from backend.enums import MediaType
from backend.models.cleanup import MatchedCandidateRecord
from backend.models.media import (
    CandidateLibraryRef,
    CandidateReasonCondition,
    CandidateReasonPart,
    RulePreviewEntry,
)

_VALUELESS_OPERATORS = {"exists", "not_exists", "is_true", "is_false"}
_NATURAL_SORT_TOKEN_RE = re.compile(r"(\d+)")


def _natural_sort_key(value: str | None) -> tuple[Any, ...]:
    """Build a natural sort key for mixed text/number strings."""
    if not value:
        return ("",)
    parts = _NATURAL_SORT_TOKEN_RE.split(value.lower())
    key: list[Any] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


def _as_list(value: Any) -> list[Any]:
    """Convert a value to a list if it is not already a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalize_library_value(value: Any, library_name_by_id: dict[str, str]) -> Any:
    """Normalize library ID values to library names using the provided mapping."""
    if isinstance(value, list):
        normalized = [
            library_name_by_id.get(str(item), item) if item is not None else item
            for item in value
        ]
        return [item for item in normalized if item is not None]
    if isinstance(value, str):
        return library_name_by_id.get(value, value)
    return value


def _format_reason_actual(actual: Any) -> str:
    """Format the actual value(s) for display in reason conditions."""
    values = _as_list(actual)
    if not values:
        return "missing"
    return ", ".join(str(value) for value in values[:4])


def _format_reason_condition(
    *,
    field_label: str,
    operator: str,
    operator_label: str,
    expected: Any,
    actual: Any,
) -> str:
    """Format a reason condition into a human-readable string."""
    if operator in _VALUELESS_OPERATORS:
        return f"{field_label} {operator_label}"
    expected_values = _as_list(expected)
    expected_text = ", ".join(str(value) for value in expected_values)
    return f"{field_label} {operator_label} {expected_text} ({_format_reason_actual(actual)})"


def normalize_reason_parts(
    reason_data: Any,
    library_name_by_id: dict[str, str],
) -> list[CandidateReasonPart]:
    """Normalize raw reason data into structured CandidateReasonPart objects."""
    parts_raw = reason_data if isinstance(reason_data, list) else []
    normalized_parts: list[CandidateReasonPart] = []
    for part in parts_raw:
        if not isinstance(part, dict):
            continue
        rule_name = str(part.get("rule_name") or "Rule")
        target_scope = str(part.get("target_scope") or "")
        season_label = (
            str(part.get("season_label"))
            if part.get("season_label") is not None
            else None
        )
        conditions_out: list[CandidateReasonCondition] = []
        condition_displays: list[str] = []
        for condition in part.get("conditions", []):
            if not isinstance(condition, dict):
                continue
            field_name = str(condition.get("field") or "")
            field_label = str(condition.get("field_label") or field_name)
            operator = str(condition.get("operator") or "")
            operator_label = str(condition.get("operator_label") or operator)
            expected = condition.get("expected")
            actual = condition.get("actual")
            if field_name == "library.id":
                expected = _normalize_library_value(expected, library_name_by_id)
                actual = _normalize_library_value(actual, library_name_by_id)
            display = _format_reason_condition(
                field_label=field_label,
                operator=operator,
                operator_label=operator_label,
                expected=expected,
                actual=actual,
            )
            conditions_out.append(
                CandidateReasonCondition(
                    field=field_name,
                    field_label=field_label,
                    operator=operator,
                    operator_label=operator_label,
                    expected=expected,
                    actual=actual,
                    display=display,
                )
            )
            condition_displays.append(display)
        label = rule_name if not season_label else f"{rule_name} ({season_label})"
        summary = ", ".join(condition_displays)
        text = f"{label}: {summary}" if summary else label
        normalized_parts.append(
            CandidateReasonPart(
                rule_id=part.get("rule_id"),
                rule_name=rule_name,
                target_scope=target_scope,
                season_label=season_label,
                conditions=conditions_out,
                text=text,
            )
        )
    return normalized_parts


def reason_tokens(reason_parts: list[CandidateReasonPart]) -> list[str]:
    """Extract display tokens from reason parts for search indexing."""
    return [part.text for part in reason_parts if part.text]


async def build_rule_preview_items(
    db: AsyncSession,
    records: list[MatchedCandidateRecord],
) -> list[RulePreviewEntry]:
    """Build a list of RulePreviewEntry items from raw MatchedCandidateRecord data,
    including related media information."""
    if not records:
        return []

    movie_ids = {record.movie_id for record in records if record.movie_id is not None}
    movie_version_ids = {
        record.movie_version_id
        for record in records
        if record.movie_version_id is not None
    }
    series_ids = {
        record.series_id for record in records if record.series_id is not None
    }
    season_ids = {
        record.season_id for record in records if record.season_id is not None
    }
    episode_ids = {
        record.episode_id for record in records if record.episode_id is not None
    }

    movies_by_id: dict[int, Movie] = {}
    if movie_ids:
        movies_result = await db.execute(select(Movie).where(Movie.id.in_(movie_ids)))
        movies_by_id = {movie.id: movie for movie in movies_result.scalars().all()}

    versions_by_id: dict[int, MovieVersion] = {}
    if movie_version_ids:
        versions_result = await db.execute(
            select(MovieVersion).where(MovieVersion.id.in_(movie_version_ids))
        )
        versions_by_id = {
            version.id: version for version in versions_result.scalars().all()
        }

    series_by_id: dict[int, Series] = {}
    if series_ids:
        series_result = await db.execute(
            select(Series).where(Series.id.in_(series_ids))
        )
        series_by_id = {series.id: series for series in series_result.scalars().all()}

    seasons_by_id: dict[int, Season] = {}
    if season_ids:
        seasons_result = await db.execute(
            select(Season).where(Season.id.in_(season_ids))
        )
        seasons_by_id = {season.id: season for season in seasons_result.scalars().all()}

    episodes_by_id: dict[int, Episode] = {}
    if episode_ids:
        episodes_result = await db.execute(
            select(Episode).where(Episode.id.in_(episode_ids))
        )
        episodes_by_id = {ep.id: ep for ep in episodes_result.scalars().all()}

    global_library_name_by_id: dict[str, str] = {}
    libraries_result = await db.execute(
        select(ServiceMediaLibrary.library_id, ServiceMediaLibrary.library_name)
    )
    for library_id, library_name in libraries_result.all():
        if library_id and library_name and library_id not in global_library_name_by_id:
            global_library_name_by_id[library_id] = library_name

    series_library_refs_by_id: dict[int, list[CandidateLibraryRef]] = {}
    if series_ids:
        refs_result = await db.execute(
            select(
                SeriesServiceRef.series_id,
                SeriesServiceRef.service,
                SeriesServiceRef.library_id,
                SeriesServiceRef.library_name,
            ).where(SeriesServiceRef.series_id.in_(series_ids))
        )
        for series_id, service, library_id, library_name in refs_result.all():
            if series_id is None or not library_id or not library_name:
                continue
            refs = series_library_refs_by_id.setdefault(series_id, [])
            if any(ref.library_id == library_id for ref in refs):
                continue
            refs.append(
                CandidateLibraryRef(
                    library_id=library_id,
                    library_name=library_name,
                    service=service.value if service is not None else None,
                )
            )

    items: list[RulePreviewEntry] = []
    for record in records:
        is_movie = record.media_type is MediaType.MOVIE
        movie = movies_by_id.get(record.movie_id or -1)
        version = versions_by_id.get(record.movie_version_id or -1)
        series = series_by_id.get(record.series_id or -1)
        season = seasons_by_id.get(record.season_id or -1)
        episode = episodes_by_id.get(record.episode_id or -1)

        media_id = record.movie_id if is_movie else record.series_id
        media_title = (
            movie.title if is_movie and movie else series.title if series else None
        )
        media_year = (
            movie.year if is_movie and movie else series.year if series else None
        )
        poster_url = (
            movie.poster_url
            if is_movie and movie
            else series.poster_url
            if series
            else None
        )
        if media_id is None or media_title is None:
            continue

        library_name_by_id = dict(global_library_name_by_id)
        if version and version.library_id and version.library_name:
            library_name_by_id[version.library_id] = version.library_name
        for ref in series_library_refs_by_id.get(record.series_id or -1, []):
            library_name_by_id[ref.library_id] = ref.library_name

        normalized_reasons = normalize_reason_parts(
            record.reason_data,
            library_name_by_id,
        )
        items.append(
            RulePreviewEntry(
                media_type=record.media_type.value,
                media_id=media_id,
                media_title=media_title,
                media_year=media_year,
                poster_url=poster_url,
                movie_version_id=record.movie_version_id,
                version_service=version.service.value if version else None,
                version_library_id=version.library_id if version else None,
                version_library_name=version.library_name if version else None,
                version_video_codec_family=version.video_codec_family
                if version
                else None,
                version_audio_codec_family=version.audio_codec_family
                if version
                else None,
                version_video_width=version.video_width if version else None,
                version_video_height=version.video_height if version else None,
                version_video_resolution=version.video_resolution if version else None,
                version_video_hdr=version.video_hdr if version else None,
                version_video_dolby_vision=version.video_dolby_vision
                if version
                else None,
                version_audio_channels=version.audio_channels if version else None,
                version_audio_languages=version.audio_languages if version else None,
                version_size=version.size if version else None,
                version_path=version.path if version else None,
                version_file_name=version.file_name if version else None,
                version_subtitle_languages=version.subtitle_languages
                if version
                else None,
                reason_parts=normalized_reasons,
                reason_tokens=reason_tokens(normalized_reasons),
                estimated_space_bytes=(
                    record.estimated_space_bytes
                    if record.estimated_space_bytes is not None
                    else version.size
                    if version
                    else episode.size
                    if episode
                    else season.size
                    if season
                    else None
                ),
                season_id=record.season_id,
                season_number=season.season_number if season else None,
                series_title=series.title if (season or episode) and series else None,
                season_has_hdr=season.has_hdr if season else None,
                season_has_dolby_vision=season.has_dolby_vision if season else None,
                season_max_video_width=season.max_video_width if season else None,
                season_max_video_height=season.max_video_height if season else None,
                season_video_codec_families=season.video_codec_families
                if season
                else None,
                season_audio_codec_families=season.audio_codec_families
                if season
                else None,
                season_audio_languages=season.audio_languages if season else None,
                season_subtitle_languages=season.subtitle_languages if season else None,
                series_library_refs=series_library_refs_by_id.get(
                    record.series_id or -1
                )
                if season or episode
                else None,
                episode_id=record.episode_id,
                episode_number=episode.episode_number if episode else None,
                episode_name=episode.name if episode else None,
            )
        )

    items.sort(
        key=lambda item: (
            _natural_sort_key(item.media_title),
            item.media_year is None,
            item.media_year or 0,
            item.season_number is None,
            item.season_number or 0,
            item.episode_number is None,
            item.episode_number or 0,
            item.movie_version_id is None,
            item.movie_version_id or 0,
            item.media_id,
        )
    )
    return items
