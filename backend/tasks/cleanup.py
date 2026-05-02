from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.logger import LOG
from backend.core.rule_engine import (
    TARGET_MOVIE_VERSION,
    TARGET_SEASON,
    TARGET_SERIES,
    evaluate_advanced_rule,
    normalize_rule_target,
)
from backend.core.service_manager import service_manager
from backend.core.task_tracking import track_task_execution
from backend.database import async_db
from backend.database.models import (
    Movie,
    MovieArrRef,
    MovieVersion,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
    ReclaimHistory,
    ReclaimRule,
    Season,
    Series,
    SeriesArrRef,
)
from backend.enums import MediaType, NotificationType, Service, Task
from backend.models.cleanup import MatchedCandidateRecord
from backend.services.notifications import notify_all_users

__all__ = [
    "scan_cleanup_candidates",
    "tag_cleanup_candidates",
    # "delete_cleanup_candidates",
    "delete_specific_candidates",
    "collect_rule_preview_matches",
]


async def collect_rule_preview_matches(
    db: AsyncSession,
    rules: list[ReclaimRule],
) -> list[MatchedCandidateRecord]:
    """Evaluate rules without mutating persisted candidates."""

    movie_rules = [r for r in rules if normalize_rule_target(r) == TARGET_MOVIE_VERSION]
    series_rules = [r for r in rules if normalize_rule_target(r) == TARGET_SERIES]
    season_rules = [r for r in rules if normalize_rule_target(r) == TARGET_SEASON]

    matches: list[MatchedCandidateRecord] = []
    if movie_rules:
        matches.extend(await _collect_movie_version_candidate_records(db, movie_rules))
    if series_rules:
        matches.extend(await _collect_series_candidate_records(db, series_rules))
    if season_rules:
        matches.extend(await _collect_season_candidate_records(db, season_rules))
    return matches


async def scan_cleanup_candidates() -> None:
    """Scan media libraries and identify cleanup candidates based on configured rules."""
    LOG.info("Starting cleanup candidates scan")

    async with track_task_execution(Task.SCAN_CLEANUP_CANDIDATES):
        try:
            async with async_db() as session:
                response = await _scan_with_db(session)
                if response and response[0] > 0:
                    try:
                        await notify_all_users(
                            notification_type=NotificationType.NEW_CLEANUP_CANDIDATES,
                            title="New Cleanup Candidates Found",
                            message=f"There are {response[0]} new cleanup candidates",
                        )
                    except Exception as e:
                        LOG.error(f"Error sending cleanup scan notification: {e}")
        except Exception as e:
            LOG.error(f"Error scanning cleanup candidates: {e}", exc_info=True)
            raise


async def _scan_with_db(db: AsyncSession) -> tuple[int, int, int] | None:
    """Internal method to perform scan with database session.

    Returns (created_count, updated_count, removed_count) or None if no rules found."""
    try:
        # load all enabled cleanup rules
        result = await db.execute(
            select(ReclaimRule).where(ReclaimRule.enabled == True)
        )
        rules = result.scalars().all()

        # Separate rules by explicit advanced target. Rules without a valid
        # advanced definition are skipped by the evaluator and will not match.
        movie_rules = [
            r for r in rules if normalize_rule_target(r) == TARGET_MOVIE_VERSION
        ]
        series_rules = [r for r in rules if normalize_rule_target(r) == TARGET_SERIES]
        season_rules = [r for r in rules if normalize_rule_target(r) == TARGET_SEASON]

        if not rules:
            LOG.info("No enabled cleanup rules found, clearing all candidates")
            await db.execute(delete(ReclaimCandidate))
            await db.commit()
            return

        LOG.info(f"Found {len(rules)} enabled cleanup rules")

        candidates_created = 0
        candidates_updated = 0
        candidates_removed = 0

        # process movies
        if movie_rules:
            created, updated, removed = await _process_media(
                db, movie_rules, MediaType.MOVIE
            )
            candidates_created += created
            candidates_updated += updated
            candidates_removed += removed
        else:
            # no movie rules active - remove stale movie candidates
            del_result = await db.execute(
                delete(ReclaimCandidate).where(
                    ReclaimCandidate.media_type == MediaType.MOVIE
                )
            )
            candidates_removed += del_result.rowcount or 0  # pyright: ignore[reportAttributeAccessIssue]
            await db.commit()

        # process series
        if series_rules:
            created, updated, removed = await _process_media(
                db, series_rules, MediaType.SERIES
            )
            candidates_created += created
            candidates_updated += updated
            candidates_removed += removed
        else:
            del_result = await db.execute(
                delete(ReclaimCandidate).where(
                    ReclaimCandidate.media_type == MediaType.SERIES,
                    ReclaimCandidate.season_id.is_(None),
                )
            )
            candidates_removed += del_result.rowcount or 0  # pyright: ignore[reportAttributeAccessIssue]
            await db.commit()

        if season_rules:
            s_cr, s_up, s_rm = await _process_series_seasons(db, season_rules)
            candidates_created += s_cr
            candidates_updated += s_up
            candidates_removed += s_rm
        else:
            del_result = await db.execute(
                delete(ReclaimCandidate).where(
                    ReclaimCandidate.media_type == MediaType.SERIES,
                    ReclaimCandidate.season_id.isnot(None),
                )
            )
            candidates_removed += del_result.rowcount or 0  # pyright: ignore[reportAttributeAccessIssue]
            await db.commit()

        LOG.info(
            f"Cleanup scan completed: {candidates_created} new candidates, "
            f"{candidates_updated} updated, {candidates_removed} removed"
        )

        return candidates_created, candidates_updated, candidates_removed
    except Exception:
        raise


async def _process_media(
    db: AsyncSession, rules: list[ReclaimRule], media_type: MediaType
) -> tuple[int, int, int]:
    """
    Process movies or series against cleanup rules.

    Returns (created_count, updated_count, removed_count)
    """
    if media_type is MediaType.MOVIE:
        return await _process_movie_versions(db, rules)

    records = await _collect_series_candidate_records(db, rules)
    return await _sync_series_candidates(db, records)


async def _collect_series_candidate_records(
    db: AsyncSession,
    rules: list[ReclaimRule],
) -> list[MatchedCandidateRecord]:
    """Evaluate whole-series rules without mutating persisted candidates."""

    # get all media items
    result = await db.execute(
        select(Series)
        .where(Series.removed_at.is_(None))
        .options(selectinload(Series.service_refs))
    )
    media_items = result.scalars().all()

    LOG.info(f"Processing {len(media_items)} {MediaType.SERIES.value} items")

    # fetch all protected items for this media type to skip them
    now = datetime.now(UTC)
    protected_result = await db.execute(
        select(ProtectedMedia).where(
            ProtectedMedia.media_type == MediaType.SERIES,
            ProtectedMedia.series_id.isnot(None),
            or_(
                ProtectedMedia.permanent.is_(True),
                ProtectedMedia.expires_at.is_(None),
                ProtectedMedia.expires_at > now,
            ),
        )
    )
    protected_ids = {b.series_id for b in protected_result.scalars().all()}

    LOG.info(
        f"Found {len(protected_ids)} protected {MediaType.SERIES.value} items to skip"
    )

    records: list[MatchedCandidateRecord] = []

    for item in media_items:
        # skip protected items
        if item.id in protected_ids:
            continue

        # evaluate all rules against this item
        matched_rules = []
        matched_criteria = {}
        reasons: list[dict[str, Any]] = []

        for rule in rules:
            if _evaluate_movie_rule(item, rule, matched_criteria, reasons):
                matched_rules.append(rule.id)

        if not matched_rules:
            continue

        records.append(
            MatchedCandidateRecord(
                media_type=MediaType.SERIES,
                series_id=item.id,
                matched_rule_ids=matched_rules,
                matched_criteria=matched_criteria,
                reason=_rule_reason_text(reasons),
                reason_data=reasons,
                estimated_space_gb=item.size / (1024**3) if item.size else None,
            )
        )

    return records


async def _sync_series_candidates(
    db: AsyncSession,
    records: list[MatchedCandidateRecord],
) -> tuple[int, int, int]:
    """Synchronize series candidates with the database."""
    result = await db.execute(
        select(ReclaimCandidate).where(
            ReclaimCandidate.media_type == MediaType.SERIES,
            ReclaimCandidate.season_id.is_(None),
        )
    )
    existing_candidates = result.scalars().all()
    candidate_lookup = {
        candidate.series_id: candidate
        for candidate in existing_candidates
        if candidate.series_id is not None
    }
    matched_item_ids = {
        record.series_id for record in records if record.series_id is not None
    }

    candidates_created = 0
    candidates_updated = 0
    for record in records:
        if record.series_id is None:
            continue
        existing = candidate_lookup.get(record.series_id)
        if existing:
            existing.matched_rule_ids = record.matched_rule_ids
            existing.matched_criteria = record.matched_criteria
            existing.reason = record.reason
            existing.reason_data = record.reason_data
            existing.estimated_space_gb = record.estimated_space_gb
            existing.updated_at = datetime.now(UTC)
            candidates_updated += 1
        else:
            db.add(
                ReclaimCandidate(
                    media_type=MediaType.SERIES,
                    series_id=record.series_id,
                    matched_rule_ids=record.matched_rule_ids,
                    matched_criteria=record.matched_criteria,
                    reason=record.reason,
                    reason_data=record.reason_data,
                    estimated_space_gb=record.estimated_space_gb,
                )
            )
            candidates_created += 1

    stale_candidates = [
        candidate
        for series_id, candidate in candidate_lookup.items()
        if series_id not in matched_item_ids
    ]
    candidates_removed = len(stale_candidates)
    for candidate in stale_candidates:
        await db.delete(candidate)

    if candidates_created > 0 or candidates_updated > 0 or candidates_removed > 0:
        await db.commit()

    return candidates_created, candidates_updated, candidates_removed


async def _process_movie_versions(
    db: AsyncSession,
    rules: list[ReclaimRule],
) -> tuple[int, int, int]:
    """Evaluate movie rules at movie-version granularity."""
    records = await _collect_movie_version_candidate_records(db, rules)
    return await _sync_movie_version_candidates(db, records)


async def _collect_movie_version_candidate_records(
    db: AsyncSession,
    rules: list[ReclaimRule],
) -> list[MatchedCandidateRecord]:
    """Evaluate movie-version rules without mutating persisted candidates."""
    result = await db.execute(
        select(Movie)
        .where(Movie.removed_at.is_(None))
        .options(selectinload(Movie.versions))
    )
    movies = result.scalars().all()
    LOG.info(f"Processing {len(movies)} movie items at version granularity")

    now = datetime.now(UTC)
    protected_result = await db.execute(
        select(ProtectedMedia).where(
            ProtectedMedia.media_type == MediaType.MOVIE,
            ProtectedMedia.movie_id.isnot(None),
            or_(
                ProtectedMedia.permanent.is_(True),
                ProtectedMedia.expires_at.is_(None),
                ProtectedMedia.expires_at > now,
            ),
        )
    )
    protected_rows = protected_result.scalars().all()
    protected_movie_ids = {
        p.movie_id
        for p in protected_rows
        if p.movie_id is not None and p.movie_version_id is None
    }
    protected_version_ids = {
        p.movie_version_id for p in protected_rows if p.movie_version_id is not None
    }
    LOG.info(
        f"Found {len(protected_movie_ids)} protected movies and "
        f"{len(protected_version_ids)} protected movie versions to skip"
    )

    records: list[MatchedCandidateRecord] = []

    for movie in movies:
        if movie.id in protected_movie_ids:
            continue
        if not movie.versions:
            continue

        for version in movie.versions:
            if version.id in protected_version_ids:
                continue

            matched_rules: list[int] = []
            matched_criteria: dict = {}
            reasons: list[dict[str, Any]] = []

            for rule in rules:
                if _evaluate_movie_version_rule(
                    movie, version, rule, matched_criteria, reasons
                ):
                    matched_rules.append(rule.id)

            if not matched_rules:
                continue

            candidate_size = (
                version.size if version.size and version.size > 0 else movie.size
            )
            records.append(
                MatchedCandidateRecord(
                    media_type=MediaType.MOVIE,
                    movie_id=movie.id,
                    movie_version_id=version.id,
                    matched_rule_ids=matched_rules,
                    matched_criteria=matched_criteria,
                    reason=_rule_reason_text(reasons),
                    reason_data=reasons,
                    estimated_space_gb=candidate_size / (1024**3)
                    if candidate_size
                    else None,
                )
            )

    return records


async def _sync_movie_version_candidates(
    db: AsyncSession,
    records: list[MatchedCandidateRecord],
) -> tuple[int, int, int]:
    """Synchronize movie version candidates with the database."""
    existing_result = await db.execute(
        select(ReclaimCandidate).where(ReclaimCandidate.media_type == MediaType.MOVIE)
    )
    existing_candidates = existing_result.scalars().all()
    version_candidate_lookup: dict[int, ReclaimCandidate] = {
        candidate.movie_version_id: candidate
        for candidate in existing_candidates
        if candidate.movie_version_id is not None
    }
    legacy_movie_candidates = [
        candidate
        for candidate in existing_candidates
        if candidate.movie_version_id is None
    ]

    matched_version_ids = {
        record.movie_version_id
        for record in records
        if record.movie_version_id is not None
    }

    candidates_created = 0
    candidates_updated = 0
    for record in records:
        if record.movie_version_id is None or record.movie_id is None:
            continue
        existing = version_candidate_lookup.get(record.movie_version_id)
        if existing:
            existing.matched_rule_ids = record.matched_rule_ids
            existing.matched_criteria = record.matched_criteria
            existing.reason = record.reason
            existing.reason_data = record.reason_data
            existing.estimated_space_gb = record.estimated_space_gb
            existing.movie_id = record.movie_id
            existing.updated_at = datetime.now(UTC)
            candidates_updated += 1
        else:
            db.add(
                ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    movie_id=record.movie_id,
                    movie_version_id=record.movie_version_id,
                    matched_rule_ids=record.matched_rule_ids,
                    matched_criteria=record.matched_criteria,
                    reason=record.reason,
                    reason_data=record.reason_data,
                    estimated_space_gb=record.estimated_space_gb,
                )
            )
            candidates_created += 1

    stale_version_candidates = [
        candidate
        for version_id, candidate in version_candidate_lookup.items()
        if version_id not in matched_version_ids
    ]
    candidates_removed = len(stale_version_candidates) + len(legacy_movie_candidates)
    for candidate in stale_version_candidates:
        await db.delete(candidate)
    for candidate in legacy_movie_candidates:
        await db.delete(candidate)

    if candidates_created > 0 or candidates_updated > 0 or candidates_removed > 0:
        await db.commit()

    return candidates_created, candidates_updated, candidates_removed


async def _process_series_seasons(
    db: AsyncSession, rules: list[ReclaimRule]
) -> tuple[int, int, int]:
    """Evaluate each series' seasons against rules and create/update season-level candidates.

    Returns (created_count, updated_count, removed_count).
    """
    records = await _collect_season_candidate_records(db, rules)
    return await _sync_season_candidates(db, records)


async def _collect_season_candidate_records(
    db: AsyncSession,
    rules: list[ReclaimRule],
) -> list[MatchedCandidateRecord]:
    """Evaluate season rules without mutating persisted candidates."""
    # load all non-deleted series with their seasons and service refs
    result = await db.execute(
        select(Series)
        .where(Series.removed_at.is_(None))
        .options(selectinload(Series.service_refs), selectinload(Series.seasons))
    )
    all_series = result.scalars().all()

    if not all_series:
        return []

    # whole-series protection also covers every season of that series
    now = datetime.now(UTC)
    protected_series_result = await db.execute(
        select(ProtectedMedia).where(
            ProtectedMedia.media_type == MediaType.SERIES,
            ProtectedMedia.series_id.isnot(None),
            ProtectedMedia.season_id.is_(None),
            or_(
                ProtectedMedia.permanent.is_(True),
                ProtectedMedia.expires_at.is_(None),
                ProtectedMedia.expires_at > now,
            ),
        )
    )
    protected_series_ids = {
        b.series_id for b in protected_series_result.scalars().all()
    }

    # season-level protection entries
    protected_season_result = await db.execute(
        select(ProtectedMedia).where(
            ProtectedMedia.media_type == MediaType.SERIES,
            ProtectedMedia.season_id.isnot(None),
            or_(
                ProtectedMedia.permanent.is_(True),
                ProtectedMedia.expires_at.is_(None),
                ProtectedMedia.expires_at > now,
            ),
        )
    )
    protected_season_ids = {
        b.season_id for b in protected_season_result.scalars().all()
    }

    records: list[MatchedCandidateRecord] = []

    for series in all_series:
        if not series.seasons:
            continue
        if series.id in protected_series_ids:
            continue

        for season in series.seasons:
            if season.id in protected_season_ids:
                continue

            matched_rules: list[int] = []
            matched_criteria: dict = {}
            reasons: list[dict[str, Any]] = []

            for rule in rules:
                if _evaluate_rule_for_season(
                    series, season, rule, matched_criteria, reasons
                ):
                    matched_rules.append(rule.id)

            if not matched_rules:
                continue

            records.append(
                MatchedCandidateRecord(
                    media_type=MediaType.SERIES,
                    series_id=series.id,
                    season_id=season.id,
                    matched_rule_ids=matched_rules,
                    matched_criteria=matched_criteria,
                    reason=_rule_reason_text(reasons),
                    reason_data=reasons,
                    estimated_space_gb=season.size / (1024**3) if season.size else None,
                )
            )

    return records


async def _sync_season_candidates(
    db: AsyncSession,
    records: list[MatchedCandidateRecord],
) -> tuple[int, int, int]:
    """Synchronize season candidates with the database."""
    existing_result = await db.execute(
        select(ReclaimCandidate).where(
            ReclaimCandidate.media_type == MediaType.SERIES,
            ReclaimCandidate.season_id.isnot(None),
        )
    )
    season_candidate_lookup: dict[int, ReclaimCandidate] = {
        candidate.season_id: candidate
        for candidate in existing_result.scalars().all()
        if candidate.season_id is not None
    }

    matched_season_ids = {
        record.season_id for record in records if record.season_id is not None
    }

    candidates_created = 0
    candidates_updated = 0
    for record in records:
        if record.season_id is None or record.series_id is None:
            continue
        existing = season_candidate_lookup.get(record.season_id)

        if existing:
            existing.matched_rule_ids = record.matched_rule_ids
            existing.matched_criteria = record.matched_criteria
            existing.reason = record.reason
            existing.reason_data = record.reason_data
            existing.estimated_space_gb = record.estimated_space_gb
            existing.updated_at = datetime.now(UTC)
            candidates_updated += 1
        else:
            db.add(
                ReclaimCandidate(
                    media_type=MediaType.SERIES,
                    matched_rule_ids=record.matched_rule_ids,
                    matched_criteria=record.matched_criteria,
                    reason=record.reason,
                    reason_data=record.reason_data,
                    estimated_space_gb=record.estimated_space_gb,
                    series_id=record.series_id,
                    season_id=record.season_id,
                )
            )
            candidates_created += 1

    stale_candidates = [
        candidate
        for season_id, candidate in season_candidate_lookup.items()
        if season_id not in matched_season_ids
    ]
    candidates_removed = len(stale_candidates)
    for candidate in stale_candidates:
        await db.delete(candidate)

    if candidates_created > 0 or candidates_updated > 0 or candidates_removed > 0:
        await db.commit()

    return candidates_created, candidates_updated, candidates_removed


def _append_rule_reason(
    reasons: list[dict[str, Any]],
    *,
    rule: ReclaimRule,
    target_scope: str,
    conditions: list[dict[str, Any]],
    season_label: str | None = None,
) -> None:
    """Append normalized reason data for a matched rule."""
    if not conditions:
        return

    label = rule.name if not season_label else f"{rule.name} ({season_label})"
    summary = ", ".join(
        str(condition.get("display", "")).strip()
        for condition in conditions
        if isinstance(condition, dict) and str(condition.get("display", "")).strip()
    )
    text = f"{label}: {summary}" if summary else label
    reasons.append(
        {
            "rule_id": rule.id,
            "rule_name": rule.name,
            "target_scope": target_scope,
            "season_label": season_label,
            "conditions": conditions,
            "text": text,
        }
    )


def _rule_reason_text(reasons: list[dict[str, Any]]) -> str:
    """Generate a human-readable text summary of rule reasons."""
    tokens = [
        str(part.get("text", "")).strip() for part in reasons if isinstance(part, dict)
    ]
    tokens = [token for token in tokens if token]
    return " | ".join(tokens) if tokens else "Matched cleanup rule"


def _evaluate_movie_rule(
    item: Movie | Series,
    rule: ReclaimRule,
    matched_criteria: dict,
    reasons: list[dict[str, Any]],
) -> bool:
    """Evaluate a whole-series rule, or any movie version for direct tests."""
    if isinstance(item, Movie):
        if normalize_rule_target(rule) != TARGET_MOVIE_VERSION:
            return False
        for version in item.versions:
            if _evaluate_movie_version_rule(
                item, version, rule, matched_criteria, reasons
            ):
                return True
        return False

    if normalize_rule_target(rule) != TARGET_SERIES or not item.size:
        return False

    matched, criteria, rule_reasons = evaluate_advanced_rule(
        rule, target_scope=TARGET_SERIES, series=item
    )
    if not matched:
        return False
    matched_criteria.update(criteria)
    _append_rule_reason(
        reasons,
        rule=rule,
        target_scope=TARGET_SERIES,
        conditions=rule_reasons,
    )
    return True


def _evaluate_movie_version_rule(
    movie: Movie,
    version: MovieVersion,
    rule: ReclaimRule,
    matched_criteria: dict,
    reasons: list[dict[str, Any]],
) -> bool:
    """Evaluate a movie rule against a single movie version candidate."""
    if normalize_rule_target(rule) != TARGET_MOVIE_VERSION:
        return False

    effective_size = version.size if version.size and version.size > 0 else movie.size
    if not effective_size or effective_size == 0:
        return False

    matched, criteria, rule_reasons = evaluate_advanced_rule(
        rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
    )
    if not matched:
        return False
    matched_criteria.update(criteria)
    matched_criteria["media_version_id"] = version.id
    matched_criteria["movie_id"] = movie.id
    _append_rule_reason(
        reasons,
        rule=rule,
        target_scope=TARGET_MOVIE_VERSION,
        conditions=rule_reasons,
    )
    return True


def _evaluate_rule_for_season(
    series: Series,
    season: Season,
    rule: ReclaimRule,
    matched_criteria: dict,
    reasons: list[dict[str, Any]],
) -> bool:
    """Evaluate a rule against a specific season of a series."""
    if normalize_rule_target(rule) != TARGET_SEASON:
        return False

    if not season.size or season.size == 0:
        return False

    label = f"S{season.season_number:02d}"
    matched, criteria, rule_reasons = evaluate_advanced_rule(
        rule, target_scope=TARGET_SEASON, series=series, season=season
    )
    if not matched:
        return False
    matched_criteria.update(criteria)
    _append_rule_reason(
        reasons,
        rule=rule,
        target_scope=TARGET_SEASON,
        conditions=rule_reasons,
        season_label=label,
    )
    return True


async def tag_cleanup_candidates() -> None:
    """Sync rule scoped rec-* tags for cleanup candidates in Radarr/Sonarr."""
    # check if services are configured before doing any work
    if not service_manager.radarr and not service_manager.sonarr:
        LOG.debug("Neither Radarr nor Sonarr configured, skipping tag sync")
        return

    LOG.info("Starting rule scoped cleanup candidate tagging")

    async with track_task_execution(Task.TAG_CLEANUP_CANDIDATES):
        try:
            movies_tagged, movies_untagged = await _sync_rule_radarr_tags()
            series_tagged, series_untagged = await _sync_rule_sonarr_tags()

            LOG.info(
                f"Tag sync completed: Movies ({movies_tagged} tagged, {movies_untagged} untagged), "
                f"Series ({series_tagged} tagged, {series_untagged} untagged)"
            )

        except Exception as e:
            LOG.error(f"Error syncing cleanup tags: {e}", exc_info=True)
            raise


def _rule_action(rule: ReclaimRule) -> dict:
    """Get the action dictionary for a rule, or an empty dictionary if none."""
    return rule.action or {}


def _managed_tag_for_rule(rule: ReclaimRule) -> str | None:
    """Determine the rec-* tag to manage for a given rule, or None if no tagging."""
    action = _rule_action(rule)
    if action.get("tag_enabled", True) is False:
        return None
    tag = str(action.get("arr_tag") or "").strip().lower()
    if not tag:
        return None
    return tag if tag.startswith("rec-") else f"rec-{tag}"


async def _sync_rule_radarr_tags() -> tuple[int, int]:
    """Synchronize Radarr tags for cleanup candidates."""
    if not service_manager.radarr:
        return 0, 0

    clients = service_manager.radarr_clients()
    if not clients and service_manager.radarr:
        clients = {0: service_manager.radarr}

    async with async_db() as db:
        rules = {
            rule.id: rule
            for rule in (
                await db.execute(select(ReclaimRule).where(ReclaimRule.enabled == True))
            )
            .scalars()
            .all()
        }
        candidate_rows = (
            await db.execute(
                select(
                    ReclaimCandidate,
                    Movie.id,
                    MovieArrRef.service_config_id,
                    MovieArrRef.arr_movie_id,
                    Movie.tmdb_id,
                )
                .join(Movie, Movie.id == ReclaimCandidate.movie_id)
                .outerjoin(MovieArrRef, MovieArrRef.movie_id == Movie.id)
                .where(ReclaimCandidate.media_type == MediaType.MOVIE)
            )
        ).all()

    by_config: dict[int, dict[str, set[int]]] = {}
    fallback_tmdb: dict[int, set[tuple[str, int]]] = {}
    for candidate, _movie_id, ref_config_id, arr_movie_id, tmdb_id in candidate_rows:
        for rule_id in candidate.matched_rule_ids or []:
            rule = rules.get(rule_id)
            if not rule:
                continue
            tag = _managed_tag_for_rule(rule)
            config_id = _rule_action(rule).get("radarr_service_config_id")
            if not tag or not isinstance(config_id, int):
                continue
            if ref_config_id == config_id and arr_movie_id is not None:
                by_config.setdefault(config_id, {}).setdefault(tag, set()).add(
                    arr_movie_id
                )
            elif tmdb_id:
                fallback_tmdb.setdefault(config_id, set()).add((tag, tmdb_id))

    total_tagged = 0
    total_untagged = 0
    for config_id, client in clients.items():
        expected = by_config.get(config_id, {})
        all_movies = await client.get_all_movies()
        tmdb_to_id = {movie.tmdb_id: movie.id for movie in all_movies if movie.tmdb_id}
        for tag, tmdb_id in fallback_tmdb.get(config_id, set()):
            resolved = tmdb_to_id.get(tmdb_id)
            if resolved is not None:
                expected.setdefault(tag, set()).add(resolved)
        tagged, untagged = await _sync_expected_arr_tags(
            client=client,
            items_by_id={movie.id: movie for movie in all_movies},
            expected_by_tag=expected,
            add_tag=client.add_tag_to_movies,
            remove_tag=client.remove_tag_from_movies,
            label="Radarr",
        )
        total_tagged += tagged
        total_untagged += untagged
    return total_tagged, total_untagged


async def _sync_rule_sonarr_tags() -> tuple[int, int]:
    """Synchronize Sonarr tags for cleanup candidates."""
    if not service_manager.sonarr:
        return 0, 0

    clients = service_manager.sonarr_clients()
    if not clients and service_manager.sonarr:
        clients = {0: service_manager.sonarr}

    async with async_db() as db:
        rules = {
            rule.id: rule
            for rule in (
                await db.execute(select(ReclaimRule).where(ReclaimRule.enabled == True))
            )
            .scalars()
            .all()
        }
        candidate_rows = (
            await db.execute(
                select(
                    ReclaimCandidate,
                    Series.id,
                    SeriesArrRef.service_config_id,
                    SeriesArrRef.arr_series_id,
                    Series.tmdb_id,
                )
                .join(Series, Series.id == ReclaimCandidate.series_id)
                .outerjoin(SeriesArrRef, SeriesArrRef.series_id == Series.id)
                .where(ReclaimCandidate.media_type == MediaType.SERIES)
            )
        ).all()

    by_config: dict[int, dict[str, set[int]]] = {}
    fallback_tmdb: dict[int, set[tuple[str, int]]] = {}
    for candidate, _series_id, ref_config_id, arr_series_id, tmdb_id in candidate_rows:
        for rule_id in candidate.matched_rule_ids or []:
            rule = rules.get(rule_id)
            if not rule:
                continue
            tag = _managed_tag_for_rule(rule)
            config_id = _rule_action(rule).get("sonarr_service_config_id")
            if not tag or not isinstance(config_id, int):
                continue
            if ref_config_id == config_id and arr_series_id is not None:
                by_config.setdefault(config_id, {}).setdefault(tag, set()).add(
                    arr_series_id
                )
            elif tmdb_id:
                fallback_tmdb.setdefault(config_id, set()).add((tag, int(tmdb_id)))

    total_tagged = 0
    total_untagged = 0
    for config_id, client in clients.items():
        expected = by_config.get(config_id, {})
        all_series = await client.get_all_series()
        tmdb_to_id = {
            series.tmdb_id: series.id for series in all_series if series.tmdb_id
        }
        for tag, tmdb_id in fallback_tmdb.get(config_id, set()):
            resolved = tmdb_to_id.get(tmdb_id)
            if resolved is not None:
                expected.setdefault(tag, set()).add(resolved)
        tagged, untagged = await _sync_expected_arr_tags(
            client=client,
            items_by_id={series.id: series for series in all_series},
            expected_by_tag=expected,
            add_tag=client.add_tag_to_series,
            remove_tag=client.remove_tag_from_series,
            label="Sonarr",
        )
        total_tagged += tagged
        total_untagged += untagged
    return total_tagged, total_untagged


async def _sync_expected_arr_tags(
    *,
    client,
    items_by_id: dict[int, object],
    expected_by_tag: dict[str, set[int]],
    add_tag,
    remove_tag,
    label: str,
) -> tuple[int, int]:
    """Sync expected tags for a set of items, adding/removing as needed. Returns
    (tagged_count, untagged_count)."""
    tags = await client.get_tags()
    active_labels = set(expected_by_tag)
    tag_by_label = {tag.label.lower(): tag for tag in tags}
    managed_stale_tags = [
        tag
        for tag in tags
        if (
            tag.label.lower().startswith("rec-")
            or tag.label.lower().startswith("reclaimerr")
        )
        and tag.label.lower() not in active_labels
    ]

    tagged_count = 0
    untagged_count = 0

    for tag_label, expected_ids in expected_by_tag.items():
        tag = tag_by_label.get(tag_label) or await client.get_or_create_tag(tag_label)
        tag_by_label[tag_label] = tag
        to_add = [
            item_id
            for item_id in expected_ids
            if item_id in items_by_id
            and tag.id not in getattr(items_by_id[item_id], "tags", [])
        ]
        if to_add:
            await add_tag(to_add, tag.id)
            tagged_count += len(to_add)
            LOG.info(f"Tagged {len(to_add)} {label} item(s) with {tag_label}")

    for stale_tag in managed_stale_tags:
        to_remove = [
            item_id
            for item_id, item in items_by_id.items()
            if stale_tag.id in getattr(item, "tags", [])
        ]
        if to_remove:
            await remove_tag(to_remove, stale_tag.id)
            untagged_count += len(to_remove)
            LOG.info(
                f"Removed stale managed tag '{stale_tag.label}' from {len(to_remove)} {label} item(s)"
            )

    for tag_label, tag in tag_by_label.items():
        if not tag_label.startswith("rec-") or tag_label not in active_labels:
            continue
        expected_ids = expected_by_tag.get(tag_label, set())
        to_remove = [
            item_id
            for item_id, item in items_by_id.items()
            if item_id not in expected_ids and tag.id in getattr(item, "tags", [])
        ]
        if to_remove:
            await remove_tag(to_remove, tag.id)
            untagged_count += len(to_remove)
            LOG.info(
                f"Removed tag '{tag.label}' from {len(to_remove)} non-candidate {label} item(s)"
            )

    return tagged_count, untagged_count


# NOT USING THIS YET BUT RE-WRITE IT WHEN WE AUTOMATE FULL DELETIONS IF NEEDED
# async def delete_cleanup_candidates() -> None:
#     """Deletes all cleanup candidates from their respective services.

#     Deletion is based purely on ReclaimCandidate records in the database.
#     Tags are optional visual indicators only and do not affect deletion.

#     Deletion priority:
#     1. Use Radarr (movies) or Sonarr (series) if item has radarr_id/sonarr_id
#     2. Fall back to Media Server deletion (Jellyfin/Emby/Plex) if no radarr_id/sonarr_id but media_refs
#     exist for that service

#     After deletion, resets the request in Seerr and marks item as removed in database.
#     """
#     LOG.info("Starting cleanup candidate deletion")

#     async with track_task_execution(Task.DELETE_CLEANUP_CANDIDATES):
#         try:
#             movies_deleted = 0
#             series_deleted = 0
#             season_deleted = 0

#             # process movies
#             if (
#                 service_manager.radarr
#                 or service_manager.jellyfin
#                 or service_manager.emby
#                 or service_manager.plex
#             ):
#                 movies_deleted = await _delete_movie_candidates()

#             # process series
#             if (
#                 service_manager.sonarr
#                 or service_manager.jellyfin
#                 or service_manager.emby
#                 or service_manager.plex
#             ):
#                 series_deleted = await _delete_series_candidates()
#                 season_deleted = await _delete_season_candidates()

#             LOG.info(
#                 f"Deletion completed: {movies_deleted} movies, "
#                 f"{series_deleted} whole series, {season_deleted} season(s) removed"
#             )

#         except Exception as e:
#             LOG.error(f"Error deleting cleanup candidates: {e}", exc_info=True)
#             raise


async def _mark_candidate_delete_failure(candidate_id: int, error: str) -> None:
    """Persist delete failure details for a candidate."""
    async with async_db() as db:
        result = await db.execute(
            select(ReclaimCandidate).where(ReclaimCandidate.id == candidate_id)
        )
        candidate = result.scalar_one_or_none()
        if candidate is None:
            return
        candidate.delete_attempts = (candidate.delete_attempts or 0) + 1
        candidate.last_delete_attempt_at = datetime.now(UTC)
        candidate.last_delete_error = error[:2000]
        await db.commit()


async def _delete_movie_version_candidates(
    version_candidates: list[ReclaimCandidate],
    approved_by: str = "system",
) -> int:
    """Delete movie-version candidates using service-aware targeted deletion."""
    if not version_candidates:
        return 0

    main_service = service_manager.main_media_server
    if not main_service:
        for candidate in version_candidates:
            await _mark_candidate_delete_failure(
                candidate.id, "No main media server configured"
            )
        return 0

    if main_service is service_manager.jellyfin:
        main_service_type = Service.JELLYFIN
    elif main_service is service_manager.emby:
        main_service_type = Service.EMBY
    else:
        main_service_type = Service.PLEX

    async with async_db() as db:
        version_ids = [
            c.movie_version_id for c in version_candidates if c.movie_version_id
        ]
        result = await db.execute(
            select(MovieVersion)
            .where(MovieVersion.id.in_(version_ids))
            .options(selectinload(MovieVersion.movie).selectinload(Movie.versions))
        )
        versions = {v.id: v for v in result.scalars().all()}

    deleted_count = 0
    for candidate in version_candidates:
        if candidate.movie_version_id is None:
            continue
        version = versions.get(candidate.movie_version_id)
        if version is None:
            await _mark_candidate_delete_failure(
                candidate.id, "Movie version missing from database"
            )
            continue
        movie = version.movie
        if movie is None:
            await _mark_candidate_delete_failure(
                candidate.id, "Movie missing for candidate version"
            )
            continue
        if version.service != main_service_type:
            await _mark_candidate_delete_failure(
                candidate.id,
                f"Version belongs to {version.service.value}, main media server is {main_service_type.value}",
            )
            continue

        same_item_versions = [
            v
            for v in movie.versions
            if v.service == main_service_type
            and v.service_item_id == version.service_item_id
        ]
        if main_service_type in {Service.JELLYFIN, Service.EMBY}:
            if len(same_item_versions) != 1:
                await _mark_candidate_delete_failure(
                    candidate.id,
                    "Exact version delete unsupported: multiple versions share one Emby/Jellyfin item id",
                )
                continue

        try:
            await main_service.delete_movie_version(
                version.service_item_id, version.service_media_id
            )

            async with async_db() as db:
                cand_result = await db.execute(
                    select(ReclaimCandidate).where(ReclaimCandidate.id == candidate.id)
                )
                cand = cand_result.scalar_one_or_none()
                if cand:
                    await db.delete(cand)

                ver_result = await db.execute(
                    select(MovieVersion).where(MovieVersion.id == version.id)
                )
                ver_db = ver_result.scalar_one_or_none()
                if ver_db:
                    deleted_size = ver_db.size or 0
                    movie_result = await db.execute(
                        select(Movie).where(Movie.id == ver_db.movie_id)
                    )
                    movie_db = movie_result.scalar_one_or_none()
                    if movie_db and movie_db.size:
                        movie_db.size = max(0, movie_db.size - deleted_size)
                    await db.delete(ver_db)

                db.add(
                    ReclaimHistory(
                        approved_by=approved_by,
                        media_type=MediaType.MOVIE,
                        tmdb_id=movie.tmdb_id,
                        name=movie.title,
                        path=version.path,
                        size=version.size,
                    )
                )

                await db.commit()

            deleted_count += 1
            LOG.info(
                f"Deleted movie version {version.id} for '{movie.title}' via {main_service_type.value}"
            )
        except Exception as e:
            await _mark_candidate_delete_failure(
                candidate.id,
                f"Version delete failed via {main_service_type.value}: {e}",
            )

    return deleted_count


async def _delete_movie_candidates(
    restrict_to_ids: frozenset[int] | None = None,
    approved_by: str = "system",
) -> int:
    """Delete movie candidates. Returns count of deleted candidates."""
    deleted_count = 0

    # get all movie candidates from database
    async with async_db() as db:
        query = (
            select(ReclaimCandidate)
            .join(Movie, ReclaimCandidate.movie_id == Movie.id)
            .where(ReclaimCandidate.media_type == MediaType.MOVIE)
            .where(Movie.removed_at.is_(None))
        )
        if restrict_to_ids:
            query = query.where(ReclaimCandidate.id.in_(restrict_to_ids))
        candidates = (await db.execute(query)).scalars().all()

        if not candidates:
            LOG.debug("No movie candidates to delete")
            return 0

        LOG.info(f"Found {len(candidates)} movie candidates to evaluate for deletion")

    whole_movie_candidates = [c for c in candidates if c.movie_version_id is None]
    version_candidates = [c for c in candidates if c.movie_version_id is not None]

    if version_candidates:
        deleted_count += await _delete_movie_version_candidates(
            version_candidates, approved_by
        )

    if not whole_movie_candidates:
        return deleted_count

    async with async_db() as db:
        result = await db.execute(
            select(
                Movie.id,
                Movie.title,
                Movie.tmdb_id,
                MovieArrRef.service_config_id,
                MovieArrRef.arr_movie_id,
            )
            .join(ReclaimCandidate, Movie.id == ReclaimCandidate.movie_id)
            .outerjoin(MovieArrRef, MovieArrRef.movie_id == Movie.id)
            .where(ReclaimCandidate.media_type == MediaType.MOVIE)
            .where(ReclaimCandidate.movie_version_id.is_(None))
            .where(Movie.removed_at.is_(None))
        )
        rows = result.all()
        movie_data: dict[int, dict] = {}
        for movie_id, title, tmdb_id, config_id, arr_movie_id in rows:
            info = movie_data.setdefault(
                movie_id,
                {"title": title, "tmdb_id": tmdb_id, "refs": []},
            )
            if config_id is not None and arr_movie_id is not None:
                info["refs"].append((config_id, arr_movie_id))

    radarr_clients = service_manager.radarr_clients()
    if not radarr_clients and service_manager.radarr:
        radarr_clients = {0: service_manager.radarr}

    movies_to_delete_by_config: dict[int, list[dict]] = {}
    if radarr_clients:
        for candidate in whole_movie_candidates:
            if candidate.movie_id is None:
                continue
            movie_info = movie_data.get(candidate.movie_id)
            if not movie_info:
                continue
            for config_id, arr_movie_id in movie_info["refs"]:
                if config_id in radarr_clients:
                    movies_to_delete_by_config.setdefault(config_id, []).append(
                        {
                            "candidate_id": candidate.id,
                            "movie_id": candidate.movie_id,
                            "radarr_id": arr_movie_id,
                            "title": movie_info["title"],
                            "method": "radarr",
                            "tmdb_id": movie_info["tmdb_id"],
                        }
                    )
                    break

    movies_to_delete: list[dict] = []
    for config_id, batch in movies_to_delete_by_config.items():
        client = radarr_clients.get(config_id)
        if not client:
            continue
        LOG.info(f"Deleting {len(batch)} movies via Radarr config {config_id}")
        radarr_ids = [m["radarr_id"] for m in batch]
        try:
            await client.delete_movies(radarr_ids)
            movies_to_delete.extend(batch)
        except Exception as e:
            LOG.error(
                f"Error deleting movies via Radarr config {config_id}: {e}",
                exc_info=True,
            )

    if movies_to_delete:
        try:
            async with async_db() as db:
                for movie_info in movies_to_delete:
                    # update movie record
                    result = await db.execute(
                        select(Movie).where(Movie.id == movie_info["movie_id"])
                    )
                    movie = result.scalar_one_or_none()
                    if movie:
                        movie.removed_at = datetime.now(UTC)

                    # delete cleanup candidate
                    result = await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.id == movie_info["candidate_id"]
                        )
                    )
                    candidate = result.scalar_one_or_none()
                    if candidate:
                        await db.delete(candidate)

                    # reset seerr request if available
                    movie_tmdb_id = movie_info.get("tmdb_id")
                    if service_manager.seerr and movie and movie_tmdb_id:
                        try:
                            await _reset_seerr_request(movie_tmdb_id, MediaType.MOVIE)
                        except Exception as e:
                            LOG.warning(
                                f"Failed to reset Seerr request for {movie_info['title']}: {e}"
                            )

                    db.add(
                        ReclaimHistory(
                            approved_by=approved_by,
                            media_type=MediaType.MOVIE,
                            tmdb_id=movie_info.get("tmdb_id"),
                            name=movie_info.get("title"),
                            size=movie.size if movie else None,
                        )
                    )

                await db.commit()

            deleted_count = len(movies_to_delete)
            LOG.info(f"Successfully deleted {deleted_count} movies via Radarr")
        except Exception as e:
            LOG.error(f"Error finalizing movie deletion state: {e}", exc_info=True)

    # fallback to media server deletion (Jellyfin/Emby/Plex) for candidates not in Radarr
    if not movies_to_delete or (len(whole_movie_candidates) > len(movies_to_delete)):
        deleted_count += await _delete_movies_via_media_server(
            whole_movie_candidates, movies_to_delete, approved_by
        )

    return deleted_count


async def _delete_series_candidates(
    restrict_to_ids: frozenset[int] | None = None,
    approved_by: str = "system",
) -> int:
    """Delete series candidates. Returns count of deleted series."""
    deleted_count = 0

    # get all series candidates from database
    async with async_db() as db:
        query = (
            select(ReclaimCandidate)
            .join(Series, ReclaimCandidate.series_id == Series.id)
            .where(ReclaimCandidate.media_type == MediaType.SERIES)
            .where(ReclaimCandidate.season_id.is_(None))  # series-level only
            .where(Series.removed_at.is_(None))
        )
        if restrict_to_ids:
            query = query.where(ReclaimCandidate.id.in_(restrict_to_ids))
        candidates = (await db.execute(query)).scalars().all()

        if not candidates:
            LOG.debug("No series candidates to delete")
            return 0

        LOG.info(f"Found {len(candidates)} series candidates to evaluate for deletion")

        result = await db.execute(
            select(
                Series.id,
                Series.title,
                Series.tmdb_id,
                SeriesArrRef.service_config_id,
                SeriesArrRef.arr_series_id,
            )
            .join(ReclaimCandidate, Series.id == ReclaimCandidate.series_id)
            .outerjoin(SeriesArrRef, SeriesArrRef.series_id == Series.id)
            .where(ReclaimCandidate.media_type == MediaType.SERIES)
            .where(Series.removed_at.is_(None))
        )
        rows = result.all()
        series_data: dict[int, dict] = {}
        for series_id, title, tmdb_id, config_id, arr_series_id in rows:
            info = series_data.setdefault(
                series_id,
                {"title": title, "tmdb_id": tmdb_id, "refs": []},
            )
            if config_id is not None and arr_series_id is not None:
                info["refs"].append((config_id, arr_series_id))

    sonarr_clients = service_manager.sonarr_clients()
    if not sonarr_clients and service_manager.sonarr:
        sonarr_clients = {0: service_manager.sonarr}

    series_to_delete_by_config: dict[int, list[dict]] = {}
    if sonarr_clients:
        for candidate in candidates:
            if candidate.series_id is None:
                continue
            series_info = series_data.get(candidate.series_id)
            if not series_info:
                continue
            for config_id, arr_series_id in series_info["refs"]:
                if config_id in sonarr_clients:
                    series_to_delete_by_config.setdefault(config_id, []).append(
                        {
                            "candidate_id": candidate.id,
                            "series_id": candidate.series_id,
                            "sonarr_id": arr_series_id,
                            "title": series_info["title"],
                            "method": "sonarr",
                            "tmdb_id": series_info["tmdb_id"],
                        }
                    )
                    break

    series_to_delete: list[dict] = []
    for config_id, batch in series_to_delete_by_config.items():
        client = sonarr_clients.get(config_id)
        if not client:
            continue
        LOG.info(f"Deleting {len(batch)} series via Sonarr config {config_id}")
        try:
            for series_info in batch:
                await client.delete_series(series_info["sonarr_id"], delete_files=True)
            series_to_delete.extend(batch)
        except Exception as e:
            LOG.error(
                f"Error deleting series via Sonarr config {config_id}: {e}",
                exc_info=True,
            )

    if series_to_delete:
        try:
            async with async_db() as db:
                for series_info in series_to_delete:
                    # update series record
                    result = await db.execute(
                        select(Series).where(Series.id == series_info["series_id"])
                    )
                    series = result.scalar_one_or_none()
                    if series:
                        series.removed_at = datetime.now(UTC)

                    # delete cleanup candidate
                    result = await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.id == series_info["candidate_id"]
                        )
                    )
                    candidate = result.scalar_one_or_none()
                    if candidate:
                        await db.delete(candidate)

                    # reset seerr request if available
                    series_tmdb_id = series_info.get("tmdb_id")
                    if service_manager.seerr and series and series_tmdb_id:
                        try:
                            await _reset_seerr_request(series_tmdb_id, MediaType.SERIES)
                        except Exception as e:
                            LOG.warning(
                                f"Failed to reset Seerr request for {series_info['title']}: {e}"
                            )

                    db.add(
                        ReclaimHistory(
                            approved_by=approved_by,
                            media_type=MediaType.SERIES,
                            tmdb_id=series_info.get("tmdb_id"),
                            name=series_info.get("title"),
                            size=series.size if series else None,
                        )
                    )

                await db.commit()

            deleted_count = len(series_to_delete)
            LOG.info(f"Successfully deleted {deleted_count} series via Sonarr")
        except Exception as e:
            LOG.error(f"Error finalizing series deletion state: {e}", exc_info=True)

    # fallback to media server deletion (Jellyfin/Emby/Plex) for candidates not in Sonarr
    if not series_to_delete or (len(candidates) > len(series_to_delete)):
        deleted_count += await _delete_series_via_media_server(
            candidates, series_to_delete, approved_by
        )

    return deleted_count


async def _delete_season_candidates(
    restrict_to_ids: frozenset[int] | None = None,
    approved_by: str = "system",
) -> int:
    """Delete season-level candidates.  Tries Sonarr first; falls back to media server.

    Returns count of seasons successfully deleted.
    """
    deleted_count = 0

    async with async_db() as db:
        query = (
            select(ReclaimCandidate)
            .join(Season, ReclaimCandidate.season_id == Season.id)
            .join(Series, ReclaimCandidate.series_id == Series.id)
            .where(ReclaimCandidate.media_type == MediaType.SERIES)
            .where(ReclaimCandidate.season_id.isnot(None))
            .where(Series.removed_at.is_(None))
        )
        if restrict_to_ids:
            query = query.where(ReclaimCandidate.id.in_(restrict_to_ids))
        candidates = (await db.execute(query)).scalars().all()

    if not candidates:
        LOG.debug("No season candidates to delete")
        return 0

    LOG.info(f"Found {len(candidates)} season candidates to evaluate for deletion")

    # bulk-load seasons and series needed for deletion
    async with async_db() as db:
        season_ids = [c.season_id for c in candidates if c.season_id]
        series_ids = list({c.series_id for c in candidates if c.series_id})

        seasons_result = await db.execute(
            select(Season).where(Season.id.in_(season_ids))
        )
        seasons: dict[int, Season] = {s.id: s for s in seasons_result.scalars().all()}

        series_result = await db.execute(
            select(Series)
            .where(Series.id.in_(series_ids))
            .options(selectinload(Series.service_refs))
        )
        series_map: dict[int, Series] = {s.id: s for s in series_result.scalars().all()}

    for candidate in candidates:
        if not candidate.season_id or not candidate.series_id:
            continue
        season = seasons.get(candidate.season_id)
        series_obj = series_map.get(candidate.series_id)
        if not season or not series_obj:
            continue

        season_number = season.season_number
        deleted_via_sonarr = False

        # try Sonarr first
        sonarr_ref_id: int | None = None
        sonarr_ref_config_id: int | None = None
        async with async_db() as db:
            ref = (
                (
                    await db.execute(
                        select(SeriesArrRef)
                        .where(SeriesArrRef.series_id == series_obj.id)
                        .order_by(SeriesArrRef.id.asc())
                    )
                )
                .scalars()
                .first()
            )
            if ref:
                sonarr_ref_id = ref.arr_series_id
                sonarr_ref_config_id = ref.service_config_id

        sonarr_client = None
        if sonarr_ref_config_id is not None:
            sonarr_client = service_manager.get_sonarr(sonarr_ref_config_id)
        if sonarr_client is None:
            sonarr_client = service_manager.sonarr

        if sonarr_client and sonarr_ref_id:
            try:
                await sonarr_client.delete_season_files(sonarr_ref_id, season_number)
                deleted_via_sonarr = True
                LOG.info(
                    f"Deleted '{series_obj.title}' S{season_number:02d} "
                    f"via Sonarr (sonarr_id={sonarr_ref_id})"
                )
            except Exception as e:
                LOG.warning(
                    f"Sonarr deletion failed for '{series_obj.title}' "
                    f"S{season_number:02d}: {e} - trying media server fallback"
                )

        # fall back to media server if Sonarr failed or unavailable
        if not deleted_via_sonarr:
            media_service = service_manager.main_media_server
            if media_service is service_manager.jellyfin:
                season_service_id = season.jellyfin_season_id
            elif media_service is service_manager.emby:
                season_service_id = season.emby_season_id
            else:
                season_service_id = (
                    season.plex_season_rating_key if media_service else None
                )
            if media_service and season_service_id:
                try:
                    await media_service.delete_item(season_service_id)
                    LOG.info(
                        f"Deleted '{series_obj.title}' S{season_number:02d} via media server"
                    )
                except Exception as e:
                    LOG.error(
                        f"Media server deletion failed for '{series_obj.title}' "
                        f"S{season_number:02d}: {e} - skipping"
                    )
                    continue
            else:
                LOG.warning(
                    f"No deletion method available for '{series_obj.title}' "
                    f"S{season_number:02d} - skipping"
                )
                continue

        # remove candidate and update series size in DB
        async with async_db() as db:
            result = await db.execute(
                select(ReclaimCandidate).where(ReclaimCandidate.id == candidate.id)
            )
            cand = result.scalar_one_or_none()
            if cand:
                await db.delete(cand)

            # reduce series stored size by the season's size
            if season.size:
                result = await db.execute(
                    select(Series).where(Series.id == candidate.series_id)
                )
                series_db = result.scalar_one_or_none()
                if series_db and series_db.size:
                    series_db.size = max(0, series_db.size - season.size)

            # delete the season row so it doesn't show stale data
            result = await db.execute(
                select(Season).where(Season.id == candidate.season_id)
            )
            season_db = result.scalar_one_or_none()
            if season_db:
                await db.execute(
                    delete(ReclaimCandidate).where(
                        ReclaimCandidate.season_id == season_db.id
                    )
                )
                await db.execute(
                    delete(ProtectionRequest).where(
                        ProtectionRequest.season_id == season_db.id
                    )
                )
                await db.execute(
                    delete(ProtectedMedia).where(
                        ProtectedMedia.season_id == season_db.id
                    )
                )
                await db.delete(season_db)

            db.add(
                ReclaimHistory(
                    approved_by=approved_by,
                    media_type=MediaType.SERIES,
                    tmdb_id=series_obj.tmdb_id,
                    name=f"{series_obj.title} S{season_number:02d}",
                    size=season.size,
                )
            )
            await db.commit()

        deleted_count += 1

    return deleted_count


async def _delete_movies_via_media_server(
    candidates, already_deleted: list[dict], approved_by: str = "system"
) -> int:
    """Deletes movies via the main media server as fallback when not in Radarr.

    Uses whichever Plex/Jellyfin/Emby server is designated as main.  If no main is
    set, falls back to whichever of Jellyfin/Emby/Plex is initialized (Jellyfin
    first).

    Returns count of movies deleted.
    """
    already_deleted_ids = {m["movie_id"] for m in already_deleted}
    remaining_candidates = [
        c for c in candidates if c.movie_id and c.movie_id not in already_deleted_ids
    ]

    if not remaining_candidates:
        return 0

    LOG.info(
        f"Attempting to delete {len(remaining_candidates)} movies via media server"
    )

    main_service = service_manager.main_media_server
    if not main_service:
        LOG.warning("No main media server available for movie deletion fallback")
        return 0

    if main_service is service_manager.jellyfin:
        main_service_type = Service.JELLYFIN
    elif main_service is service_manager.emby:
        main_service_type = Service.EMBY
    else:
        main_service_type = Service.PLEX

    # load movies with their versions
    async with async_db() as db:
        movie_ids = [c.movie_id for c in remaining_candidates]
        result = await db.execute(
            select(Movie)
            .where(Movie.id.in_(movie_ids))
            .options(selectinload(Movie.versions))
        )
        movies = {m.id: m for m in result.scalars().all()}

    deleted_count = 0

    for candidate in remaining_candidates:
        movie = movies.get(candidate.movie_id)
        if not movie or not movie.tmdb_id:
            continue

        service_versions = [v for v in movie.versions if v.service == main_service_type]
        if not service_versions:
            LOG.debug(
                f"Movie '{movie.title}' has no versions for {main_service_type} - skipping"
            )
            continue

        try:
            # delete each unique item (same movie may appear in multiple libraries)
            deleted_item_ids: set[str] = set()
            for ver in service_versions:
                if ver.service_item_id not in deleted_item_ids:
                    await main_service.delete_item(ver.service_item_id)
                    deleted_item_ids.add(ver.service_item_id)

            # mark as removed and delete candidate
            async with async_db() as db:
                result = await db.execute(
                    select(Movie).where(Movie.id == candidate.movie_id)
                )
                movie_obj = result.scalar_one_or_none()
                if movie_obj:
                    movie_obj.removed_at = datetime.now(UTC)

                result = await db.execute(
                    select(ReclaimCandidate).where(ReclaimCandidate.id == candidate.id)
                )
                cand = result.scalar_one_or_none()
                if cand:
                    await db.delete(cand)

                if service_manager.seerr and movie.tmdb_id:
                    try:
                        await _reset_seerr_request(movie.tmdb_id, MediaType.MOVIE)
                    except Exception as e:
                        LOG.warning(
                            f"Failed to reset Seerr request for '{movie.title}': {e}"
                        )

                db.add(
                    ReclaimHistory(
                        approved_by=approved_by,
                        media_type=MediaType.MOVIE,
                        tmdb_id=movie.tmdb_id,
                        name=movie.title,
                        size=movie.size,
                    )
                )

                await db.commit()

            deleted_count += 1
            LOG.info(f"Deleted movie '{movie.title}' via {main_service_type}")

        except Exception as e:
            LOG.error(
                f"Failed to delete movie '{movie.title}' via {main_service_type}: {e}"
            )

    return deleted_count


async def _delete_series_via_media_server(
    candidates, already_deleted: list[dict], approved_by: str = "system"
) -> int:
    """Deletes series via Jellyfin or Plex as fallback when not in Sonarr.

    Args:
        candidates: All series candidates
        already_deleted: List of series already deleted via Sonarr

    Returns:
        Count of series deleted via media servers
    """
    already_deleted_ids = {s["series_id"] for s in already_deleted}
    remaining_candidates = [
        c for c in candidates if c.series_id and c.series_id not in already_deleted_ids
    ]

    if not remaining_candidates:
        return 0

    LOG.info(
        f"Attempting to delete {len(remaining_candidates)} series via media server (not in Sonarr)"
    )
    deleted_count = 0

    main_service = service_manager.main_media_server
    if not main_service:
        LOG.warning("No main media server available for series deletion fallback")
        return 0

    if main_service is service_manager.jellyfin:
        main_service_type = Service.JELLYFIN
    elif main_service is service_manager.emby:
        main_service_type = Service.EMBY
    else:
        main_service_type = Service.PLEX

    # get series from database to access service refs
    async with async_db() as db:
        series_ids = [c.series_id for c in remaining_candidates]
        result = await db.execute(
            select(Series)
            .where(Series.id.in_(series_ids))
            .options(selectinload(Series.service_refs))
        )
        series = {s.id: s for s in result.scalars().all()}

    for candidate in remaining_candidates:
        series_obj = series.get(candidate.series_id)
        if not series_obj or not series_obj.tmdb_id:
            continue

        ref = next(
            (r for r in series_obj.service_refs if r.service == main_service_type),
            None,
        )
        if not ref:
            LOG.debug(
                f"Series '{series_obj.title}' not found in {main_service_type} (no service ref)"
            )
            continue

        try:
            await main_service.delete_item(ref.service_id)

            async with async_db() as db:
                result = await db.execute(
                    select(Series).where(Series.id == candidate.series_id)
                )
                series_db = result.scalar_one_or_none()
                if series_db:
                    series_db.removed_at = datetime.now(UTC)

                result = await db.execute(
                    select(ReclaimCandidate).where(ReclaimCandidate.id == candidate.id)
                )
                cand = result.scalar_one_or_none()
                if cand:
                    await db.delete(cand)

                if service_manager.seerr and series_obj.tmdb_id:
                    try:
                        await _reset_seerr_request(series_obj.tmdb_id, MediaType.SERIES)
                    except Exception as e:
                        LOG.warning(
                            f"Failed to reset Seerr request for {series_obj.title}: {e}"
                        )

                db.add(
                    ReclaimHistory(
                        approved_by=approved_by,
                        media_type=MediaType.SERIES,
                        tmdb_id=series_obj.tmdb_id,
                        name=series_obj.title,
                        size=series_db.size if series_db else None,
                    )
                )

                await db.commit()

            deleted_count += 1
            LOG.info(f"Deleted series '{series_obj.title}' via {main_service_type}")

        except Exception as e:
            LOG.error(
                f"Failed to delete series '{series_obj.title}' via {main_service_type}: {e}"
            )

    return deleted_count


async def _reset_seerr_request(tmdb_id: int, media_type: MediaType) -> None:
    """Remove requests and media items in Seerr (Overseerr/Jellyseerr) after media deletion.

    Args:
        tmdb_id: TMDB ID of the media
        media_type: Movie or Series
    """
    if not service_manager.seerr:
        return

    try:
        if media_type is MediaType.MOVIE:
            # delete all requests first
            await service_manager.seerr.delete_movie_requests(tmdb_id)
            LOG.debug(f"Deleted Seerr movie requests for TMDB ID {tmdb_id}")
            # then delete the media item itself
            await service_manager.seerr.delete_movie_media(tmdb_id)
            LOG.debug(f"Deleted Seerr movie media for TMDB ID {tmdb_id}")
        else:
            # delete all requests first
            await service_manager.seerr.delete_tv_requests(tmdb_id)
            LOG.debug(f"Deleted Seerr TV requests for TMDB ID {tmdb_id}")
            # then delete the media item itself
            await service_manager.seerr.delete_tv_media(tmdb_id)
            LOG.debug(f"Deleted Seerr TV media for TMDB ID {tmdb_id}")
    except PermissionError as e:
        LOG.warning(f"Seerr permission error for TMDB {tmdb_id}: {e}")
    except Exception as e:
        LOG.warning(f"Failed to delete Seerr data for TMDB {tmdb_id}: {e}")


async def delete_specific_candidates(
    candidate_ids: list[int], approved_by: str = "system"
) -> tuple[int, int]:
    """Deletes specific reclaim candidates by their IDs.

    Uses the same deletion priority as delete_cleanup_candidates:
    Radarr/Sonarr first, then Jellyfin/Emby/Plex (main server first) fallback.

    Returns (deleted_count, failed_count).
    """
    if not candidate_ids:
        return 0, 0

    restrict = frozenset(candidate_ids)

    # look up which types we're dealing with so we only invoke relevant paths
    async with async_db() as db:
        result = await db.execute(
            select(ReclaimCandidate.id, ReclaimCandidate.media_type).where(
                ReclaimCandidate.id.in_(restrict)
            )
        )
        rows = result.all()

    found_ids = {r[0] for r in rows}
    types = {r[1] for r in rows}

    LOG.info(
        f"Manual deletion of {len(found_ids)} candidate(s) requested "
        f"(movies={MediaType.MOVIE in types}, series={MediaType.SERIES in types})"
    )

    deleted = 0
    if MediaType.MOVIE in types and (
        service_manager.radarr or service_manager.main_media_server
    ):
        deleted += await _delete_movie_candidates(
            restrict_to_ids=restrict, approved_by=approved_by
        )

    if MediaType.SERIES in types and (
        service_manager.sonarr or service_manager.main_media_server
    ):
        deleted += await _delete_series_candidates(
            restrict_to_ids=restrict, approved_by=approved_by
        )
        deleted += await _delete_season_candidates(
            restrict_to_ids=restrict, approved_by=approved_by
        )

    failed = max(0, len(found_ids) - deleted)
    LOG.info(f"Manual deletion complete: {deleted} deleted, {failed} failed")
    return deleted, failed
