from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.settings import settings
from backend.database.database import async_db, get_db
from backend.database.models import CleanupCandidate, CleanupRule, Movie, Series
from backend.enums import MediaType


async def scan_cleanup_candidates() -> None:
    """Scan media libraries and identify cleanup candidates based on configured rules."""
    LOG.info("Starting cleanup candidates scan")

    try:
        async for db in get_db():
            await _scan_with_db(db)
            break

    except Exception as e:
        LOG.error(f"Error scanning cleanup candidates: {e}", exc_info=True)
        raise


async def _scan_with_db(db: AsyncSession) -> None:
    """Internal method to perform scan with database session."""
    try:
        # load all enabled cleanup rules
        result = await db.execute(
            select(CleanupRule).where(CleanupRule.enabled == True)
        )
        rules = result.scalars().all()

        if not rules:
            LOG.info("No enabled cleanup rules found")
            return

        LOG.info(f"Found {len(rules)} enabled cleanup rules")

        # separate rules by media type
        movie_rules = [r for r in rules if r.media_type == MediaType.MOVIE]
        series_rules = [r for r in rules if r.media_type == MediaType.SERIES]

        candidates_created = 0
        candidates_updated = 0

        # process movies
        if movie_rules:
            created, updated = await _process_media(db, movie_rules, MediaType.MOVIE)
            candidates_created += created
            candidates_updated += updated

        # process series
        if series_rules:
            created, updated = await _process_media(db, series_rules, MediaType.SERIES)
            candidates_created += created
            candidates_updated += updated

        LOG.info(
            f"Cleanup scan completed: {candidates_created} new candidates, "
            f"{candidates_updated} updated"
        )
    except Exception:
        raise


async def _process_media(
    db: AsyncSession, rules: list[CleanupRule], media_type: MediaType
) -> tuple[int, int]:
    """
    Process movies or series against cleanup rules.

    Returns (created_count, updated_count)
    """
    # get all media items
    if media_type == MediaType.MOVIE:
        result = await db.execute(select(Movie).where(Movie.removed_at.is_(None)))
        media_items = result.scalars().all()
        id_field = "movie_id"
    else:
        result = await db.execute(select(Series).where(Series.removed_at.is_(None)))
        media_items = result.scalars().all()
        id_field = "series_id"

    LOG.info(f"Processing {len(media_items)} {media_type.value} items")

    candidates_created = 0
    candidates_updated = 0

    for item in media_items:
        # evaluate all rules against this item
        matched_rules = []
        matched_criteria = {}
        reasons = []

        for rule in rules:
            if _evaluate_rule(item, rule, matched_criteria, reasons):
                matched_rules.append(rule.id)

        # if item matches at least one rule, create/update candidate
        if matched_rules:
            # check if candidate already exists
            result = await db.execute(
                select(CleanupCandidate).where(CleanupCandidate.id == item.id)
            )
            existing = result.scalar_one_or_none()

            combined_reason = "; ".join(reasons)

            # calculate space savings (convert bytes to GB)
            space_gb = item.size / (1024**3) if item.size else None

            if existing:
                # update existing candidate
                existing.matched_rule_ids = matched_rules
                existing.matched_criteria = matched_criteria
                existing.reason = combined_reason
                existing.estimated_space_gb = space_gb
                existing.library_name = item.library_name
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
                    "library_name": item.library_name,
                }
                if id_field == "movie_id":
                    candidate_data["movie_id"] = item.id
                else:
                    candidate_data["series_id"] = item.id

                candidate = CleanupCandidate(**candidate_data)
                db.add(candidate)
                candidates_created += 1

    # add deletion candidates to db
    if candidates_created > 0 or candidates_updated > 0:
        await db.commit()

    return candidates_created, candidates_updated


def _evaluate_rule(
    item: Movie | Series, rule: CleanupRule, matched_criteria: dict, reasons: list[str]
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
            rule.library_name is not None,
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

    # check library filtering
    if rule.library_name is not None and (item.library_name != rule.library_name):
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

    if not settings.auto_tag_enabled:
        LOG.debug("Auto-tagging disabled in settings, skipping")
        return

    if not service_manager.radarr and not service_manager.sonarr:
        LOG.debug("Neither Radarr nor Sonarr configured, skipping tag sync")
        return

    LOG.info(f"Starting cleanup candidate tagging (tag: {settings.cleanup_tag})")

    try:
        movies_tagged = 0
        movies_untagged = 0
        series_tagged = 0
        series_untagged = 0

        # process Radarr movies
        if service_manager.radarr:
            tagged, untagged = await _sync_radarr_tags()
            movies_tagged = tagged
            movies_untagged = untagged

        # process Sonarr series
        if service_manager.sonarr:
            tagged, untagged = await _sync_sonarr_tags()
            series_tagged = tagged
            series_untagged = untagged

        LOG.info(
            f"Tag sync completed: Movies ({movies_tagged} tagged, {movies_untagged} untagged), "
            f"Series ({series_tagged} tagged, {series_untagged} untagged)"
        )

    except Exception as e:
        LOG.error(f"Error syncing cleanup tags: {e}", exc_info=True)
        raise


async def _sync_radarr_tags() -> tuple[int, int]:
    """Sync Radarr movie tags. Returns (tagged_count, untagged_count)."""
    if not service_manager.radarr:
        return 0, 0

    # get or create the cleanup tag
    tag = await service_manager.radarr.get_or_create_tag(settings.cleanup_tag)
    LOG.debug(f"Using Radarr tag '{tag.label}' (ID: {tag.id})")

    # get all movies from Radarr
    all_movies = await service_manager.radarr.get_all_movies()
    LOG.debug(f"Found {len(all_movies)} movies in Radarr")

    # build lookup: radarr_id -> movie
    movies_by_id = {movie.id: movie for movie in all_movies}

    # get radarr IDs for all movie candidates from database
    async with async_db() as db:
        result = await db.execute(
            select(Movie.radarr_id)
            .join(CleanupCandidate, Movie.id == CleanupCandidate.movie_id)
            .where(CleanupCandidate.media_type == MediaType.MOVIE)
            .where(Movie.radarr_id.isnot(None))
        )
        candidate_radarr_ids = {row[0] for row in result.all()}

    LOG.debug(f"Found {len(candidate_radarr_ids)} movie candidates with Radarr IDs")

    # determine which movies need tagging/un-tagging
    movies_to_tag = []  # candidates without tag
    movies_to_untag = []  # non-candidates with tag

    for radarr_id, movie in movies_by_id.items():
        has_tag = tag.id in movie.tags
        should_have_tag = radarr_id in candidate_radarr_ids

        if should_have_tag and not has_tag:
            movies_to_tag.append(radarr_id)
        elif not should_have_tag and has_tag:
            movies_to_untag.append(radarr_id)

    LOG.debug(
        f"Need to tag {len(movies_to_tag)} movies, untag {len(movies_to_untag)} movies"
    )

    # apply tags in bulk
    if movies_to_tag:
        await service_manager.radarr.add_tag_to_movies(movies_to_tag, tag.id)
        LOG.info(f"Tagged {len(movies_to_tag)} movies in Radarr")

    # remove tags in bulk
    if movies_to_untag:
        await service_manager.radarr.remove_tag_from_movies(movies_to_untag, tag.id)
        LOG.info(f"Untagged {len(movies_to_untag)} movies in Radarr")

    return len(movies_to_tag), len(movies_to_untag)


async def _sync_sonarr_tags() -> tuple[int, int]:
    """Sync Sonarr series tags. Returns (tagged_count, untagged_count)."""
    if not service_manager.sonarr:
        return 0, 0

    # get or create the cleanup tag
    tag = await service_manager.sonarr.get_or_create_tag(settings.cleanup_tag)
    LOG.debug(f"Using Sonarr tag '{tag.label}' (ID: {tag.id})")

    # get all series from Sonarr
    all_series = await service_manager.sonarr.get_all_series()
    LOG.debug(f"Found {len(all_series)} series in Sonarr")

    # build lookup: sonarr_id -> series
    series_by_id = {series.id: series for series in all_series}

    # get sonarr IDs for all series candidates from database
    async with async_db() as db:
        result = await db.execute(
            select(Series.sonarr_id)
            .join(CleanupCandidate, Series.id == CleanupCandidate.series_id)
            .where(CleanupCandidate.media_type == MediaType.SERIES)
            .where(Series.sonarr_id.isnot(None))
        )
        candidate_sonarr_ids = {row[0] for row in result.all()}

    LOG.debug(f"Found {len(candidate_sonarr_ids)} series candidates with Sonarr IDs")

    # determine which series need tagging/un-tagging
    series_to_tag = []  # candidates without tag
    series_to_untag = []  # non-candidates with tag

    for sonarr_id, series in series_by_id.items():
        has_tag = tag.id in series.tags
        should_have_tag = sonarr_id in candidate_sonarr_ids

        if should_have_tag and not has_tag:
            series_to_tag.append(sonarr_id)
        elif not should_have_tag and has_tag:
            series_to_untag.append(sonarr_id)

    LOG.debug(
        f"Need to tag {len(series_to_tag)} series, untag {len(series_to_untag)} series"
    )

    # apply tags in bulk
    if series_to_tag:
        await service_manager.sonarr.add_tag_to_series(series_to_tag, tag.id)
        LOG.info(f"Tagged {len(series_to_tag)} series in Sonarr")

    # remove tags in bulk
    if series_to_untag:
        await service_manager.sonarr.remove_tag_from_series(series_to_untag, tag.id)
        LOG.info(f"Untagged {len(series_to_untag)} series in Sonarr")

    return len(series_to_tag), len(series_to_untag)
