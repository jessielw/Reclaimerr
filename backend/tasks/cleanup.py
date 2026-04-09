from datetime import datetime, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.task_tracking import track_task_execution
from backend.database import async_db
from backend.database.models import (
    GeneralSettings,
    Movie,
    ProtectedMedia,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
    ServiceConfig,
)
from backend.enums import MediaType, NotificationType, Service, Task
from backend.services.notifications import notify_all_users
from backend.types import MEDIA_SERVERS

__all__ = [
    "scan_cleanup_candidates",
    "tag_cleanup_candidates",
    "delete_cleanup_candidates",
    "delete_specific_candidates",
]


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

        # separate rules by media type
        movie_rules = [r for r in rules if r.media_type == MediaType.MOVIE]
        series_rules = [r for r in rules if r.media_type == MediaType.SERIES]

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

            # also evaluate at season level for series rules
            s_cr, s_up, s_rm = await _process_series_seasons(db, series_rules)
            candidates_created += s_cr
            candidates_updated += s_up
            candidates_removed += s_rm
        else:
            # no series rules active - remove stale series candidates
            del_result = await db.execute(
                delete(ReclaimCandidate).where(
                    ReclaimCandidate.media_type == MediaType.SERIES
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
    # get all media items
    if media_type is MediaType.MOVIE:
        result = await db.execute(
            select(Movie)
            .where(Movie.removed_at.is_(None))
            .options(selectinload(Movie.versions))
        )
        media_items = result.scalars().all()
        id_field = "movie_id"
    else:
        result = await db.execute(
            select(Series)
            .where(Series.removed_at.is_(None))
            .options(selectinload(Series.service_refs))
        )
        media_items = result.scalars().all()
        id_field = "series_id"

    LOG.info(f"Processing {len(media_items)} {media_type.value} items")

    # fetch all protected items for this media type to skip them
    now = datetime.now(timezone.utc)
    if media_type is MediaType.MOVIE:
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
        protected_ids = {b.movie_id for b in protected_result.scalars().all()}
    else:
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

    LOG.info(f"Found {len(protected_ids)} protected {media_type.value} items to skip")

    # fetch all existing candidates for this media type once (avoid N queries in loop)
    if media_type is MediaType.MOVIE:
        result = await db.execute(
            select(ReclaimCandidate).where(
                ReclaimCandidate.media_type == MediaType.MOVIE
            )
        )
    else:
        # only series-level candidates (season_id IS NULL)
        result = await db.execute(
            select(ReclaimCandidate).where(
                ReclaimCandidate.media_type == MediaType.SERIES,
                ReclaimCandidate.season_id.is_(None),
            )
        )
    existing_candidates = result.scalars().all()

    # build lookup - movie_id/series_id -> candidate
    candidate_lookup = {}
    for candidate in existing_candidates:
        key = (
            candidate.movie_id if media_type is MediaType.MOVIE else candidate.series_id
        )
        if key:
            candidate_lookup[key] = candidate

    candidates_created = 0
    candidates_updated = 0
    matched_item_ids: set[int] = set()

    for item in media_items:
        # skip protected items
        if item.id in protected_ids:
            continue

        # evaluate all rules against this item
        matched_rules = []
        matched_criteria = {}
        reasons = []

        for rule in rules:
            if _evaluate_rule(item, rule, matched_criteria, reasons):
                matched_rules.append(rule.id)

        # if item matches at least one rule, create/update candidate
        if matched_rules:
            matched_item_ids.add(item.id)
            # check if candidate already exists using lookup dict
            existing = candidate_lookup.get(item.id)

            combined_reason = "; ".join(reasons)

            # calculate space savings (convert bytes to GB)
            space_gb = item.size / (1024**3) if item.size else None

            if existing:
                # update existing candidate
                existing.matched_rule_ids = matched_rules
                existing.matched_criteria = matched_criteria
                existing.reason = combined_reason
                existing.estimated_space_gb = space_gb
                existing.updated_at = datetime.now(timezone.utc)
                candidates_updated += 1
            else:
                # create new candidate
                candidate_data = {
                    "media_type": media_type,
                    "matched_rule_ids": matched_rules,
                    "matched_criteria": matched_criteria,
                    "reason": combined_reason,
                    "estimated_space_gb": space_gb,
                }
                if id_field == "movie_id":
                    candidate_data["movie_id"] = item.id
                else:
                    candidate_data["series_id"] = item.id

                candidate = ReclaimCandidate(**candidate_data)
                db.add(candidate)
                candidates_created += 1

    # remove candidates for items that no longer match any rule (or are now protected)
    stale_candidates = [
        c for key, c in candidate_lookup.items() if key not in matched_item_ids
    ]
    candidates_removed = len(stale_candidates)
    for candidate in stale_candidates:
        await db.delete(candidate)

    # commit if anything changed
    if candidates_created > 0 or candidates_updated > 0 or candidates_removed > 0:
        await db.commit()

    return candidates_created, candidates_updated, candidates_removed


async def _process_series_seasons(
    db: AsyncSession, rules: list[ReclaimRule]
) -> tuple[int, int, int]:
    """Evaluate each series' seasons against rules and create/update season-level candidates.

    Returns (created_count, updated_count, removed_count).
    """
    # load all non-deleted series with their seasons and service refs
    result = await db.execute(
        select(Series)
        .where(Series.removed_at.is_(None))
        .options(selectinload(Series.service_refs), selectinload(Series.seasons))
    )
    all_series = result.scalars().all()

    if not all_series:
        return 0, 0, 0

    # whole-series protection also covers every season of that series
    now = datetime.now(timezone.utc)
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

    # load all existing season-level candidates
    existing_result = await db.execute(
        select(ReclaimCandidate).where(
            ReclaimCandidate.media_type == MediaType.SERIES,
            ReclaimCandidate.season_id.isnot(None),
        )
    )
    season_candidate_lookup: dict[int, ReclaimCandidate] = {
        c.season_id: c
        for c in existing_result.scalars().all()
        if c.season_id is not None
    }

    candidates_created = 0
    candidates_updated = 0
    matched_season_ids: set[int] = set()

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
            reasons: list[str] = []

            for rule in rules:
                if _evaluate_rule_for_season(
                    series, season, rule, matched_criteria, reasons
                ):
                    matched_rules.append(rule.id)

            if not matched_rules:
                continue

            matched_season_ids.add(season.id)
            combined_reason = "; ".join(reasons)
            space_gb = season.size / (1024**3) if season.size else None
            existing = season_candidate_lookup.get(season.id)

            if existing:
                existing.matched_rule_ids = matched_rules
                existing.matched_criteria = matched_criteria
                existing.reason = combined_reason
                existing.estimated_space_gb = space_gb
                existing.updated_at = datetime.now(timezone.utc)
                candidates_updated += 1
            else:
                db.add(
                    ReclaimCandidate(
                        media_type=MediaType.SERIES,
                        matched_rule_ids=matched_rules,
                        matched_criteria=matched_criteria,
                        reason=combined_reason,
                        estimated_space_gb=space_gb,
                        series_id=series.id,
                        season_id=season.id,
                    )
                )
                candidates_created += 1

    # remove season candidates that no longer match any rule
    stale = [
        c for sid, c in season_candidate_lookup.items() if sid not in matched_season_ids
    ]
    candidates_removed = len(stale)
    for c in stale:
        await db.delete(c)

    if candidates_created > 0 or candidates_updated > 0 or candidates_removed > 0:
        await db.commit()

    return candidates_created, candidates_updated, candidates_removed


def _evaluate_rule_for_season(
    series: Series,
    season: Season,
    rule: ReclaimRule,
    matched_criteria: dict,
    reasons: list[str],
) -> bool:
    """Evaluate a cleanup rule against a single season.

    TMDB metadata criteria (popularity, vote_average, vote_count, library_ids) are
    sourced from the parent series; per-season watch/size data comes from the Season row.
    """
    has_criteria = any(
        (
            rule.library_ids is not None,
            rule.min_popularity is not None,
            rule.max_popularity is not None,
            rule.min_vote_average is not None,
            rule.max_vote_average is not None,
            rule.min_vote_count is not None,
            rule.max_vote_count is not None,
            rule.min_view_count is not None,
            rule.max_view_count is not None,
            rule.include_never_watched is not None,
            rule.min_days_since_added is not None,
            rule.max_days_since_added is not None,
            rule.min_days_since_last_watched is not None,
            rule.max_days_since_last_watched is not None,
            rule.min_size is not None,
            rule.max_size is not None,
        )
    )
    if not has_criteria:
        return False

    rule_reasons: list[str] = []

    # skip seasons with no files
    if not season.size or season.size == 0:
        return False

    # library filtering via parent series' service refs
    if rule.library_ids is not None and len(rule.library_ids) > 0:
        item_libraries = [ref.library_id for ref in series.service_refs]
        if not any(lib in rule.library_ids for lib in item_libraries):
            return False

    # TMDB popularity from parent series
    if rule.min_popularity is not None and (
        series.popularity is None or series.popularity < rule.min_popularity
    ):
        return False
    if rule.max_popularity is not None and (
        series.popularity is None or series.popularity > rule.max_popularity
    ):
        return False
    if rule.min_popularity is not None or rule.max_popularity is not None:
        matched_criteria["popularity"] = series.popularity
        rule_reasons.append(f"Popularity {series.popularity}")

    # TMDB vote average from parent series
    if rule.min_vote_average is not None and (
        series.vote_average is None or series.vote_average < rule.min_vote_average
    ):
        return False
    if rule.max_vote_average is not None and (
        series.vote_average is None or series.vote_average > rule.max_vote_average
    ):
        return False
    if rule.min_vote_average is not None or rule.max_vote_average is not None:
        matched_criteria["vote_average"] = series.vote_average
        rule_reasons.append(f"Rating {series.vote_average}/10")

    # TMDB vote count from parent series
    if rule.min_vote_count is not None and (
        series.vote_count is None or series.vote_count < rule.min_vote_count
    ):
        return False
    if rule.max_vote_count is not None and (
        series.vote_count is None or series.vote_count > rule.max_vote_count
    ):
        return False
    if rule.min_vote_count is not None or rule.max_vote_count is not None:
        matched_criteria["vote_count"] = series.vote_count
        rule_reasons.append(f"{series.vote_count} votes")

    # season view count
    if (
        rule.min_view_count is not None
        and season.view_count is not None
        and season.view_count < rule.min_view_count
    ):
        return False
    if (
        rule.max_view_count is not None
        and season.view_count is not None
        and season.view_count > rule.max_view_count
    ):
        return False

    if rule.include_never_watched is False and season.never_watched:
        return False

    if (
        rule.min_view_count is not None
        or rule.max_view_count is not None
        or rule.include_never_watched is False
    ):
        matched_criteria["view_count"] = season.view_count
        matched_criteria["never_watched"] = season.never_watched
        if season.never_watched:
            rule_reasons.append(f"S{season.season_number:02d} never watched")
        else:
            rule_reasons.append(
                f"S{season.season_number:02d} watched {season.view_count} time(s)"
            )

    # days since added (season level)
    if season.added_at:
        days_since_added = (
            datetime.now(timezone.utc) - season.added_at.replace(tzinfo=timezone.utc)
        ).days
        if (
            rule.min_days_since_added is not None
            and days_since_added < rule.min_days_since_added
        ):
            return False
        if (
            rule.max_days_since_added is not None
            and days_since_added > rule.max_days_since_added
        ):
            return False
        if (
            rule.min_days_since_added is not None
            or rule.max_days_since_added is not None
        ):
            matched_criteria["days_since_added"] = days_since_added
            rule_reasons.append(
                f"S{season.season_number:02d} added {days_since_added} days ago"
            )

    # days since last watched (season level)
    if season.last_viewed_at:
        days_since_last_watched = (
            datetime.now(timezone.utc)
            - season.last_viewed_at.replace(tzinfo=timezone.utc)
        ).days
        if (
            rule.min_days_since_last_watched is not None
            and days_since_last_watched < rule.min_days_since_last_watched
        ):
            return False
        if (
            rule.max_days_since_last_watched is not None
            and days_since_last_watched > rule.max_days_since_last_watched
        ):
            return False
        if (
            rule.min_days_since_last_watched is not None
            or rule.max_days_since_last_watched is not None
        ):
            matched_criteria["days_since_last_watched"] = days_since_last_watched
            rule_reasons.append(
                f"S{season.season_number:02d} last watched {days_since_last_watched} days ago"
            )
    elif (
        rule.min_days_since_last_watched is not None
        or rule.max_days_since_last_watched is not None
    ):
        return False

    # season size
    if rule.min_size is not None and (
        season.size is None or season.size < rule.min_size
    ):
        return False
    if rule.max_size is not None and (
        season.size is None or season.size > rule.max_size
    ):
        return False
    if rule.min_size is not None or rule.max_size is not None:
        matched_criteria["size"] = season.size
        size_gb = season.size / (1024**3) if season.size else 0
        rule_reasons.append(f"S{season.season_number:02d} size {size_gb:.2f} GB")

    if rule_reasons:
        reasons.append(
            f"{rule.name} (S{season.season_number:02d}): {', '.join(rule_reasons)}"
        )
    return True


def _evaluate_rule(
    item: Movie | Series, rule: ReclaimRule, matched_criteria: dict, reasons: list[str]
) -> bool:
    """
    Evaluate if an item matches a cleanup rule.

    All non-null criteria must match (AND logic).
    Updates matched_criteria dict and reasons list if matched.

    Returns True if item matches rule.
    """
    # validate rule has at least one criterion set
    has_criteria = any(
        (
            rule.library_ids is not None,
            rule.min_popularity is not None,
            rule.max_popularity is not None,
            rule.min_vote_average is not None,
            rule.max_vote_average is not None,
            rule.min_vote_count is not None,
            rule.max_vote_count is not None,
            rule.min_view_count is not None,
            rule.max_view_count is not None,
            rule.include_never_watched is not None,
            rule.min_days_since_added is not None,
            rule.max_days_since_added is not None,
            rule.min_days_since_last_watched is not None,
            rule.max_days_since_last_watched is not None,
            rule.min_size is not None,
            rule.max_size is not None,
        )
    )

    if not has_criteria:
        LOG.warning(f"Rule '{rule.name}' has no criteria set, skipping")
        return False

    rule_reasons = []

    # always exclude items with no media (size = 0 or None)
    if not item.size or item.size == 0:
        return False

    # check library filtering (check across all stored versions/refs)
    if rule.library_ids is not None and len(rule.library_ids) > 0:
        refs = item.versions if isinstance(item, Movie) else item.service_refs
        item_libraries = [v.library_id for v in refs]
        if not any(lib in rule.library_ids for lib in item_libraries):
            return False

    # check popularity
    if rule.min_popularity is not None and (
        item.popularity is None or item.popularity < rule.min_popularity
    ):
        return False
    if rule.max_popularity is not None and (
        item.popularity is None or item.popularity > rule.max_popularity
    ):
        return False
    if rule.min_popularity is not None or rule.max_popularity is not None:
        matched_criteria["popularity"] = item.popularity
        rule_reasons.append(f"Popularity {item.popularity}")

    # check vote average
    if rule.min_vote_average is not None and (
        item.vote_average is None or item.vote_average < rule.min_vote_average
    ):
        return False
    if rule.max_vote_average is not None and (
        item.vote_average is None or item.vote_average > rule.max_vote_average
    ):
        return False
    if rule.min_vote_average is not None or rule.max_vote_average is not None:
        matched_criteria["vote_average"] = item.vote_average
        rule_reasons.append(f"Rating {item.vote_average}/10")

    # check vote count
    if rule.min_vote_count is not None and (
        item.vote_count is None or item.vote_count < rule.min_vote_count
    ):
        return False
    if rule.max_vote_count is not None and (
        item.vote_count is None or item.vote_count > rule.max_vote_count
    ):
        return False
    if rule.min_vote_count is not None or rule.max_vote_count is not None:
        matched_criteria["vote_count"] = item.vote_count
        rule_reasons.append(f"{item.vote_count} votes")

    # check view count
    if rule.min_view_count is not None and item.view_count < rule.min_view_count:
        return False
    if rule.max_view_count is not None and item.view_count > rule.max_view_count:
        return False

    # check never watched flag
    # include_never_watched=True (default/None): Include both watched and never-watched (no filter)
    # include_never_watched=False: Exclude never-watched items (only watched items)
    if rule.include_never_watched is False:
        if item.never_watched:
            return False

    # log view/watch status if we're filtering by it
    if (
        rule.min_view_count is not None
        or rule.max_view_count is not None
        or rule.include_never_watched is False
    ):
        matched_criteria["view_count"] = item.view_count
        matched_criteria["never_watched"] = item.never_watched
        if item.never_watched:
            rule_reasons.append("Never watched")
        else:
            rule_reasons.append(f"Watched {item.view_count} time(s)")

    # check days since added
    if item.added_at:
        days_since_added = (
            datetime.now(timezone.utc) - item.added_at.replace(tzinfo=timezone.utc)
        ).days
        if (
            rule.min_days_since_added is not None
            and days_since_added < rule.min_days_since_added
        ):
            return False
        if (
            rule.max_days_since_added is not None
            and days_since_added > rule.max_days_since_added
        ):
            return False
        if (
            rule.min_days_since_added is not None
            or rule.max_days_since_added is not None
        ):
            matched_criteria["days_since_added"] = days_since_added
            rule_reasons.append(f"Added {days_since_added} days ago")

    # check days since last watched
    if item.last_viewed_at:
        days_since_last_watched = (
            datetime.now(timezone.utc)
            - item.last_viewed_at.replace(tzinfo=timezone.utc)
        ).days
        if (
            rule.min_days_since_last_watched is not None
            and days_since_last_watched < rule.min_days_since_last_watched
        ):
            return False
        if (
            rule.max_days_since_last_watched is not None
            and days_since_last_watched > rule.max_days_since_last_watched
        ):
            return False
        if (
            rule.min_days_since_last_watched is not None
            or rule.max_days_since_last_watched is not None
        ):
            matched_criteria["days_since_last_watched"] = days_since_last_watched
            rule_reasons.append(f"Last watched {days_since_last_watched} days ago")
    elif (
        rule.min_days_since_last_watched is not None
        or rule.max_days_since_last_watched is not None
    ):
        # if filtering by days since last watched but item was never watched, exclude it
        return False

    # check size (bytes)
    if rule.min_size is not None and (item.size is None or item.size < rule.min_size):
        return False
    if rule.max_size is not None and (item.size is None or item.size > rule.max_size):
        return False
    if rule.min_size is not None or rule.max_size is not None:
        matched_criteria["size"] = item.size
        size_gb = item.size / (1024**3) if item.size else 0
        rule_reasons.append(f"Size {size_gb:.2f} GB")

    # all criteria matched!
    if rule_reasons:
        reasons.append(f"{rule.name}: {', '.join(rule_reasons)}")
    return True


async def tag_cleanup_candidates() -> None:
    """Sync tags for cleanup candidates in Radarr/Sonarr.

    Efficiently tags candidates and removes tags from items no longer candidates.
    Uses bulk operations for performance.
    """
    tag = "reclaimerr"

    # check if services are configured before doing any work
    if not service_manager.radarr and not service_manager.sonarr:
        LOG.debug("Neither Radarr nor Sonarr configured, skipping tag sync")
        return

    # check if auto-tagging is enabled in settings before doing any work
    tagging_enabled = False
    async with async_db() as db:
        result = await db.execute(select(GeneralSettings))
        settings = result.scalars().first()
        if settings:
            tagging_enabled = settings.auto_tag_enabled
            tag = f"{tag}{settings.cleanup_tag_suffix or ''}"

    # if tagging is disabled, skip entire process (don't even query media or candidates)
    if not tagging_enabled:
        LOG.debug("Auto-tagging disabled in settings, skipping")
        return

    LOG.info(f"Starting cleanup candidate tagging (tag: {tag})")

    async with track_task_execution(Task.TAG_CLEANUP_CANDIDATES):
        try:
            movies_tagged = 0
            movies_untagged = 0
            series_tagged = 0
            series_untagged = 0

            # process Radarr movies
            if service_manager.radarr:
                tagged, untagged = await _sync_radarr_tags(tag)
                movies_tagged = tagged
                movies_untagged = untagged

            # process Sonarr series
            if service_manager.sonarr:
                tagged, untagged = await _sync_sonarr_tags(tag)
                series_tagged = tagged
                series_untagged = untagged

            LOG.info(
                f"Tag sync completed: Movies ({movies_tagged} tagged, {movies_untagged} untagged), "
                f"Series ({series_tagged} tagged, {series_untagged} untagged)"
            )

        except Exception as e:
            LOG.error(f"Error syncing cleanup tags: {e}", exc_info=True)
            raise


async def _sync_radarr_tags(cleanup_tag: str) -> tuple[int, int]:
    """Sync Radarr movie tags. Returns (tagged_count, untagged_count)."""
    if not service_manager.radarr:
        return 0, 0

    # get all tags to detect old cleanup tags
    all_tags = await service_manager.radarr.get_tags()

    # get or create the current cleanup tag
    tag = await service_manager.radarr.get_or_create_tag(cleanup_tag)
    LOG.debug(f"Using Radarr tag '{tag.label}' (ID: {tag.id})")

    # find old reclaimerr tags (starts with 'reclaimerr' but isn't current tag)
    old_tags = [
        t for t in all_tags if t.label.startswith("reclaimerr") and t.id != tag.id
    ]
    if old_tags:
        LOG.info(
            f"Found {len(old_tags)} old cleanup tags to migrate: {[t.label for t in old_tags]}"
        )

    # get all movies from Radarr
    all_movies = await service_manager.radarr.get_all_movies()
    LOG.debug(f"Found {len(all_movies)} movies in Radarr")

    # build lookup: radarr_id -> movie
    movies_by_id = {movie.id: movie for movie in all_movies}

    # get radarr IDs for all movie candidates from database
    async with async_db() as db:
        result = await db.execute(
            select(Movie.radarr_id)
            .join(ReclaimCandidate, Movie.id == ReclaimCandidate.movie_id)
            .where(ReclaimCandidate.media_type == MediaType.MOVIE)
            .where(Movie.radarr_id.isnot(None))
        )
        candidate_radarr_ids = {row[0] for row in result.all()}

    LOG.debug(f"Found {len(candidate_radarr_ids)} movie candidates with Radarr IDs")

    # determine which movies need tagging/un-tagging
    movies_to_tag = []  # candidates without current tag
    movies_to_untag = []  # non-candidates with current tag
    movies_with_old_tags = {}  # movies with old tags: {old_tag_id: [movie_ids]}

    for radarr_id, movie in movies_by_id.items():
        has_tag = tag.id in movie.tags
        should_have_tag = radarr_id in candidate_radarr_ids

        if should_have_tag and not has_tag:
            movies_to_tag.append(radarr_id)
        elif not should_have_tag and has_tag:
            movies_to_untag.append(radarr_id)

        # check for old cleanup tags that need removal
        for old_tag in old_tags:
            if old_tag.id in movie.tags:
                if old_tag.id not in movies_with_old_tags:
                    movies_with_old_tags[old_tag.id] = []
                movies_with_old_tags[old_tag.id].append(radarr_id)

    LOG.debug(
        f"Need to tag {len(movies_to_tag)} movies, untag {len(movies_to_untag)} movies"
    )

    # clean up old cleanup tags first (migration)
    for old_tag_id, movie_ids in movies_with_old_tags.items():
        old_tag_label = next(
            (t.label for t in old_tags if t.id == old_tag_id), "unknown"
        )
        await service_manager.radarr.remove_tag_from_movies(movie_ids, old_tag_id)
        LOG.info(f"Removed old tag '{old_tag_label}' from {len(movie_ids)} movies")

    # apply current tags in bulk
    if movies_to_tag:
        await service_manager.radarr.add_tag_to_movies(movies_to_tag, tag.id)
        LOG.info(f"Tagged {len(movies_to_tag)} movies in Radarr")

    # remove current tags from non-candidates in bulk
    if movies_to_untag:
        await service_manager.radarr.remove_tag_from_movies(movies_to_untag, tag.id)
        LOG.info(f"Untagged {len(movies_to_untag)} movies in Radarr")

    return len(movies_to_tag), len(movies_to_untag)


async def _sync_sonarr_tags(cleanup_tag: str) -> tuple[int, int]:
    """Sync Sonarr series tags. Returns (tagged_count, untagged_count)."""
    if not service_manager.sonarr:
        return 0, 0

    # get all tags to detect old cleanup tags
    all_tags = await service_manager.sonarr.get_tags()

    # get or create the current cleanup tag
    tag = await service_manager.sonarr.get_or_create_tag(cleanup_tag)
    LOG.debug(f"Using Sonarr tag '{tag.label}' (ID: {tag.id})")

    # find old reclaimerr tags (starts with 'reclaimerr' but isn't current tag)
    old_tags = [
        t for t in all_tags if t.label.startswith("reclaimerr") and t.id != tag.id
    ]
    if old_tags:
        LOG.info(
            f"Found {len(old_tags)} old cleanup tags to migrate: {[t.label for t in old_tags]}"
        )

    # get all series from Sonarr
    all_series = await service_manager.sonarr.get_all_series()
    LOG.debug(f"Found {len(all_series)} series in Sonarr")

    # build lookup: sonarr_id -> series
    series_by_id = {series.id: series for series in all_series}

    # get sonarr IDs for all series candidates from database
    async with async_db() as db:
        result = await db.execute(
            select(Series.sonarr_id)
            .join(ReclaimCandidate, Series.id == ReclaimCandidate.series_id)
            .where(ReclaimCandidate.media_type == MediaType.SERIES)
            .where(Series.sonarr_id.isnot(None))
        )
        candidate_sonarr_ids = {row[0] for row in result.all()}

    LOG.debug(f"Found {len(candidate_sonarr_ids)} series candidates with Sonarr IDs")

    # determine which series need tagging/un-tagging
    series_to_tag = []  # candidates without current tag
    series_to_untag = []  # non-candidates with current tag
    series_with_old_tags = {}  # series with old tags: {old_tag_id: [series_ids]}

    for sonarr_id, series in series_by_id.items():
        has_tag = tag.id in series.tags
        should_have_tag = sonarr_id in candidate_sonarr_ids

        if should_have_tag and not has_tag:
            series_to_tag.append(sonarr_id)
        elif not should_have_tag and has_tag:
            series_to_untag.append(sonarr_id)

        # check for old cleanup tags that need removal
        for old_tag in old_tags:
            if old_tag.id in series.tags:
                if old_tag.id not in series_with_old_tags:
                    series_with_old_tags[old_tag.id] = []
                series_with_old_tags[old_tag.id].append(sonarr_id)

    LOG.debug(
        f"Need to tag {len(series_to_tag)} series, untag {len(series_to_untag)} series"
    )

    # clean up old cleanup tags first (migration)
    for old_tag_id, series_ids in series_with_old_tags.items():
        old_tag_label = next(
            (t.label for t in old_tags if t.id == old_tag_id), "unknown"
        )
        await service_manager.sonarr.remove_tag_from_series(series_ids, old_tag_id)
        LOG.info(f"Removed old tag '{old_tag_label}' from {len(series_ids)} series")

    # apply current tags in bulk
    if series_to_tag:
        await service_manager.sonarr.add_tag_to_series(series_to_tag, tag.id)
        LOG.info(f"Tagged {len(series_to_tag)} series in Sonarr")

    # remove current tags from non-candidates in bulk
    if series_to_untag:
        await service_manager.sonarr.remove_tag_from_series(series_to_untag, tag.id)
        LOG.info(f"Untagged {len(series_to_untag)} series in Sonarr")

    return len(series_to_tag), len(series_to_untag)


async def delete_cleanup_candidates() -> None:
    """Delete all cleanup candidates from their respective services.

    Deletion is based purely on ReclaimCandidate records in the database.
    Tags are optional visual indicators only and do not affect deletion.

    Deletion priority:
    1. Use Radarr (movies) or Sonarr (series) if item has radarr_id/sonarr_id
    2. Fall back to Jellyfin if configured
    3. Fall back to Plex if configured

    After deletion, resets the request in Seerr and marks item as removed in database.
    """
    LOG.info("Starting cleanup candidate deletion")

    async with track_task_execution(Task.DELETE_CLEANUP_CANDIDATES):
        try:
            movies_deleted = 0
            series_deleted = 0
            season_deleted = 0

            # process movies
            if (
                service_manager.radarr
                or service_manager.jellyfin
                or service_manager.plex
            ):
                movies_deleted = await _delete_movie_candidates()

            # process series
            if (
                service_manager.sonarr
                or service_manager.jellyfin
                or service_manager.plex
            ):
                series_deleted = await _delete_series_candidates()
                season_deleted = await _delete_season_candidates()

            LOG.info(
                f"Deletion completed: {movies_deleted} movies, "
                f"{series_deleted} whole series, {season_deleted} season(s) removed"
            )

        except Exception as e:
            LOG.error(f"Error deleting cleanup candidates: {e}", exc_info=True)
            raise


async def _delete_movie_candidates(
    restrict_to_ids: frozenset[int] | None = None,
) -> int:
    """Delete movie candidates. Returns count of deleted movies."""
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

        # get radarr IDs and build lookup
        result = await db.execute(
            select(Movie.id, Movie.radarr_id, Movie.title, Movie.tmdb_id)
            .join(ReclaimCandidate, Movie.id == ReclaimCandidate.movie_id)
            .where(ReclaimCandidate.media_type == MediaType.MOVIE)
            .where(Movie.removed_at.is_(None))
        )
        movie_data = {
            row[0]: {"radarr_id": row[1], "title": row[2], "tmdb_id": row[3]}
            for row in result.all()
        }

    # delete all candidates that exist in Radarr (by radarr_id)
    movies_to_delete = []
    if service_manager.radarr:
        for candidate in candidates:
            movie_info = movie_data.get(candidate.movie_id)
            if movie_info and movie_info["radarr_id"]:
                movies_to_delete.append(
                    {
                        "candidate_id": candidate.id,
                        "movie_id": candidate.movie_id,
                        "radarr_id": movie_info["radarr_id"],
                        "title": movie_info["title"],
                        "method": "radarr",
                        "tmdb_id": movie_info["tmdb_id"],
                    }
                )

    # delete using radarr
    if movies_to_delete and service_manager.radarr:
        LOG.info(f"Deleting {len(movies_to_delete)} movies via Radarr")
        radarr_ids = [m["radarr_id"] for m in movies_to_delete]

        try:
            await service_manager.radarr.delete_movies(radarr_ids)

            # mark as removed in database and delete candidate
            async with async_db() as db:
                for movie_info in movies_to_delete:
                    # update movie record
                    result = await db.execute(
                        select(Movie).where(Movie.id == movie_info["movie_id"])
                    )
                    movie = result.scalar_one_or_none()
                    if movie:
                        movie.removed_at = datetime.now(timezone.utc)

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

                await db.commit()

            deleted_count = len(movies_to_delete)
            LOG.info(f"Successfully deleted {deleted_count} movies via Radarr")

        except Exception as e:
            LOG.error(f"Error deleting movies via Radarr: {e}", exc_info=True)

    # fallback to Jellyfin/Plex for candidates not in Radarr
    if not movies_to_delete or (len(candidates) > len(movies_to_delete)):
        deleted_count += await _delete_movies_via_media_server(
            candidates, movies_to_delete
        )

    return deleted_count


async def _delete_series_candidates(
    restrict_to_ids: frozenset[int] | None = None,
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

        # get sonarr IDs and build lookup
        result = await db.execute(
            select(Series.id, Series.sonarr_id, Series.title, Series.tmdb_id)
            .join(ReclaimCandidate, Series.id == ReclaimCandidate.series_id)
            .where(ReclaimCandidate.media_type == MediaType.SERIES)
            .where(Series.removed_at.is_(None))
        )
        series_data = {
            row[0]: {"sonarr_id": row[1], "title": row[2], "tmdb_id": row[3]}
            for row in result.all()
        }

    # delete all candidates that exist in Sonarr (by sonarr_id)
    series_to_delete = []
    if service_manager.sonarr:
        for candidate in candidates:
            series_info = series_data.get(candidate.series_id)
            if series_info and series_info["sonarr_id"]:
                series_to_delete.append(
                    {
                        "candidate_id": candidate.id,
                        "series_id": candidate.series_id,
                        "sonarr_id": series_info["sonarr_id"],
                        "title": series_info["title"],
                        "method": "sonarr",
                        "tmdb_id": series_info["tmdb_id"],
                    }
                )

    # delete using Sonarr
    if series_to_delete and service_manager.sonarr:
        LOG.info(f"Deleting {len(series_to_delete)} series via Sonarr")

        try:
            # Sonarr deletes one at a time (no bulk endpoint)
            for series_info in series_to_delete:
                await service_manager.sonarr.delete_series(
                    series_info["sonarr_id"], delete_files=True
                )

            # mark as removed in database and delete candidate
            async with async_db() as db:
                for series_info in series_to_delete:
                    # update series record
                    result = await db.execute(
                        select(Series).where(Series.id == series_info["series_id"])
                    )
                    series = result.scalar_one_or_none()
                    if series:
                        series.removed_at = datetime.now(timezone.utc)

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

                await db.commit()

            deleted_count = len(series_to_delete)
            LOG.info(f"Successfully deleted {deleted_count} series via Sonarr")

        except Exception as e:
            LOG.error(f"Error deleting series via Sonarr: {e}", exc_info=True)

    # fallback to Jellyfin/Plex for candidates not in Sonarr
    if not series_to_delete or (len(candidates) > len(series_to_delete)):
        deleted_count += await _delete_series_via_media_server(
            candidates, series_to_delete
        )

    return deleted_count


async def _delete_season_candidates(
    restrict_to_ids: frozenset[int] | None = None,
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
        if service_manager.sonarr and series_obj.sonarr_id:
            try:
                await service_manager.sonarr.delete_season_files(
                    series_obj.sonarr_id, season_number
                )
                deleted_via_sonarr = True
                LOG.info(
                    f"Deleted '{series_obj.title}' S{season_number:02d} "
                    f"via Sonarr (sonarr_id={series_obj.sonarr_id})"
                )
            except Exception as e:
                LOG.warning(
                    f"Sonarr deletion failed for '{series_obj.title}' "
                    f"S{season_number:02d}: {e} - trying media server fallback"
                )

        # fall back to media server if Sonarr failed or unavailable
        if not deleted_via_sonarr:
            media_service = service_manager.jellyfin or service_manager.plex
            season_service_id = (
                season.jellyfin_season_id
                if service_manager.jellyfin
                else season.plex_season_rating_key
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
                await db.delete(season_db)

            await db.commit()

        deleted_count += 1

    return deleted_count


async def _delete_movies_via_media_server(
    candidates, already_deleted: list[dict]
) -> int:
    """Deletes movies via the main media server as fallback when not in Radarr.

    Uses whichever Plex/Jellyfin server is designated as main.  If no main is
    set, falls back to whichever of Jellyfin/Plex is initialized (Jellyfin
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

    # determine main service type
    async with async_db() as db:
        main_result = await db.execute(
            select(ServiceConfig).where(
                ServiceConfig.service_type.in_(MEDIA_SERVERS),
                ServiceConfig.is_main.is_(True),
                ServiceConfig.enabled.is_(True),
            )
        )
        main_cfg = main_result.scalar_one_or_none()

    if main_cfg:
        main_service_type = main_cfg.service_type
        main_service = (
            service_manager.jellyfin
            if main_service_type == Service.JELLYFIN
            else service_manager.plex
        )
    elif service_manager.jellyfin:
        main_service_type = Service.JELLYFIN
        main_service = service_manager.jellyfin
    elif service_manager.plex:
        main_service_type = Service.PLEX
        main_service = service_manager.plex
    else:
        LOG.warning("No media server available for movie deletion fallback")
        return 0

    if not main_service:
        LOG.warning(f"Main media server {main_service_type} is not initialized")
        return 0

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
    deleted_paths: list[str] = []

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
                if ver.path:
                    deleted_paths.append(ver.path)

            # mark as removed and delete candidate
            async with async_db() as db:
                result = await db.execute(
                    select(Movie).where(Movie.id == candidate.movie_id)
                )
                movie_obj = result.scalar_one_or_none()
                if movie_obj:
                    movie_obj.removed_at = datetime.now(timezone.utc)

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

                await db.commit()

            deleted_count += 1
            LOG.info(f"Deleted movie '{movie.title}' via {main_service_type}")

        except Exception as e:
            LOG.error(
                f"Failed to delete movie '{movie.title}' via {main_service_type}: {e}"
            )

    # trigger library scans for deleted paths
    if deleted_paths:
        LOG.info(
            f"Triggering {main_service_type} scans for {len(deleted_paths)} deleted paths"
        )
        for path in deleted_paths:
            try:
                await main_service.scan_item_path(path)
            except Exception as e:
                LOG.warning(f"Failed to trigger scan for {path}: {e}")

    return deleted_count


async def _delete_series_via_media_server(
    candidates, already_deleted: list[dict]
) -> int:
    # TODO: this still needs more testing!
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
        f"Attempting to delete {len(remaining_candidates)} series via media servers (not in Sonarr)"
    )
    deleted_count = 0

    # get series from database to access service IDs
    async with async_db() as db:
        series_ids = [c.series_id for c in remaining_candidates]
        result = await db.execute(
            select(Series)
            .where(Series.id.in_(series_ids))
            .options(selectinload(Series.service_refs))
        )
        series = {s.id: s for s in result.scalars().all()}

    # prioritize Jellyfin over Plex
    if service_manager.jellyfin:
        deleted_paths = []

        for candidate in remaining_candidates:
            series_obj = series.get(candidate.series_id)
            if not series_obj or not series_obj.tmdb_id:
                continue

            ref = next(
                (r for r in series_obj.service_refs if r.service == Service.JELLYFIN),
                None,
            )
            if not ref:
                LOG.debug(
                    f"Series '{series_obj.title}' not found in Jellyfin (no service ref)"
                )
                continue

            try:
                await service_manager.jellyfin.delete_item(ref.service_id)

                # track service-specific path for scanning (from database)
                if ref.path:
                    deleted_paths.append(ref.path)

                # mark as removed and delete candidate
                async with async_db() as db:
                    result = await db.execute(
                        select(Series).where(Series.id == candidate.series_id)
                    )
                    series_db = result.scalar_one_or_none()
                    if series_db:
                        series_db.removed_at = datetime.now(timezone.utc)

                    result = await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.id == candidate.id
                        )
                    )
                    cand = result.scalar_one_or_none()
                    if cand:
                        await db.delete(cand)

                    # reset seerr request
                    if service_manager.seerr and series_obj.tmdb_id:
                        try:
                            await _reset_seerr_request(
                                series_obj.tmdb_id, MediaType.SERIES
                            )
                        except Exception as e:
                            LOG.warning(
                                f"Failed to reset Seerr request for {series_obj.title}: {e}"
                            )

                    await db.commit()

                deleted_count += 1
                LOG.info(f"Deleted series '{series_obj.title}' via Jellyfin")

            except Exception as e:
                LOG.error(
                    f"Failed to delete series '{series_obj.title}' via Jellyfin: {e}"
                )

        # trigger path-specific library scans after all deletions
        if deleted_paths:
            LOG.info(
                f"Triggering Jellyfin scans for {len(deleted_paths)} deleted series paths"
            )
            for path in deleted_paths:
                try:
                    await service_manager.jellyfin.scan_item_path(path)
                except Exception as e:
                    LOG.warning(f"Failed to trigger Jellyfin scan for {path}: {e}")

    elif service_manager.plex:
        deleted_paths = []

        for candidate in remaining_candidates:
            series_obj = series.get(candidate.series_id)
            if not series_obj or not series_obj.tmdb_id:
                continue

            ref = next(
                (r for r in series_obj.service_refs if r.service == Service.PLEX),
                None,
            )
            if not ref:
                LOG.debug(
                    f"Series '{series_obj.title}' not found in Plex (no service ref)"
                )
                continue

            try:
                await service_manager.plex.delete_item(ref.service_id)

                # track service-specific path for scanning (from database)
                if ref.path:
                    deleted_paths.append(ref.path)

                # mark as removed and delete candidate
                async with async_db() as db:
                    result = await db.execute(
                        select(Series).where(Series.id == candidate.series_id)
                    )
                    series_db = result.scalar_one_or_none()
                    if series_db:
                        series_db.removed_at = datetime.now(timezone.utc)

                    result = await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.id == candidate.id
                        )
                    )
                    cand = result.scalar_one_or_none()
                    if cand:
                        await db.delete(cand)

                    # reset seerr request
                    if service_manager.seerr and series_obj.tmdb_id:
                        try:
                            await _reset_seerr_request(
                                series_obj.tmdb_id, MediaType.SERIES
                            )
                        except Exception as e:
                            LOG.warning(
                                f"Failed to reset Seerr request for {series_obj.title}: {e}"
                            )

                    await db.commit()

                deleted_count += 1
                LOG.info(f"Deleted series '{series_obj.title}' via Plex")

            except Exception as e:
                LOG.error(f"Failed to delete series '{series_obj.title}' via Plex: {e}")

        # trigger path-specific library scans after all deletions
        if deleted_paths:
            LOG.info(
                f"Triggering Plex scans for {len(deleted_paths)} deleted series paths"
            )
            for path in deleted_paths:
                try:
                    await service_manager.plex.scan_item_path(path)
                except Exception as e:
                    LOG.warning(f"Failed to trigger Plex scan for {path}: {e}")

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
    except Exception as e:
        LOG.warning(f"Failed to delete Seerr data for TMDB {tmdb_id}: {e}")


async def delete_specific_candidates(candidate_ids: list[int]) -> tuple[int, int]:
    """Deletes specific reclaim candidates by their IDs.

    Uses the same deletion priority as delete_cleanup_candidates:
    Radarr/Sonarr first, then Jellyfin/Plex (main server first) fallback.

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
        service_manager.radarr or service_manager.jellyfin or service_manager.plex
    ):
        deleted += await _delete_movie_candidates(restrict_to_ids=restrict)

    if MediaType.SERIES in types and (
        service_manager.sonarr or service_manager.jellyfin or service_manager.plex
    ):
        deleted += await _delete_series_candidates(restrict_to_ids=restrict)
        deleted += await _delete_season_candidates(restrict_to_ids=restrict)

    failed = max(0, len(found_ids) - deleted)
    LOG.info(f"Manual deletion complete: {deleted} deleted, {failed} failed")
    return deleted, failed
