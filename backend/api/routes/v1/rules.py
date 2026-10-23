from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.api_tokens import (
    API_TOKEN_RULES_READ_SCOPE,
    ApiPrincipal,
    require_api_scope,
)
from backend.core.rule_engine import (
    RULE_OUTCOME_MATCH,
    normalize_rule_outcome,
    normalize_rule_target,
)
from backend.database import get_db
from backend.database.models import (
    Episode,
    Movie,
    MovieArrRef,
    ReclaimRule,
    Season,
    Series,
    SeriesArrRef,
    ServiceConfig,
)
from backend.enums import MediaType
from backend.models.api_v1 import (
    RuleListResponse,
    RuleMatchesResponse,
    RuleResponse,
)
from backend.models.api_v1.rules import (
    RuleMatchArrRef,
    RuleMatchItem,
    RuleMatchRuleRef,
)
from backend.models.cleanup import MatchedCandidateRecord, RulePreviewMatchMetadata
from backend.tasks.cleanup import collect_rule_preview_matches_with_metadata

router = APIRouter(tags=["v1:rules"])

# every call is a full live evaluation, so a tight polling loop must not be
# able to stack them up
_evaluation_lock = asyncio.Lock()


def _rule_response(rule: ReclaimRule) -> RuleResponse:
    return RuleResponse(
        id=rule.id,
        name=rule.name,
        media_type=rule.media_type,
        target_scope=normalize_rule_target(rule),
        outcome=normalize_rule_outcome(rule),
        enabled=rule.enabled,
    )


def _match_warnings(metadata: RulePreviewMatchMetadata) -> list[str]:
    warnings: list[str] = []
    if metadata.seerr_unavailable:
        warnings.append(
            f"Seerr request data was unavailable: {metadata.seerr_error or 'unknown error'}"
        )
    if metadata.playback_unavailable_count:
        warnings.append(
            f"Playback history was unavailable for "
            f"{metadata.playback_unavailable_count} item(s)"
            + (f": {metadata.playback_error}" if metadata.playback_error else "")
        )
    if metadata.sonarr_unavailable_count:
        warnings.append(
            f"Sonarr data was unavailable for {metadata.sonarr_unavailable_count} "
            "series" + (f": {metadata.sonarr_error}" if metadata.sonarr_error else "")
        )
    if metadata.season_inventory_unavailable_count:
        warnings.append(
            "Season episode inventory was unavailable for "
            f"{metadata.season_inventory_unavailable_count} season(s)"
        )
    if metadata.requester_watch_unavailable_count:
        warnings.append(
            "Requester watch data was unavailable for "
            f"{metadata.requester_watch_unavailable_count} item(s)"
        )
    if metadata.watch_completion_unavailable_count:
        warnings.append(
            "Watch completion data was unavailable for "
            f"{metadata.watch_completion_unavailable_count} item(s)"
        )
    return warnings


async def _match_items(
    db: AsyncSession, target_scope: str, records: list[MatchedCandidateRecord]
) -> list[RuleMatchItem]:
    movie_ids = {r.movie_id for r in records if r.movie_id is not None}
    series_ids = {r.series_id for r in records if r.series_id is not None}
    episode_ids = {r.episode_id for r in records if r.episode_id is not None}

    movies_by_id: dict[int, Movie] = {}
    if movie_ids:
        movies_by_id = {
            movie.id: movie
            for movie in (
                await db.execute(select(Movie).where(Movie.id.in_(movie_ids)))
            ).scalars()
        }
    series_by_id: dict[int, Series] = {}
    if series_ids:
        series_by_id = {
            series.id: series
            for series in (
                await db.execute(select(Series).where(Series.id.in_(series_ids)))
            ).scalars()
        }
    episodes_by_id: dict[int, Episode] = {}
    if episode_ids:
        episodes_by_id = {
            episode.id: episode
            for episode in (
                await db.execute(select(Episode).where(Episode.id.in_(episode_ids)))
            ).scalars()
        }
    season_ids = {r.season_id for r in records if r.season_id is not None} | {
        episode.season_id for episode in episodes_by_id.values()
    }
    season_numbers: dict[int, int] = {}
    if season_ids:
        season_numbers = {
            season_id: season_number
            for season_id, season_number in (
                await db.execute(
                    select(Season.id, Season.season_number).where(
                        Season.id.in_(season_ids)
                    )
                )
            ).all()
        }

    service_names = {
        config_id: name or None
        for config_id, name in (
            await db.execute(select(ServiceConfig.id, ServiceConfig.name))
        ).all()
    }
    arr_refs: dict[int, list[RuleMatchArrRef]] = {}
    if movie_ids:
        for media_id, config_id, arr_id in (
            await db.execute(
                select(
                    MovieArrRef.movie_id,
                    MovieArrRef.service_config_id,
                    MovieArrRef.arr_movie_id,
                ).where(MovieArrRef.movie_id.in_(movie_ids))
            )
        ).all():
            arr_refs.setdefault(media_id, []).append(
                RuleMatchArrRef(
                    service="radarr",
                    service_config_id=config_id,
                    service_name=service_names.get(config_id),
                    arr_id=arr_id,
                )
            )
    if series_ids:
        for media_id, config_id, arr_id in (
            await db.execute(
                select(
                    SeriesArrRef.series_id,
                    SeriesArrRef.service_config_id,
                    SeriesArrRef.arr_series_id,
                ).where(SeriesArrRef.series_id.in_(series_ids))
            )
        ).all():
            arr_refs.setdefault(media_id, []).append(
                RuleMatchArrRef(
                    service="sonarr",
                    service_config_id=config_id,
                    service_name=service_names.get(config_id),
                    arr_id=arr_id,
                )
            )

    items: list[RuleMatchItem] = []
    for record in records:
        episode = episodes_by_id.get(record.episode_id or -1)
        season_id = record.season_id or (episode.season_id if episode else None)
        if record.media_type is MediaType.MOVIE:
            movie = movies_by_id.get(record.movie_id or -1)
            if movie is None:
                continue
            items.append(
                RuleMatchItem(
                    target_scope=target_scope,
                    media_type=record.media_type,
                    movie_id=movie.id,
                    movie_version_id=record.movie_version_id,
                    title=movie.title,
                    year=movie.year,
                    tmdb_id=movie.tmdb_id,
                    imdb_id=movie.imdb_id,
                    arr=arr_refs.get(movie.id, []),
                    reason=record.reason,
                )
            )
            continue
        series = series_by_id.get(record.series_id or -1)
        if series is None:
            continue
        items.append(
            RuleMatchItem(
                target_scope=target_scope,
                media_type=record.media_type,
                series_id=series.id,
                season_number=season_numbers.get(season_id or -1),
                episode_number=episode.episode_number if episode else None,
                title=series.title,
                year=series.year,
                tmdb_id=series.tmdb_id,
                tvdb_id=series.tvdb_id,
                imdb_id=series.imdb_id,
                arr=arr_refs.get(series.id, []),
                reason=record.reason,
            )
        )
    return items


@router.get("/rules", response_model=RuleListResponse)
async def list_rules(
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_RULES_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RuleListResponse:
    rules = (
        (
            await db.execute(
                select(ReclaimRule).order_by(ReclaimRule.name, ReclaimRule.id)
            )
        )
        .scalars()
        .all()
    )
    return RuleListResponse(items=[_rule_response(rule) for rule in rules])


@router.get("/rules/{rule_id}/matches", response_model=RuleMatchesResponse)
async def get_rule_matches(
    rule_id: int,
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_RULES_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RuleMatchesResponse:
    rule = await db.get(ReclaimRule, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found"
        )
    if normalize_rule_outcome(rule) != RULE_OUTCOME_MATCH:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Only match-only rules can be evaluated here; "
                "use /api/v1/candidates for cleanup rules"
            ),
        )
    # disabling the rule is the user's kill switch for scripts that act on it
    if not rule.enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Rule is disabled"
        )
    target_scope = normalize_rule_target(rule)
    async with _evaluation_lock:
        result = await collect_rule_preview_matches_with_metadata(db, [rule])
        items = await _match_items(db, target_scope, result.matches)

    warnings = _match_warnings(result.metadata)
    return RuleMatchesResponse(
        rule=RuleMatchRuleRef(
            id=rule.id,
            name=rule.name,
            media_type=rule.media_type,
            target_scope=target_scope,
        ),
        complete=not warnings,
        warnings=warnings,
        total=len(items),
        items=items,
    )
