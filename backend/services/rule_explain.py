"""Targeted rule explanations using the normal preview preparation and filters."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.protection_scope import active_protection_clause
from backend.core.rule_engine import explain_advanced_rule
from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    ProtectedMedia,
    ReclaimRule,
    Season,
    Series,
)
from backend.models.rules import (
    RuleExplainNode,
    RuleExplainResponse,
    RuleExplainTarget,
    RuleExplainTargetsResponse,
    RuleTargetScope,
)
from backend.tasks import cleanup


class RuleExplainTargetNotFound(LookupError):
    pass


def _target_query(scope: RuleTargetScope):
    empty = literal(None)
    if scope == "movie_version":
        return select(
            MovieVersion.id,
            Movie.title,
            Movie.year,
            MovieVersion.path,
            empty.label("season_number"),
            empty.label("episode_number"),
            MovieVersion.library_name.label("detail"),
        ).join(Movie, Movie.id == MovieVersion.movie_id)
    if scope == "series":
        return select(
            Series.id,
            Series.title,
            Series.year,
            empty.label("path"),
            empty.label("season_number"),
            empty.label("episode_number"),
            empty.label("detail"),
        )
    if scope == "season":
        return select(
            Season.id,
            Series.title,
            Series.year,
            Season.path,
            Season.season_number,
            empty.label("episode_number"),
            empty.label("detail"),
        ).join(Series, Series.id == Season.series_id)
    return (
        select(
            Episode.id,
            Series.title,
            Series.year,
            Episode.path,
            Season.season_number,
            Episode.episode_number,
            Episode.name.label("detail"),
        )
        .join(Season, Season.id == Episode.season_id)
        .join(Series, Series.id == Season.series_id)
    )


def _target_label(row: Any) -> RuleExplainTarget:
    label = row.title + (f" ({row.year})" if row.year else "")
    if row.season_number is not None:
        label += f" · S{row.season_number:02d}"
    if row.episode_number is not None:
        label += f"E{row.episode_number:02d}"
    if row.detail:
        label += f" · {row.detail}"
    return RuleExplainTarget(id=row.id, label=label, path=row.path)


async def list_rule_explain_targets(
    db: AsyncSession,
    scope: RuleTargetScope,
    search: str,
    page: int,
    per_page: int,
) -> RuleExplainTargetsResponse:
    # Include protected, removed and empty items: explaining their absence is the point.
    query = _target_query(scope)
    columns = query.selected_columns
    if search.strip():
        query = query.where(
            or_(
                columns.title.icontains(search.strip(), autoescape=True),
                columns.path.icontains(search.strip(), autoescape=True),
                columns.detail.icontains(search.strip(), autoescape=True),
            )
        )
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.execute(
        query.order_by(columns.title, columns.id)
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return RuleExplainTargetsResponse(
        items=[_target_label(row) for row in rows],
        total=total or 0,
        page=page,
        per_page=per_page,
    )


async def _load_context(
    db: AsyncSession, scope: RuleTargetScope, target_id: int
) -> dict[str, Any]:
    if scope == "movie_version":
        movie = await db.scalar(
            select(Movie)
            .where(Movie.versions.any(MovieVersion.id == target_id))
            .options(selectinload(Movie.versions))
        )
        if movie is not None:
            version = next((v for v in movie.versions if v.id == target_id), None)
            if version is not None:
                return {"movie": movie, "version": version}
    else:
        query = select(Series).options(
            selectinload(Series.service_refs),
            selectinload(Series.seasons).selectinload(Season.episodes),
        )
        if scope == "series":
            query = query.where(Series.id == target_id)
        elif scope == "season":
            query = query.where(Series.seasons.any(Season.id == target_id))
        else:
            query = query.where(
                Series.seasons.any(Season.episodes.any(Episode.id == target_id))
            )
        series = await db.scalar(query)
        if series is not None:
            context: dict[str, Any] = {"series": series}
            if scope == "series":
                return context
            for season in series.seasons:
                if scope == "season" and season.id == target_id:
                    return {**context, "season": season}
                for episode in season.episodes:
                    if scope == "episode" and episode.id == target_id:
                        return {**context, "season": season, "episode": episode}
    raise RuleExplainTargetNotFound(
        "The selected media item no longer exists. Search again."
    )


async def _exclusions(
    db: AsyncSession, rule: ReclaimRule, context: dict[str, Any]
) -> list[str]:
    exclusions: list[str] = []
    movie, version = context.get("movie"), context.get("version")
    series, season, episode = (
        context.get("series"),
        context.get("season"),
        context.get("episode"),
    )
    parent = movie if movie is not None else series
    assert parent is not None
    if parent.removed_at is not None:
        exclusions.append("This title is marked as removed from the library.")
    scope = rule.target_scope
    if version is not None:
        assert movie is not None
        effective_size = (
            version.size if version.size and version.size > 0 else movie.size
        )
        if not effective_size:
            exclusions.append("No file size is available for this movie version.")
    if scope == "series":
        assert series is not None
        if not any(
            cleanup._has_media_path(s.path) and cleanup._season_holds_media(s)
            for s in series.seasons
        ):
            exclusions.append(
                "This show has no season with a media path and indexed media."
            )
    if scope == "season":
        assert season is not None
        if not cleanup._has_media_path(season.path):
            exclusions.append("This season has no media path.")
        if not cleanup._season_holds_media(season) or not season.size:
            exclusions.append("This season has no indexed media with a known size.")
    if episode is not None and not cleanup._has_media_path(episode.path):
        exclusions.append("This episode has no media file path.")
    if (rule.action or {}).get("outcome", "candidate") != "candidate":
        return exclusions
    enabled, all_users, usernames = await cleanup._load_favorites_policy(db)
    if enabled:
        favorite_ids = await cleanup._load_favorite_tmdb_ids(
            db,
            media_type=rule.media_type,
            protect_all_users=all_users,
            usernames=usernames,
        )
        if parent.tmdb_id in favorite_ids:
            exclusions.append("Excluded by the favorites/watchlist protection setting.")
    query = select(ProtectedMedia).where(
        active_protection_clause(datetime.now(UTC)),
        ProtectedMedia.media_type == rule.media_type,
    )
    if movie is not None:
        assert version is not None
        query = query.where(
            ProtectedMedia.movie_id == movie.id,
            or_(
                ProtectedMedia.movie_version_id.is_(None),
                ProtectedMedia.movie_version_id == version.id,
            ),
        )
    else:
        assert series is not None
        query = query.where(ProtectedMedia.series_id == series.id)
        if scope == "season":
            assert season is not None
            query = query.where(
                or_(
                    cleanup._is_series_scope(ProtectedMedia),
                    ProtectedMedia.season_id == season.id,
                )
            ).where(ProtectedMedia.episode_id.is_(None))
        elif scope == "episode":
            assert season is not None and episode is not None
            peers = await cleanup._episode_file_peers(db, [series.id])
            query = query.where(
                or_(
                    cleanup._is_series_scope(ProtectedMedia),
                    (ProtectedMedia.season_id == season.id)
                    & ProtectedMedia.episode_id.is_(None),
                    ProtectedMedia.episode_id.in_(
                        {episode.id, *peers.get(episode.id, set())}
                    ),
                )
            )
    protections = (await db.scalars(query)).all()
    for protection in protections:
        source = (
            "an automated protection rule"
            if protection.source == "rule"
            else "an active protection"
        )
        reason = f" Reason: {protection.reason}" if protection.reason else ""
        exclusions.append(f"Excluded by {source}.{reason}")
    return exclusions


async def explain_rule_target(
    db: AsyncSession, rule: ReclaimRule, scope: RuleTargetScope, target_id: int
) -> RuleExplainResponse:
    context = await _load_context(db, scope, target_id)
    query = _target_query(scope)
    row = (await db.execute(query.where(query.selected_columns.id == target_id))).one()
    # This uses exactly the same data refresh, resolvers, inventory and safeguards as Preview.
    preview = await cleanup.collect_rule_preview_matches_with_metadata(
        db, [rule], target_ids_by_scope={scope: {target_id}}
    )
    root = RuleExplainNode.model_validate(
        explain_advanced_rule(rule, target_scope=scope, **context)
    )
    exclusions = await _exclusions(db, rule, context)
    warnings = list(
        dict.fromkeys(
            error
            for error in (
                preview.metadata.sonarr_error,
                preview.metadata.playback_error,
                preview.metadata.seerr_error,
            )
            if error
        )
    )
    eligible = bool(preview.matches)
    if root.state == "pass" and not eligible and not exclusions:
        exclusions.append(
            "The conditions pass, but a preview prerequisite blocked this item. "
            + (" ".join(warnings) or "Check source inventory and service availability.")
        )
    return RuleExplainResponse(
        target=_target_label(row),
        target_scope=scope,
        outcome=(rule.action or {}).get("outcome", "candidate"),
        condition_state=root.state,
        eligible=eligible,
        exclusions=exclusions,
        warnings=warnings,
        root=root,
    )
