import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.logger import LOG
from backend.core.rule_engine import (
    TARGET_MOVIE_VERSION,
    TARGET_SEASON,
    TARGET_SERIES,
    collect_rule_conditions,
    evaluate_advanced_rule,
    normalize_rule_target,
)
from backend.core.service_manager import service_manager
from backend.core.task_tracking import track_task_execution
from backend.core.utils.filesystem import (
    find_season_folder,
    move_directory,
    move_media,
    move_season_files,
    resolve_path,
    sibling_cleanup,
)
from backend.database import async_db
from backend.database.models import (
    DeleteRequest,
    GeneralSettings,
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
from backend.models.post_action_webhooks import PostActionWebhookEvent
from backend.services.notifications import notify_all_users
from backend.services.post_action_webhooks import (
    dispatch_configured_post_action_webhooks,
)

__all__ = [
    "scan_cleanup_candidates",
    "tag_cleanup_candidates",
    # "delete_cleanup_candidates",
    "delete_specific_candidates",
    "move_specific_candidates",
    "collect_rule_preview_matches",
]


async def collect_rule_preview_matches(
    db: AsyncSession,
    rules: list[ReclaimRule],
) -> list[MatchedCandidateRecord]:
    """Evaluate rules without mutating persisted candidates."""
    await _refresh_arr_tags_for_rules(list(rules))

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


def _collect_arr_tag_labels(rules: list[ReclaimRule]) -> set[str]:
    """Return the distinct lowercase tag labels referenced in arr.tags conditions across rules."""
    labels: set[str] = set()
    for rule in rules:
        for condition in collect_rule_conditions(rule.definition, field="arr.tags"):
            value = condition.get("value")
            values = value if isinstance(value, list) else [value]
            for v in values:
                if v is not None and str(v).strip():
                    labels.add(str(v).strip().lower())
    return labels


async def _refresh_arr_tags_for_rules(rules: list[ReclaimRule]) -> None:
    """Refresh arr_tags on Movie/Series rows for tag labels referenced by active rules.

    Steps:
    1. Collect the distinct tag labels used in arr.tags conditions across all rules.
    2. Per arr client: single GET /tag/detail call returning all tags with their item IDs.
    3. Map arr item IDs -> DB IDs via MovieArrRef / SeriesArrRef.
    4. Strip rule relevant labels from all tracked rows, then re add only where confirmed present.
    This keeps negative operators (not_contains_any) correct without fetching all movies/series.
    """
    movie_rules = [
        r for r in rules if normalize_rule_target(r) in {TARGET_MOVIE_VERSION}
    ]
    series_rules = [
        r for r in rules if normalize_rule_target(r) in {TARGET_SERIES, TARGET_SEASON}
    ]

    movie_tag_labels = _collect_arr_tag_labels(movie_rules)
    series_tag_labels = _collect_arr_tag_labels(series_rules)

    if not movie_tag_labels and not series_tag_labels:
        return

    #### radarr: refresh movie arr_tags for rule relevant labels ####
    if movie_tag_labels:
        radarr_clients = service_manager.radarr_clients()
        if not radarr_clients and service_manager.radarr:
            radarr_clients = {0: service_manager.radarr}

        if radarr_clients:
            # movie_id (DB) -> set of labels to add
            movie_label_additions: dict[int, set[str]] = {}

            async with async_db() as db:
                rows = (
                    await db.execute(
                        select(
                            MovieArrRef.arr_movie_id,
                            MovieArrRef.movie_id,
                            MovieArrRef.service_config_id,
                        )
                    )
                ).all()
            # arr_movie_id -> db movie_id, grouped by config_id
            arr_to_db_by_config: dict[int, dict[int, int]] = {}
            for arr_movie_id, db_movie_id, config_id in rows:
                arr_to_db_by_config.setdefault(config_id, {})[arr_movie_id] = (
                    db_movie_id
                )

            for config_id, client in radarr_clients.items():
                try:
                    # single call: label -> [arr_movie_id, ...] for ALL tags
                    tag_details = await client.get_all_tag_details()
                except Exception as e:
                    LOG.warning(
                        f"Failed to fetch Radarr tag details for config {config_id}: {e}"
                    )
                    continue

                arr_to_db = arr_to_db_by_config.get(config_id, {})
                for label in movie_tag_labels:
                    for arr_id in tag_details.get(label, []):
                        db_id = arr_to_db.get(arr_id)
                        if db_id is not None:
                            movie_label_additions.setdefault(db_id, set()).add(label)

            # apply: strip then re-add relevant labels on all tracked movie rows
            all_db_movie_ids = {
                db_id
                for mapping in arr_to_db_by_config.values()
                for db_id in mapping.values()
            }
            if all_db_movie_ids:
                async with async_db() as db:
                    result = await db.execute(
                        select(Movie).where(Movie.id.in_(all_db_movie_ids))
                    )
                    for movie in result.scalars().all():
                        current = set(movie.arr_tags or [])
                        current -= movie_tag_labels  # strip stale rule-relevant labels
                        current |= movie_label_additions.get(
                            movie.id, set()
                        )  # re-add current ones
                        movie.arr_tags = sorted(current)
                    await db.commit()
            LOG.debug(
                f"Refreshed arr_tags for {len(all_db_movie_ids)} movies (labels: {movie_tag_labels})"
            )

    #### sonarr: refresh series arr_tags for rule relevant labels ####
    if series_tag_labels:
        sonarr_clients = service_manager.sonarr_clients()
        if not sonarr_clients and service_manager.sonarr:
            sonarr_clients = {0: service_manager.sonarr}

        if sonarr_clients:
            series_label_additions: dict[int, set[str]] = {}

            async with async_db() as db:
                rows = (
                    await db.execute(
                        select(
                            SeriesArrRef.arr_series_id,
                            SeriesArrRef.series_id,
                            SeriesArrRef.service_config_id,
                        )
                    )
                ).all()
            sonarr_arr_to_db_by_config: dict[int, dict[int, int]] = {}
            for arr_series_id, db_series_id, config_id in rows:
                sonarr_arr_to_db_by_config.setdefault(config_id, {})[arr_series_id] = (
                    db_series_id
                )

            for config_id, client in sonarr_clients.items():
                try:
                    # single call: label -> [arr_series_id, ...] for ALL tags
                    tag_details = await client.get_all_tag_details()
                except Exception as e:
                    LOG.warning(
                        f"Failed to fetch Sonarr tag details for config {config_id}: {e}"
                    )
                    continue

                arr_to_db = sonarr_arr_to_db_by_config.get(config_id, {})
                for label in series_tag_labels:
                    for arr_id in tag_details.get(label, []):
                        db_id = arr_to_db.get(arr_id)
                        if db_id is not None:
                            series_label_additions.setdefault(db_id, set()).add(label)

            all_db_series_ids = {
                db_id
                for mapping in sonarr_arr_to_db_by_config.values()
                for db_id in mapping.values()
            }
            if all_db_series_ids:
                async with async_db() as db:
                    result = await db.execute(
                        select(Series).where(Series.id.in_(all_db_series_ids))
                    )
                    for series in result.scalars().all():
                        current = set(series.arr_tags or [])
                        current -= series_tag_labels
                        current |= series_label_additions.get(series.id, set())
                        series.arr_tags = sorted(current)
                    await db.commit()
            LOG.debug(
                f"Refreshed arr_tags for {len(all_db_series_ids)} series (labels: {series_tag_labels})"
            )


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

        # refresh arr_tags from Radarr/Sonarr for any labels referenced in active rules
        # (1 GET /tag + 1 GET /tag/detail/{id} per relevant label per client - no bulk fetch)
        await _refresh_arr_tags_for_rules(list(rules))

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
                estimated_space_bytes=item.size if item.size else None,
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
            existing.estimated_space_bytes = record.estimated_space_bytes
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
                    estimated_space_bytes=record.estimated_space_bytes,
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
                    estimated_space_bytes=candidate_size if candidate_size else None,
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
            existing.estimated_space_bytes = record.estimated_space_bytes
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
                    estimated_space_bytes=record.estimated_space_bytes,
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
                    estimated_space_bytes=season.size if season.size else None,
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
            existing.estimated_space_bytes = record.estimated_space_bytes
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
                    estimated_space_bytes=record.estimated_space_bytes,
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


def _get_arr_action(candidate: "ReclaimCandidate", rules: dict) -> str:
    """Return 'unmonitor' if any matched rule requests it, otherwise 'delete'."""
    for rule_id in candidate.matched_rule_ids or []:
        rule = rules.get(rule_id)
        if rule and _rule_action(rule).get("arr_action") == "unmonitor":
            return "unmonitor"
    return "delete"


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


async def _load_path_mappings() -> list[dict]:
    """Load path mappings from GeneralSettings (returns empty list if unset)."""
    async with async_db() as db:
        result = await db.execute(select(GeneralSettings))
        settings = result.scalars().first()
        if settings and settings.path_mappings:
            return settings.path_mappings
    return []


def _service_value(service: Service | str | None) -> str | None:
    if service is None:
        return None
    return service.value if isinstance(service, Service) else str(service)


def _path_text(path: Path | None) -> str | None:
    return str(path) if path is not None else None


async def _resolve_event_local_path(
    path: str | None,
    service_type: Service | str | None = None,
    service_config_id: int | None = None,
) -> str | None:
    if not path:
        return None
    mappings = await _load_path_mappings()
    return _path_text(
        resolve_path(
            path,
            mappings,
            service_type=_service_value(service_type),
            service_config_id=service_config_id,
        )
    )


async def _dispatch_reclaim_event(
    *,
    action: str,
    media_type: MediaType,
    title: str | None = None,
    tmdb_id: int | None = None,
    candidate_id: int | None = None,
    path: str | None = None,
    destination_path: str | None = None,
    service_type: Service | str | None = None,
    service_config_id: int | None = None,
    movie_version_id: int | None = None,
    season_id: int | None = None,
    season_number: int | None = None,
    local_path: str | None = None,
) -> None:
    try:
        await dispatch_configured_post_action_webhooks(
            PostActionWebhookEvent(
                action=action,
                media_type=media_type,
                title=title,
                tmdb_id=tmdb_id,
                candidate_id=candidate_id,
                path=path,
                local_path=local_path
                if local_path is not None
                else await _resolve_event_local_path(
                    path, service_type=service_type, service_config_id=service_config_id
                ),
                destination_path=destination_path,
                service_type=service_type,
                service_config_id=service_config_id,
                movie_version_id=movie_version_id,
                season_id=season_id,
                season_number=season_number,
            )
        )
    except Exception as e:
        LOG.warning(f"Post-action webhook dispatch failed: {e}")


async def _delete_movie_version_candidates(
    version_candidates: list[ReclaimCandidate],
    approved_by: str = "system",
    media_server_fallback_enabled: bool = True,
) -> int:
    """Delete movie-version candidates using service-aware targeted deletion.

    These are partial-version deletions (e.g. delete 1080p but keep 4K).  Radarr
    cannot delete individual quality versions, so the media server is the only
    authority here.  When *media_server_fallback_enabled* is False the deletion
    is skipped and a warning is logged instead.
    """
    if not version_candidates:
        return 0

    if not media_server_fallback_enabled:
        for candidate in version_candidates:
            async with async_db() as db:
                result = await db.execute(
                    select(MovieVersion)
                    .where(MovieVersion.id == candidate.movie_version_id)
                    .options(selectinload(MovieVersion.movie))
                )
                version = result.scalars().first()
                title = version.movie.title if version and version.movie else "unknown"
            LOG.warning(
                f"Media server fallback disabled — skipping partial-version deletion "
                f"for '{title}'. Enable 'Allow Media Server Fallback Deletion' in "
                f"General Settings to delete individual quality versions."
            )
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

            # attempt filesystem sibling cleanup (subtitle/nfo files + empty dirs)
            path_mappings = await _load_path_mappings()
            local_path = resolve_path(
                version.path, path_mappings, service_type=main_service_type.value
            )
            if local_path:
                try:
                    sibling_cleanup(local_path)
                except Exception as fs_err:
                    LOG.warning(
                        f"sibling_cleanup failed for '{version.path}': {fs_err}"
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
            await _dispatch_reclaim_event(
                action="deleted",
                media_type=MediaType.MOVIE,
                title=movie.title,
                tmdb_id=movie.tmdb_id,
                candidate_id=candidate.id,
                path=version.path,
                local_path=_path_text(local_path),
                service_type=main_service_type,
                movie_version_id=version.id,
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

    # load fallback toggle from settings
    async with async_db() as db:
        settings_row = (await db.execute(select(GeneralSettings))).scalars().first()
        media_server_fallback_enabled = (
            settings_row.media_server_fallback_enabled if settings_row else True
        )

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

    # load rules for arr_action resolution
    all_rule_ids = {rid for c in candidates for rid in (c.matched_rule_ids or [])}
    async with async_db() as db:
        rules_by_id: dict[int, ReclaimRule] = {}
        if all_rule_ids:
            rules_by_id = {
                r.id: r
                for r in (
                    await db.execute(
                        select(ReclaimRule).where(ReclaimRule.id.in_(all_rule_ids))
                    )
                )
                .scalars()
                .all()
            }

    # determine per-movie action ("delete" or "unmonitor")
    movie_arr_action: dict[int, str] = {}
    for cand in candidates:
        if cand.movie_id:
            action = _get_arr_action(cand, rules_by_id)
            if action == "unmonitor" or cand.movie_id not in movie_arr_action:
                movie_arr_action[cand.movie_id] = action

    whole_movie_candidates = [c for c in candidates if c.movie_version_id is None]
    version_candidates = [c for c in candidates if c.movie_version_id is not None]

    radarr_clients = service_manager.radarr_clients()
    if not radarr_clients and service_manager.radarr:
        radarr_clients = {0: service_manager.radarr}

    # Load movie data + all arr refs (with stored arr_movie_path for path-first routing).
    # Path-first routing: match the media-server file path (MovieVersion.path) against
    # the Radarr folder path (MovieArrRef.arr_movie_path) to find the correct Radarr
    # instance for each version.  Falls back to service_config_id membership when
    # arr_movie_path is not yet populated (e.g. before first sync after upgrade).
    all_movie_ids = {c.movie_id for c in candidates if c.movie_id}
    async with async_db() as db:
        rows = (
            await db.execute(
                select(
                    Movie.id,
                    Movie.title,
                    Movie.tmdb_id,
                    MovieArrRef.service_config_id,
                    MovieArrRef.arr_movie_id,
                    MovieArrRef.arr_movie_path,
                )
                .outerjoin(MovieArrRef, MovieArrRef.movie_id == Movie.id)
                .where(Movie.id.in_(all_movie_ids))
                .where(Movie.removed_at.is_(None))
            )
        ).all()
        movie_data: dict[int, dict] = {}
        for movie_id, title, tmdb_id, config_id, arr_movie_id, arr_movie_path in rows:
            info = movie_data.setdefault(
                movie_id,
                {"title": title, "tmdb_id": tmdb_id, "refs": []},
            )
            if config_id is not None and arr_movie_id is not None:
                info["refs"].append((config_id, arr_movie_id, arr_movie_path))

        # load version paths needed for path-based routing
        version_ids = [c.movie_version_id for c in version_candidates if c.movie_version_id]
        version_path_by_id: dict[int, str | None] = {}
        if version_ids:
            ver_rows = (
                await db.execute(
                    select(MovieVersion.id, MovieVersion.path).where(
                        MovieVersion.id.in_(version_ids)
                    )
                )
            ).all()
            version_path_by_id = {ver_id: ver_path for ver_id, ver_path in ver_rows}

    def _match_version_to_arr(
        version_path: str | None,
        refs: list[tuple],
    ) -> set[tuple[int, int]]:
        """Return (config_id, arr_movie_id) pairs whose folder path contains this version.

        Path prefix matching is tried first.  If no arr_movie_path is stored yet
        (e.g. before first sync after upgrade), falls back to routing to all arr
        instances that have this movie.
        """
        if not refs:
            return set()
        matched: set[tuple[int, int]] = set()
        if version_path:
            norm_ver = version_path.rstrip("/")
            for config_id, arr_movie_id, arr_movie_path in refs:
                if arr_movie_path and config_id in radarr_clients:
                    if norm_ver.startswith(arr_movie_path.rstrip("/") + "/"):
                        matched.add((config_id, arr_movie_id))
        if not matched:
            # no path match — use all known arr configs for this movie
            for config_id, arr_movie_id, _ in refs:
                if config_id in radarr_clients:
                    matched.add((config_id, arr_movie_id))
        return matched

    # Route each candidate to the correct arr instance(s).
    # movie_id -> set of (config_id, arr_movie_id)
    movie_arr_routing: dict[int, set[tuple[int, int]]] = {}
    # movie_id -> all candidate IDs that belong to it (for DB cleanup after deletion)
    all_cand_ids_by_movie: dict[int, list[int]] = {}
    # version candidates that could not be routed to any arr instance
    unmatched_version_candidates: list[ReclaimCandidate] = []

    if radarr_clients:
        for cand in version_candidates:
            if not cand.movie_id or not cand.movie_version_id:
                unmatched_version_candidates.append(cand)
                continue
            movie_info = movie_data.get(cand.movie_id)
            if not movie_info or not movie_info["refs"]:
                unmatched_version_candidates.append(cand)
                continue
            ver_path = version_path_by_id.get(cand.movie_version_id)
            matched = _match_version_to_arr(ver_path, movie_info["refs"])
            if matched:
                movie_arr_routing.setdefault(cand.movie_id, set()).update(matched)
                all_cand_ids_by_movie.setdefault(cand.movie_id, []).append(cand.id)
            else:
                LOG.warning(
                    f"No Radarr instance found for '{movie_info['title']}' "
                    f"(path: {ver_path!r}) — routing to media server fallback"
                )
                unmatched_version_candidates.append(cand)

        for cand in whole_movie_candidates:
            if not cand.movie_id:
                continue
            movie_info = movie_data.get(cand.movie_id)
            if not movie_info:
                continue
            # whole-movie candidates go to ALL arr instances that know this movie
            for config_id, arr_movie_id, _ in movie_info["refs"]:
                if config_id in radarr_clients:
                    movie_arr_routing.setdefault(cand.movie_id, set()).add(
                        (config_id, arr_movie_id)
                    )
            all_cand_ids_by_movie.setdefault(cand.movie_id, []).append(cand.id)
    else:
        unmatched_version_candidates = list(version_candidates)

    # Handle version candidates not routable to any arr instance via media server
    if unmatched_version_candidates:
        deleted_count += await _delete_movie_version_candidates(
            unmatched_version_candidates, approved_by, media_server_fallback_enabled
        )

    if not movie_arr_routing:
        # No arr deletions — route remaining whole-movie candidates to media server
        if whole_movie_candidates:
            if media_server_fallback_enabled:
                deleted_count += await _delete_movies_via_media_server(
                    whole_movie_candidates, [], approved_by
                )
            else:
                for cand in whole_movie_candidates:
                    title = movie_data.get(cand.movie_id or 0, {}).get("title", "unknown")
                    LOG.warning(
                        f"Media server fallback disabled — skipping deletion for "
                        f"'{title}' (not found in any Radarr instance)"
                    )
        return deleted_count

    # build per-config deletion / unmonitor batches
    movies_to_delete_by_config: dict[int, list[dict]] = {}
    movies_to_unmonitor_by_config: dict[int, list[dict]] = {}
    for movie_id, config_arr_set in movie_arr_routing.items():
        movie_info = movie_data.get(movie_id, {})
        all_cand_ids = all_cand_ids_by_movie.get(movie_id, [])
        target = (
            movies_to_unmonitor_by_config
            if movie_arr_action.get(movie_id) == "unmonitor"
            else movies_to_delete_by_config
        )
        for config_id, arr_movie_id in config_arr_set:
            target.setdefault(config_id, []).append(
                {
                    "candidate_id": all_cand_ids[0] if all_cand_ids else None,
                    "all_candidate_ids": all_cand_ids,
                    "movie_id": movie_id,
                    "radarr_id": arr_movie_id,
                    "config_id": config_id,
                    "title": movie_info.get("title", ""),
                    "method": "radarr",
                    "tmdb_id": movie_info.get("tmdb_id"),
                }
            )

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
        movie_events: list[dict[str, Any]] = []
        try:
            async with async_db() as db:
                # use seen_movie_ids to avoid double writes when a movie appears in
                # multiple arr configs (both Radarr_1080p and Radarr4K)
                seen_movie_ids: set[int] = set()
                for movie_info in movies_to_delete:
                    movie_id = movie_info["movie_id"]
                    already_processed = movie_id in seen_movie_ids
                    seen_movie_ids.add(movie_id)

                    result = await db.execute(
                        select(Movie)
                        .where(Movie.id == movie_id)
                        .options(selectinload(Movie.versions))
                    )
                    movie = result.scalar_one_or_none()

                    if not already_processed:
                        if movie:
                            movie.removed_at = datetime.now(UTC)
                            event_versions = [v for v in movie.versions if v.path] or [None]
                            for version in event_versions:
                                movie_events.append(
                                    {
                                        "title": movie.title,
                                        "tmdb_id": movie.tmdb_id,
                                        "candidate_id": movie_info["candidate_id"],
                                        "path": version.path if version else None,
                                        "service_type": version.service
                                        if version
                                        else Service.RADARR,
                                        "movie_version_id": version.id if version else None,
                                    }
                                )

                        for cand_id in movie_info["all_candidate_ids"]:
                            result = await db.execute(
                                select(ReclaimCandidate).where(
                                    ReclaimCandidate.id == cand_id
                                )
                            )
                            cand = result.scalar_one_or_none()
                            if cand:
                                await db.delete(cand)

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

            unique_deleted = len({m["movie_id"] for m in movies_to_delete})
            deleted_count += unique_deleted
            LOG.info(f"Successfully deleted {unique_deleted} movies via Radarr")
            for event in movie_events:
                await _dispatch_reclaim_event(
                    action="deleted",
                    media_type=MediaType.MOVIE,
                    **event,
                )
        except Exception as e:
            LOG.error(f"Error finalizing movie deletion state: {e}", exc_info=True)

    # process unmonitor batches: mark unmonitored in Radarr, then delete files + media server
    movies_to_unmonitor: list[dict] = []
    for config_id, batch in movies_to_unmonitor_by_config.items():
        client = radarr_clients.get(config_id)
        if not client:
            continue
        LOG.info(f"Unmonitoring {len(batch)} movies via Radarr config {config_id}")
        radarr_ids = [m["radarr_id"] for m in batch]
        try:
            await client.unmonitor_movies(radarr_ids)
            movies_to_unmonitor.extend(batch)
        except Exception as e:
            LOG.error(
                f"Error unmonitoring movies via Radarr config {config_id}: {e}",
                exc_info=True,
            )

    if movies_to_unmonitor:
        unmonitor_events: list[dict[str, Any]] = []
        path_mappings = await _load_path_mappings()
        main_service = service_manager.main_media_server
        if main_service is None:
            unmonitor_service_type: Service | None = None
        elif main_service is service_manager.jellyfin:
            unmonitor_service_type = Service.JELLYFIN
        elif main_service is service_manager.emby:
            unmonitor_service_type = Service.EMBY
        else:
            unmonitor_service_type = Service.PLEX
        try:
            async with async_db() as db:
                seen_unmonitor_ids: set[int] = set()
                for movie_info in movies_to_unmonitor:
                    movie_id = movie_info["movie_id"]
                    already_processed = movie_id in seen_unmonitor_ids
                    seen_unmonitor_ids.add(movie_id)

                    result = await db.execute(
                        select(Movie)
                        .where(Movie.id == movie_id)
                        .options(selectinload(Movie.versions))
                    )
                    movie = result.scalar_one_or_none()

                    if not already_processed:
                        if movie:
                            # delete files from disk
                            for ver in movie.versions:
                                if not ver.path:
                                    continue
                                local_path = resolve_path(
                                    ver.path,
                                    path_mappings,
                                    service_type=unmonitor_service_type.value
                                    if unmonitor_service_type
                                    else None,
                                )
                                if local_path:
                                    try:
                                        sibling_cleanup(local_path)
                                    except Exception as fs_err:
                                        LOG.warning(
                                            f"sibling_cleanup failed for '{ver.path}': {fs_err}"
                                        )

                            # remove from media server
                            if main_service is not None and unmonitor_service_type is not None:
                                service_versions = [
                                    v
                                    for v in movie.versions
                                    if v.service == unmonitor_service_type
                                ]
                                deleted_item_ids: set[str] = set()
                                for ver in service_versions:
                                    if ver.service_item_id not in deleted_item_ids:
                                        try:
                                            await main_service.delete_item(
                                                ver.service_item_id
                                            )
                                            deleted_item_ids.add(ver.service_item_id)
                                        except Exception as ms_err:
                                            LOG.warning(
                                                f"Media server delete_item failed for "
                                                f"'{movie.title}': {ms_err}"
                                            )

                            movie.removed_at = datetime.now(UTC)
                            event_versions = (
                                [v for v in movie.versions if v.path] or [None]
                            )
                            for version in event_versions:
                                unmonitor_events.append(
                                    {
                                        "title": movie.title,
                                        "tmdb_id": movie.tmdb_id,
                                        "candidate_id": movie_info["candidate_id"],
                                        "path": version.path if version else None,
                                        "service_type": version.service
                                        if version
                                        else Service.RADARR,
                                        "movie_version_id": version.id
                                        if version
                                        else None,
                                    }
                                )

                        for cand_id in movie_info["all_candidate_ids"]:
                            result = await db.execute(
                                select(ReclaimCandidate).where(
                                    ReclaimCandidate.id == cand_id
                                )
                            )
                            cand = result.scalar_one_or_none()
                            if cand:
                                await db.delete(cand)

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
                                action="unmonitored",
                            )
                        )

                await db.commit()

            unique_unmonitored = len(seen_unmonitor_ids)
            deleted_count += unique_unmonitored
            LOG.info(
                f"Successfully unmonitored {unique_unmonitored} movies via Radarr "
                f"and cleaned up files"
            )
            for event in unmonitor_events:
                await _dispatch_reclaim_event(
                    action="unmonitored",
                    media_type=MediaType.MOVIE,
                    **event,
                )
        except Exception as e:
            LOG.error(f"Error finalizing movie unmonitor state: {e}", exc_info=True)

    # fallback to media server for whole-movie candidates not handled by any arr instance
    if whole_movie_candidates:
        if media_server_fallback_enabled:
            deleted_count += await _delete_movies_via_media_server(
                whole_movie_candidates, movies_to_delete + movies_to_unmonitor, approved_by
            )
        else:
            movies_handled_ids = {m["movie_id"] for m in movies_to_delete + movies_to_unmonitor}
            unhandled = [
                c for c in whole_movie_candidates if c.movie_id not in movies_handled_ids
            ]
            for cand in unhandled:
                title = movie_data.get(cand.movie_id or 0, {}).get("title", "unknown")
                LOG.warning(
                    f"Media server fallback disabled — skipping deletion for "
                    f"'{title}' (not handled by any Radarr instance)"
                )

    return deleted_count


async def _delete_series_candidates(
    restrict_to_ids: frozenset[int] | None = None,
    approved_by: str = "system",
) -> int:
    """Delete series candidates. Returns count of deleted series."""
    deleted_count = 0

    # load fallback toggle from settings
    async with async_db() as db:
        settings_row = (await db.execute(select(GeneralSettings))).scalars().first()
        media_server_fallback_enabled = (
            settings_row.media_server_fallback_enabled if settings_row else True
        )

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

    # load rules for arr_action resolution
    series_rule_ids = {rid for c in candidates for rid in (c.matched_rule_ids or [])}
    async with async_db() as db:
        series_rules_by_id: dict[int, ReclaimRule] = {}
        if series_rule_ids:
            series_rules_by_id = {
                r.id: r
                for r in (
                    await db.execute(
                        select(ReclaimRule).where(ReclaimRule.id.in_(series_rule_ids))
                    )
                )
                .scalars()
                .all()
            }

    # determine per-series action ("delete" or "unmonitor")
    series_arr_action: dict[int, str] = {}
    for cand in candidates:
        if cand.series_id:
            action = _get_arr_action(cand, series_rules_by_id)
            if action == "unmonitor" or cand.series_id not in series_arr_action:
                series_arr_action[cand.series_id] = action

    async with async_db() as db:
        # load series data + all arr refs (with stored arr_series_path for path routing)
        result = await db.execute(
            select(
                Series.id,
                Series.title,
                Series.tmdb_id,
                SeriesArrRef.service_config_id,
                SeriesArrRef.arr_series_id,
                SeriesArrRef.arr_series_path,
            )
            .join(ReclaimCandidate, Series.id == ReclaimCandidate.series_id)
            .outerjoin(SeriesArrRef, SeriesArrRef.series_id == Series.id)
            .where(ReclaimCandidate.media_type == MediaType.SERIES)
            .where(Series.removed_at.is_(None))
        )
        rows = result.all()
        series_data: dict[int, dict] = {}
        for series_id, title, tmdb_id, config_id, arr_series_id, arr_series_path in rows:
            info = series_data.setdefault(
                series_id,
                {"title": title, "tmdb_id": tmdb_id, "refs": []},
            )
            if config_id is not None and arr_series_id is not None:
                info["refs"].append((config_id, arr_series_id, arr_series_path))

    sonarr_clients = service_manager.sonarr_clients()
    if not sonarr_clients and service_manager.sonarr:
        sonarr_clients = {0: service_manager.sonarr}

    # Route each candidate to ALL matching Sonarr instances.
    # Series-level candidates don't have a per-version path, so we route to every
    # Sonarr instance that tracks this series (arr_series_path used when available
    # for disambiguation in multi-Sonarr setups; falls back to config_id membership).
    # series_id -> set of (config_id, arr_series_id)
    series_arr_routing: dict[int, set[tuple[int, int]]] = {}

    if sonarr_clients:
        for candidate in candidates:
            if candidate.series_id is None:
                continue
            series_info = series_data.get(candidate.series_id)
            if not series_info:
                continue
            for config_id, arr_series_id, _ in series_info["refs"]:
                if config_id in sonarr_clients:
                    series_arr_routing.setdefault(candidate.series_id, set()).add(
                        (config_id, arr_series_id)
                    )

    series_to_delete_by_config: dict[int, list[dict]] = {}
    series_to_unmonitor_by_config: dict[int, list[dict]] = {}
    for series_id, config_arr_set in series_arr_routing.items():
        series_info = series_data.get(series_id, {})
        candidate_id = next(
            (c.id for c in candidates if c.series_id == series_id), None
        )
        target = (
            series_to_unmonitor_by_config
            if series_arr_action.get(series_id) == "unmonitor"
            else series_to_delete_by_config
        )
        for config_id, arr_series_id in config_arr_set:
            target.setdefault(config_id, []).append(
                {
                    "candidate_id": candidate_id,
                    "series_id": series_id,
                    "sonarr_id": arr_series_id,
                    "title": series_info.get("title", ""),
                    "method": "sonarr",
                    "tmdb_id": series_info.get("tmdb_id"),
                }
            )

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
        series_events: list[dict[str, Any]] = []
        try:
            async with async_db() as db:
                # deduplicate: a series may appear in multiple Sonarr instances
                seen_series_ids: set[int] = set()
                for series_info in series_to_delete:
                    series_id = series_info["series_id"]
                    already_processed = series_id in seen_series_ids
                    seen_series_ids.add(series_id)

                    if already_processed:
                        continue

                    result = await db.execute(
                        select(Series)
                        .where(Series.id == series_id)
                        .options(selectinload(Series.service_refs))
                    )
                    series = result.scalar_one_or_none()
                    if series:
                        series.removed_at = datetime.now(UTC)
                        event_refs = [r for r in series.service_refs if r.path] or [
                            None
                        ]
                        for ref in event_refs:
                            series_events.append(
                                {
                                    "title": series.title,
                                    "tmdb_id": series.tmdb_id,
                                    "candidate_id": series_info["candidate_id"],
                                    "path": ref.path if ref else None,
                                    "service_type": ref.service
                                    if ref
                                    else Service.SONARR,
                                }
                            )

                    result = await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.id == series_info["candidate_id"]
                        )
                    )
                    candidate = result.scalar_one_or_none()
                    if candidate:
                        await db.delete(candidate)

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

            unique_deleted = len({s["series_id"] for s in series_to_delete})
            deleted_count += unique_deleted
            LOG.info(f"Successfully deleted {unique_deleted} series via Sonarr")
            for event in series_events:
                await _dispatch_reclaim_event(
                    action="deleted",
                    media_type=MediaType.SERIES,
                    **event,
                )
        except Exception as e:
            LOG.error(f"Error finalizing series deletion state: {e}", exc_info=True)

    # process unmonitor batches: mark unmonitored in Sonarr, then delete files + media server
    series_to_unmonitor: list[dict] = []
    for config_id, batch in series_to_unmonitor_by_config.items():
        client = sonarr_clients.get(config_id)
        if not client:
            continue
        LOG.info(f"Unmonitoring {len(batch)} series via Sonarr config {config_id}")
        sonarr_ids = [s["sonarr_id"] for s in batch]
        try:
            await client.unmonitor_series(sonarr_ids)
            series_to_unmonitor.extend(batch)
        except Exception as e:
            LOG.error(
                f"Error unmonitoring series via Sonarr config {config_id}: {e}",
                exc_info=True,
            )

    if series_to_unmonitor:
        unmonitor_series_events: list[dict[str, Any]] = []
        path_mappings_series = await _load_path_mappings()
        main_service_s = service_manager.main_media_server
        if main_service_s is None:
            unmonitor_svc_type: Service | None = None
        elif main_service_s is service_manager.jellyfin:
            unmonitor_svc_type = Service.JELLYFIN
        elif main_service_s is service_manager.emby:
            unmonitor_svc_type = Service.EMBY
        else:
            unmonitor_svc_type = Service.PLEX
        try:
            async with async_db() as db:
                seen_series_unmonitor: set[int] = set()
                for series_info in series_to_unmonitor:
                    series_id = series_info["series_id"]
                    if series_id in seen_series_unmonitor:
                        continue
                    seen_series_unmonitor.add(series_id)

                    result = await db.execute(
                        select(Series)
                        .where(Series.id == series_id)
                        .options(selectinload(Series.service_refs))
                    )
                    series = result.scalar_one_or_none()

                    if series:
                        # delete series folder from disk using arr_series_path
                        s_refs = series_data.get(series_id, {}).get("refs", [])
                        for _, _, arr_series_path in s_refs:
                            if not arr_series_path:
                                continue
                            local_path = Path(arr_series_path)
                            if not local_path.exists():
                                local_path = resolve_path(
                                    arr_series_path,
                                    path_mappings_series,
                                    service_type="sonarr",
                                )
                            if local_path and local_path.exists():
                                try:
                                    shutil.rmtree(str(local_path))
                                    LOG.info(
                                        f"Removed series folder: {local_path}"
                                    )
                                except Exception as fs_err:
                                    LOG.warning(
                                        f"shutil.rmtree failed for series '{series.title}' "
                                        f"at '{local_path}': {fs_err}"
                                    )
                                break  # only delete the matched folder once

                        # remove from media server
                        if main_service_s is not None and unmonitor_svc_type is not None:
                            ref = next(
                                (
                                    r
                                    for r in series.service_refs
                                    if r.service == unmonitor_svc_type
                                ),
                                None,
                            )
                            if ref:
                                try:
                                    await main_service_s.delete_item(ref.service_id)
                                except Exception as ms_err:
                                    LOG.warning(
                                        f"Media server delete_item failed for series "
                                        f"'{series.title}': {ms_err}"
                                    )

                        series.removed_at = datetime.now(UTC)
                        for ref in series.service_refs:
                            if ref.path:
                                unmonitor_series_events.append(
                                    {
                                        "title": series.title,
                                        "tmdb_id": series.tmdb_id,
                                        "candidate_id": series_info["candidate_id"],
                                        "path": ref.path,
                                        "service_type": ref.service,
                                    }
                                )

                    result = await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.id == series_info["candidate_id"]
                        )
                    )
                    candidate = result.scalar_one_or_none()
                    if candidate:
                        await db.delete(candidate)

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
                            action="unmonitored",
                        )
                    )

                await db.commit()

            unique_unmonitored_series = len(seen_series_unmonitor)
            deleted_count += unique_unmonitored_series
            LOG.info(
                f"Successfully unmonitored {unique_unmonitored_series} series via Sonarr "
                f"and cleaned up files"
            )
            for event in unmonitor_series_events:
                await _dispatch_reclaim_event(
                    action="unmonitored",
                    media_type=MediaType.SERIES,
                    **event,
                )
        except Exception as e:
            LOG.error(f"Error finalizing series unmonitor state: {e}", exc_info=True)

    # fallback to media server deletion for candidates not handled by any Sonarr instance
    if media_server_fallback_enabled:
        deleted_count += await _delete_series_via_media_server(
            candidates, series_to_delete + series_to_unmonitor, approved_by
        )
    else:
        series_handled_ids = {s["series_id"] for s in series_to_delete + series_to_unmonitor}
        unhandled = [c for c in candidates if c.series_id not in series_handled_ids]
        for cand in unhandled:
            title = series_data.get(cand.series_id or 0, {}).get("title", "unknown")
            LOG.warning(
                f"Media server fallback disabled — skipping deletion for "
                f"'{title}' (not handled by any Sonarr instance)"
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

    # load fallback toggle from settings
    async with async_db() as db:
        settings_row = (await db.execute(select(GeneralSettings))).scalars().first()
        media_server_fallback_enabled = (
            settings_row.media_server_fallback_enabled if settings_row else True
        )

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

    # load rules for arr_action resolution
    season_rule_ids = {rid for c in candidates for rid in (c.matched_rule_ids or [])}
    async with async_db() as db:
        season_rules_by_id: dict[int, ReclaimRule] = {}
        if season_rule_ids:
            season_rules_by_id = {
                r.id: r
                for r in (
                    await db.execute(
                        select(ReclaimRule).where(ReclaimRule.id.in_(season_rule_ids))
                    )
                )
                .scalars()
                .all()
            }

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

        # determine arr_action for this candidate
        cand_arr_action = _get_arr_action(candidate, season_rules_by_id)

        # try Sonarr first
        sonarr_ref_id: int | None = None
        sonarr_ref_config_id: int | None = None
        arr_series_path: str | None = None
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
                arr_series_path = ref.arr_series_path

        sonarr_client = None
        if sonarr_ref_config_id is not None:
            sonarr_client = service_manager.get_sonarr(sonarr_ref_config_id)
        if sonarr_client is None:
            sonarr_client = service_manager.sonarr

        if sonarr_client and sonarr_ref_id:
            # unmonitor first so Sonarr can't queue a re-grab if the delete is
            # slow or partially fails; if un-monitoring fails we abort entirely
            # rather than risk deleting files that will immediately be re-grabbed
            try:
                await sonarr_client.update_season_monitoring(
                    sonarr_ref_id, season_number, monitored=False
                )
                LOG.debug(
                    f"Unmonitored '{series_obj.title}' S{season_number:02d} in Sonarr"
                )
            except Exception as e:
                LOG.warning(
                    f"Failed to unmonitor '{series_obj.title}' S{season_number:02d} "
                    f"in Sonarr: {e} - aborting deletion to avoid re-grab"
                )
                continue

            if cand_arr_action == "unmonitor":
                # unmonitor path: delete files from disk, keep Sonarr entry
                season_path_mappings = await _load_path_mappings()
                season_folder: Path | None = None
                if arr_series_path:
                    local_series_path = Path(arr_series_path)
                    if not local_series_path.is_dir():
                        local_series_path = resolve_path(
                            arr_series_path,
                            season_path_mappings,
                            service_type="sonarr",
                        )
                    if local_series_path and local_series_path.is_dir():
                        season_folder = find_season_folder(
                            local_series_path, season_number
                        )
                if season_folder and season_folder.is_dir():
                    try:
                        shutil.rmtree(str(season_folder))
                        LOG.info(
                            f"Removed '{series_obj.title}' S{season_number:02d} "
                            f"folder: {season_folder}"
                        )
                        deleted_via_sonarr = True
                    except Exception as fs_err:
                        LOG.warning(
                            f"shutil.rmtree failed for '{series_obj.title}' "
                            f"S{season_number:02d} at '{season_folder}': {fs_err} "
                            f"- will attempt media server fallback"
                        )
                else:
                    LOG.warning(
                        f"Could not locate season folder for '{series_obj.title}' "
                        f"S{season_number:02d} (arr_series_path={arr_series_path!r})"
                    )
                # also remove from media server to keep library clean
                if deleted_via_sonarr:
                    media_svc = service_manager.main_media_server
                    if media_svc is None:
                        _season_svc_id = None
                    elif media_svc is service_manager.jellyfin:
                        _season_svc_id = season.jellyfin_season_id
                    elif media_svc is service_manager.emby:
                        _season_svc_id = season.emby_season_id
                    else:
                        _season_svc_id = season.plex_season_rating_key
                    if media_svc is not None and _season_svc_id:
                        try:
                            await media_svc.delete_item(_season_svc_id)
                        except Exception as ms_err:
                            LOG.warning(
                                f"Media server delete_item failed for "
                                f"'{series_obj.title}' S{season_number:02d}: {ms_err}"
                            )
            else:
                try:
                    await sonarr_client.delete_season_files(sonarr_ref_id, season_number)
                    deleted_via_sonarr = True
                    LOG.info(
                        f"Deleted '{series_obj.title}' S{season_number:02d} "
                        f"via Sonarr (sonarr_id={sonarr_ref_id})"
                    )
                except Exception as e:
                    LOG.warning(
                        f"Sonarr file deletion failed for '{series_obj.title}' "
                        f"S{season_number:02d}: {e} - will attempt media server fallback"
                    )

            # if no files remain across all seasons (delete path only), remove series
            if deleted_via_sonarr and cand_arr_action != "unmonitor":
                try:
                    fresh_series = await sonarr_client.get_series(sonarr_ref_id)
                    all_empty = all(
                        (s.statistics or {}).get("episodeFileCount", 0) == 0
                        for s in fresh_series.seasons
                    )
                    if all_empty:
                        await sonarr_client.delete_series(
                            sonarr_ref_id, delete_files=False
                        )
                        LOG.info(
                            f"Removed '{series_obj.title}' from Sonarr entirely "
                            f"(no files remaining, sonarr_id={sonarr_ref_id})"
                        )
                except Exception as e:
                    LOG.warning(
                        f"Could not check/remove empty series '{series_obj.title}' "
                        f"from Sonarr: {e}"
                    )

        # fall back to media server if Sonarr failed or unavailable
        if not deleted_via_sonarr:
            if not media_server_fallback_enabled:
                LOG.warning(
                    f"Media server fallback disabled — skipping season deletion for "
                    f"'{series_obj.title}' S{season_number:02d}. Enable 'Allow Media "
                    f"Server Fallback Deletion' in General Settings."
                )
                continue
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
                    action="unmonitored" if cand_arr_action == "unmonitor" else "deleted",
                )
            )
            await db.commit()

        deleted_count += 1
        if service_manager.main_media_server is service_manager.jellyfin:
            event_service_type = Service.JELLYFIN
        elif service_manager.main_media_server is service_manager.emby:
            event_service_type = Service.EMBY
        else:
            event_service_type = (
                Service.PLEX if service_manager.main_media_server else None
            )
        await _dispatch_reclaim_event(
            action="unmonitored" if cand_arr_action == "unmonitor" else "deleted",
            media_type=MediaType.SERIES,
            title=series_obj.title,
            tmdb_id=series_obj.tmdb_id,
            candidate_id=candidate.id,
            path=season.path,
            service_type=event_service_type,
            season_id=season.id,
            season_number=season_number,
        )

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

            # attempt filesystem sibling cleanup for each deleted version
            path_mappings = await _load_path_mappings()
            for ver in service_versions:
                local_path = resolve_path(
                    ver.path, path_mappings, service_type=main_service_type.value
                )
                if local_path:
                    try:
                        sibling_cleanup(local_path)
                    except Exception as fs_err:
                        LOG.warning(
                            f"sibling_cleanup failed for '{ver.path}': {fs_err}"
                        )

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
            for ver in service_versions:
                await _dispatch_reclaim_event(
                    action="deleted",
                    media_type=MediaType.MOVIE,
                    title=movie.title,
                    tmdb_id=movie.tmdb_id,
                    candidate_id=candidate.id,
                    path=ver.path,
                    service_type=main_service_type,
                    movie_version_id=ver.id,
                )

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
            await _dispatch_reclaim_event(
                action="deleted",
                media_type=MediaType.SERIES,
                title=series_obj.title,
                tmdb_id=series_obj.tmdb_id,
                candidate_id=candidate.id,
                path=ref.path,
                service_type=main_service_type,
            )

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


async def move_specific_candidates(
    candidate_ids: list[int], approved_by: str = "system"
) -> tuple[int, int]:
    """Move specific reclaim candidates to the configured destination root.

    Resolves each candidate's file path using configured path mappings, moves the
    file (and same stem siblings) to ``move_destination_root``, then removes the
    item from the arr/media-server without deleting the original (since it was
    already moved).

    Returns (moved_count, failed_count).
    """
    if not candidate_ids:
        return 0, 0

    # load move settings + path mappings from DB
    async with async_db() as db:
        result = await db.execute(select(GeneralSettings))
        gen_settings = result.scalars().first()

    if not gen_settings or not gen_settings.move_enabled:
        LOG.warning("move_specific_candidates called but move is not enabled")
        return 0, len(candidate_ids)

    destination_movies_str = gen_settings.move_destination_movies or ""
    destination_series_str = gen_settings.move_destination_series or ""
    path_mappings = gen_settings.path_mappings or []

    # load candidates with their version/movie data
    async with async_db() as db:
        result = await db.execute(
            select(ReclaimCandidate).where(ReclaimCandidate.id.in_(candidate_ids))
        )
        candidates = result.scalars().all()
        candidate_version_ids = [
            c.movie_version_id for c in candidates if c.movie_version_id
        ]
        ver_result = await db.execute(
            select(MovieVersion)
            .where(MovieVersion.id.in_(candidate_version_ids))
            .options(selectinload(MovieVersion.movie))
        )
        versions_by_id: dict[int, MovieVersion] = {
            v.id: v for v in ver_result.scalars().all()
        }

    moved = 0
    failed = 0

    #### movie candidates ####
    movie_candidates = [c for c in candidates if c.media_type == MediaType.MOVIE]
    for candidate in movie_candidates:
        try:
            version = (
                versions_by_id.get(candidate.movie_version_id)
                if candidate.movie_version_id
                else None
            )
            if version is None:
                LOG.warning(
                    f"move_specific_candidates: candidate {candidate.id} has no movie version"
                )
                failed += 1
                continue

            movie = version.movie
            if movie is None:
                failed += 1
                continue

            # pick destination root based on media type
            dest_str = (
                destination_series_str
                if candidate.media_type == MediaType.SERIES
                else destination_movies_str
            )
            if not dest_str.strip():
                LOG.warning(
                    f"move_specific_candidates: destination not configured for "
                    f"{candidate.media_type} (candidate {candidate.id})"
                )
                failed += 1
                continue
            destination_root = Path(dest_str)

            local_path = resolve_path(
                version.path, path_mappings, service_type=version.service.value
            )
            if local_path is None:
                LOG.warning(
                    f"move_specific_candidates: cannot resolve path for '{version.path}' "
                    f"(candidate {candidate.id})"
                )
                failed += 1
                continue

            # move the file + same stem siblings to destination
            dest = move_media(local_path, destination_root)

            # unmonitor in Radarr so it doesn't re-queue a download
            radarr_clients = service_manager.radarr_clients()
            if not radarr_clients and service_manager.radarr:
                radarr_clients = {0: service_manager.radarr}
            if radarr_clients and movie.id:
                try:
                    async with async_db() as db:
                        arr_refs = (
                            await db.execute(
                                select(
                                    MovieArrRef.service_config_id,
                                    MovieArrRef.arr_movie_id,
                                    MovieArrRef.arr_movie_path,
                                ).where(MovieArrRef.movie_id == movie.id)
                            )
                        ).all()
                    norm_ver = version.path.rstrip("/") if version.path else None
                    unmonitored: set[tuple[int, int]] = set()
                    for config_id, arr_movie_id, arr_movie_path in arr_refs:
                        if config_id not in radarr_clients or arr_movie_id is None:
                            continue
                        if norm_ver and arr_movie_path:
                            if not norm_ver.startswith(arr_movie_path.rstrip("/") + "/"):
                                continue
                        pair = (config_id, arr_movie_id)
                        if pair not in unmonitored:
                            await radarr_clients[config_id].unmonitor_movies([arr_movie_id])
                            unmonitored.add(pair)
                            LOG.info(
                                f"move: unmonitored '{movie.title}' in Radarr "
                                f"(config_id={config_id}, arr_id={arr_movie_id})"
                            )
                except Exception as arr_err:
                    LOG.warning(
                        f"move_specific_candidates: Radarr unmonitor failed for "
                        f"'{movie.title}': {arr_err}"
                    )

            # remove the item from the media server (no file deletion)
            try:
                main_service = service_manager.main_media_server
                if main_service:
                    await main_service.delete_movie_version(
                        version.service_item_id, version.service_media_id
                    )
            except Exception as svc_err:
                LOG.warning(
                    f"move_specific_candidates: service removal failed for "
                    f"'{movie.title}' after move: {svc_err}"
                )

            # update DB
            async with async_db() as db:
                # bulk-delete all candidates and delete-requests referencing this
                # version before removing the version row (no ondelete on their FKs)
                await db.execute(
                    delete(ReclaimCandidate).where(
                        ReclaimCandidate.movie_version_id == version.id
                    )
                )
                await db.execute(
                    delete(DeleteRequest).where(
                        DeleteRequest.movie_version_id == version.id
                    )
                )

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
                        action="moved",
                        destination_path=str(dest),
                    )
                )
                await db.commit()

            moved += 1
            LOG.info(f"Moved '{movie.title}' version {version.id} to {dest}")
            await _dispatch_reclaim_event(
                action="moved",
                media_type=MediaType.MOVIE,
                title=movie.title,
                tmdb_id=movie.tmdb_id,
                candidate_id=candidate.id,
                path=version.path,
                local_path=str(local_path),
                destination_path=str(dest),
                service_type=version.service,
                movie_version_id=version.id,
            )

        except Exception as e:
            LOG.error(
                f"move_specific_candidates: failed for candidate {candidate.id}: {e}",
                exc_info=True,
            )
            failed += 1

    #### series / season candidates ####
    series_candidates = [c for c in candidates if c.media_type == MediaType.SERIES]
    if series_candidates:
        series_ids = list({c.series_id for c in series_candidates if c.series_id})
        season_ids = [c.season_id for c in series_candidates if c.season_id]

        async with async_db() as db:
            series_result = await db.execute(
                select(Series)
                .where(Series.id.in_(series_ids))
                .options(selectinload(Series.service_refs))
            )
            series_map: dict[int, Series] = {
                s.id: s for s in series_result.scalars().all()
            }

            seasons_map: dict[int, Season] = {}
            if season_ids:
                seasons_result = await db.execute(
                    select(Season).where(Season.id.in_(season_ids))
                )
                seasons_map = {s.id: s for s in seasons_result.scalars().all()}

            # load arr refs for Sonarr unmonitor routing
            arr_refs_result = await db.execute(
                select(
                    SeriesArrRef.series_id,
                    SeriesArrRef.service_config_id,
                    SeriesArrRef.arr_series_id,
                    SeriesArrRef.arr_series_path,
                ).where(SeriesArrRef.series_id.in_(series_ids))
            )
            # series_id -> list of (config_id, arr_series_id, arr_series_path)
            series_arr_refs: dict[int, list[tuple]] = {}
            for s_id, config_id, arr_s_id, arr_s_path in arr_refs_result.all():
                series_arr_refs.setdefault(s_id, []).append((config_id, arr_s_id, arr_s_path))

        for candidate in series_candidates:
            try:
                series_obj = (
                    series_map.get(candidate.series_id) if candidate.series_id else None
                )
                if not series_obj:
                    LOG.warning(
                        f"move_specific_candidates: no series for candidate {candidate.id}"
                    )
                    failed += 1
                    continue

                if not destination_series_str.strip():
                    LOG.warning(
                        f"move_specific_candidates: series destination not configured "
                        f"(candidate {candidate.id})"
                    )
                    failed += 1
                    continue
                destination_root = Path(destination_series_str)

                # pick the primary service ref that has a path
                series_ref = next((r for r in series_obj.service_refs if r.path), None)
                if not series_ref or not series_ref.path:
                    LOG.warning(
                        f"move_specific_candidates: no path on service ref for "
                        f"'{series_obj.title}' (candidate {candidate.id})"
                    )
                    failed += 1
                    continue

                local_series_path = resolve_path(
                    series_ref.path,
                    path_mappings,
                    service_type=series_ref.service.value,
                )
                if local_series_path is None:
                    LOG.warning(
                        f"move_specific_candidates: cannot resolve path for "
                        f"'{series_obj.title}' (candidate {candidate.id})"
                    )
                    failed += 1
                    continue

                is_season = candidate.season_id is not None
                season = (
                    seasons_map.get(candidate.season_id)
                    if is_season and candidate.season_id
                    else None
                )
                season_folder: Path | None = None

                if is_season:
                    if season is None:
                        LOG.warning(
                            f"move_specific_candidates: season record missing "
                            f"(candidate {candidate.id})"
                        )
                        failed += 1
                        continue
                    resolved_season_path = (
                        resolve_path(
                            season.path,
                            path_mappings,
                            service_type=series_ref.service.value,
                        )
                        if season.path
                        else None
                    )
                    season_folder = resolved_season_path or find_season_folder(
                        local_series_path, season.season_number
                    )
                    if season_folder is None:
                        LOG.warning(
                            f"move_specific_candidates: cannot find season folder "
                            f"S{season.season_number:02d} inside '{local_series_path}' "
                            f"(candidate {candidate.id})"
                        )
                        failed += 1
                        continue

                    # flat series: episodes live directly in the series root with
                    # no season sub folders (move only this season's files so
                    # other seasons are left intact)
                    if season_folder == local_series_path:
                        dest = move_season_files(
                            local_series_path,
                            destination_root,
                            episode_paths=season.episode_paths or [],
                            path_mappings=path_mappings,
                        )
                    else:
                        # nest under the series folder name so the destination is readable
                        series_dest_root = destination_root / local_series_path.name
                        dest = move_directory(season_folder, series_dest_root)
                else:
                    dest = move_directory(local_series_path, destination_root)

                # unmonitor in Sonarr so it doesn't re-queue a download
                sonarr_clients = service_manager.sonarr_clients()
                if not sonarr_clients and service_manager.sonarr:
                    sonarr_clients = {0: service_manager.sonarr}
                if sonarr_clients and candidate.series_id:
                    try:
                        refs = series_arr_refs.get(candidate.series_id, [])
                        for config_id, arr_s_id, _arr_s_path in refs:
                            if config_id not in sonarr_clients or arr_s_id is None:
                                continue
                            if is_season and season:
                                await sonarr_clients[config_id].update_season_monitoring(
                                    arr_s_id, season.season_number, False
                                )
                                LOG.info(
                                    f"move: unmonitored '{series_obj.title}' "
                                    f"S{season.season_number:02d} in Sonarr "
                                    f"(config_id={config_id}, arr_id={arr_s_id})"
                                )
                            else:
                                await sonarr_clients[config_id].unmonitor_series([arr_s_id])
                                LOG.info(
                                    f"move: unmonitored '{series_obj.title}' in Sonarr "
                                    f"(config_id={config_id}, arr_id={arr_s_id})"
                                )
                    except Exception as arr_err:
                        LOG.warning(
                            f"move_specific_candidates: Sonarr unmonitor failed for "
                            f"'{series_obj.title}': {arr_err}"
                        )

                # remove from media server (no file deletion - already moved)
                try:
                    main_service = service_manager.main_media_server
                    if main_service and series_ref:
                        if is_season and season:
                            # delete the season item from the media server
                            if main_service is service_manager.jellyfin:
                                season_service_id = season.jellyfin_season_id
                            elif main_service is service_manager.emby:
                                season_service_id = season.emby_season_id
                            else:
                                season_service_id = season.plex_season_rating_key
                            if season_service_id:
                                await main_service.delete_item(season_service_id)
                        else:
                            await main_service.delete_item(series_ref.service_id)
                except Exception as svc_err:
                    LOG.warning(
                        f"move_specific_candidates: media server removal failed for "
                        f"'{series_obj.title}' after move: {svc_err}"
                    )

                # update DB
                async with async_db() as db:
                    cand_result = await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.id == candidate.id
                        )
                    )
                    cand = cand_result.scalar_one_or_none()
                    if cand:
                        await db.delete(cand)

                    if is_season and season:
                        # reduce series size and remove season row
                        series_db = (
                            await db.execute(
                                select(Series).where(Series.id == candidate.series_id)
                            )
                        ).scalar_one_or_none()
                        if series_db and series_db.size and season.size:
                            series_db.size = max(0, series_db.size - season.size)

                        season_db = (
                            await db.execute(
                                select(Season).where(Season.id == candidate.season_id)
                            )
                        ).scalar_one_or_none()
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

                        history_name = f"{series_obj.title} S{season.season_number:02d}"
                        history_size = season.size
                    else:
                        # mark entire series as removed
                        series_db = (
                            await db.execute(
                                select(Series).where(Series.id == candidate.series_id)
                            )
                        ).scalar_one_or_none()
                        if series_db:
                            series_db.removed_at = datetime.now(UTC)

                        history_name = series_obj.title
                        history_size = series_obj.size

                    db.add(
                        ReclaimHistory(
                            approved_by=approved_by,
                            media_type=MediaType.SERIES,
                            tmdb_id=series_obj.tmdb_id,
                            name=history_name,
                            path=series_ref.path,
                            size=history_size,
                            action="moved",
                            destination_path=str(dest),
                        )
                    )
                    await db.commit()

                moved += 1
                LOG.info(f"Moved '{series_obj.title}' to {dest}")
                event_local_path = (
                    season_folder if is_season and season else local_series_path
                )
                await _dispatch_reclaim_event(
                    action="moved",
                    media_type=MediaType.SERIES,
                    title=series_obj.title,
                    tmdb_id=series_obj.tmdb_id,
                    candidate_id=candidate.id,
                    path=season.path if is_season and season else series_ref.path,
                    local_path=str(event_local_path),
                    destination_path=str(dest),
                    service_type=series_ref.service,
                    season_id=season.id if is_season and season else None,
                    season_number=season.season_number
                    if is_season and season
                    else None,
                )

            except Exception as e:
                LOG.error(
                    f"move_specific_candidates: failed for series candidate "
                    f"{candidate.id}: {e}",
                    exc_info=True,
                )
                failed += 1

    LOG.info(f"Move complete: {moved} moved, {failed} failed")
    return moved, failed
