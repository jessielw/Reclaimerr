"""Warn Seerr requesters before their titles are deleted automatically.

After each cleanup scan, a candidate whose automatic deletion falls inside the
warning window gets its requesters looked up in Seerr. Each requester is
matched to a Reclaimerr account through an exact media-server identity (Plex
or Jellyfin/Emby account, or email); display names are never matched. Every
matched account gets one notification per scan listing its titles, and each
candidate is warned only once.

Only whole movies, whole series, and seasons warn. Losing one version or one
episode doesn't take away what someone asked for.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auto_delete import resolve_auto_delete_policy
from backend.core.logger import LOG
from backend.database.models import (
    GeneralSettings,
    MediaUserIdentity,
    Movie,
    ReclaimCandidate,
    Season,
    Series,
    TaskSchedule,
    User,
)
from backend.enums import MediaType, NotificationType, Service, Task
from backend.models.services.seerr import SeerrUser
from backend.services.media_auth import normalize_email, normalize_username
from backend.services.notifications import notify_user
from backend.services.reclaim_stats import load_auto_delete_inputs
from backend.services.seerr_cache import SeerrRequestSnapshot, seerr_snapshot_cache


@dataclass(frozen=True, slots=True)
class LeavingTitle:
    candidate_id: int
    label: str
    media_type: MediaType
    scope: str
    deletes_at: datetime


@dataclass(slots=True)
class WarningResult:
    warned_candidates: int = 0
    notified_users: int = 0
    # requesters with no linked Reclaimerr account, or more than one
    unmatched_requesters: int = 0


def _jellyfin_id(value: object) -> str:
    # Seerr stores Jellyfin user GUIDs without dashes
    return str(value or "").strip().lower().replace("-", "")


class _AccountIndex:
    """Linked media-server identities and account emails, by exact key."""

    def __init__(self, identities: list[MediaUserIdentity], users: list[User]) -> None:
        self.plex_ids: dict[str, set[int]] = defaultdict(set)
        self.plex_names: dict[str, set[int]] = defaultdict(set)
        self.jellyfin_ids: dict[str, set[int]] = defaultdict(set)
        self.jellyfin_names: dict[str, set[int]] = defaultdict(set)
        self.emails: dict[str, set[int]] = defaultdict(set)
        for identity in identities:
            if identity.user_id is None:
                continue
            if identity.source_service is Service.PLEX:
                self.plex_ids[str(identity.source_user_id).strip()].add(
                    identity.user_id
                )
                self.plex_names[identity.username_normalized].add(identity.user_id)
            elif identity.source_service in (Service.JELLYFIN, Service.EMBY):
                self.jellyfin_ids[_jellyfin_id(identity.source_user_id)].add(
                    identity.user_id
                )
                self.jellyfin_names[identity.username_normalized].add(identity.user_id)
            if email := normalize_email(identity.email):
                self.emails[email].add(identity.user_id)
        for user in users:
            if email := normalize_email(user.email):
                self.emails[email].add(user.id)

    def match(self, requester: SeerrUser) -> set[int]:
        found: set[int] = set()
        if requester.plex_id is not None:
            found |= self.plex_ids.get(str(requester.plex_id), set())
        if name := normalize_username(requester.plex_username):
            found |= self.plex_names.get(name, set())
        if guid := _jellyfin_id(requester.jellyfin_user_id):
            found |= self.jellyfin_ids.get(guid, set())
        if name := normalize_username(requester.jellyfin_username):
            found |= self.jellyfin_names.get(name, set())
        if email := normalize_email(requester.email):
            found |= self.emails.get(email, set())
        return found


def _requester_keys(
    snapshot: SeerrRequestSnapshot,
    media_type: MediaType,
    tmdb_id: int,
    season_number: int | None,
) -> set[str]:
    # the same requesters the Candidates page shows for this row
    if season_number is not None:
        keys = snapshot.requester_ids_by_series_season.get((tmdb_id, season_number))
        if keys:
            return set(keys)
    return set(snapshot.requester_ids_by_key.get((media_type, tmdb_id), set()))


async def _describe(
    db: AsyncSession, candidate: ReclaimCandidate
) -> tuple[int, str, str, int | None] | None:
    """(tmdb_id, label, scope, season_number), or None for scopes that don't warn."""
    if candidate.media_type is MediaType.MOVIE:
        if candidate.movie_version_id is not None or candidate.movie_id is None:
            return None
        movie = await db.get(Movie, candidate.movie_id)
        if movie is None:
            return None
        label = f"{movie.title} ({movie.year})" if movie.year else movie.title
        return movie.tmdb_id, label, "Movie", None
    if candidate.episode_id is not None or candidate.series_id is None:
        return None
    series = await db.get(Series, candidate.series_id)
    if series is None or series.tmdb_id is None:
        return None
    label = f"{series.title} ({series.year})" if series.year else series.title
    if candidate.season_id is None:
        return series.tmdb_id, label, "Series", None
    season = await db.get(Season, candidate.season_id)
    if season is None:
        return None
    return (
        series.tmdb_id,
        f"{label} Season {season.season_number}",
        "Season",
        season.season_number,
    )


def _when(deletes_at: datetime, now: datetime) -> str:
    if deletes_at <= now:
        return "at the next cleanup run"
    return f"on {deletes_at:%B} {deletes_at.day}, {deletes_at.year}"


async def warn_requesters_before_deletion(
    db: AsyncSession, now: datetime | None = None
) -> WarningResult:
    """Send leaving-soon warnings for candidates inside the warning window."""
    result = WarningResult()
    now = now or datetime.now(UTC)
    settings_row = (await db.execute(select(GeneralSettings))).scalars().first()
    window_days = settings_row.requester_warning_days if settings_row else 7
    if window_days <= 0:
        return result
    # nothing is deleted automatically while the deletion task is off
    if not await db.scalar(
        select(TaskSchedule.enabled).where(
            TaskSchedule.task == Task.DELETE_CLEANUP_CANDIDATES
        )
    ):
        return result

    candidates = (
        (
            await db.execute(
                select(ReclaimCandidate).where(
                    ReclaimCandidate.requester_warned_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        return result
    inputs = await load_auto_delete_inputs(
        db,
        (rule_id for c in candidates for rule_id in (c.matched_rule_ids or [])),
    )
    window_end = now + timedelta(days=window_days)
    due: list[ReclaimCandidate] = []
    deadlines: dict[int, datetime] = {}
    for candidate in candidates:
        policy = resolve_auto_delete_policy(
            media_type=cast(MediaType, candidate.media_type),
            matched_rule_ids=cast(list[int], candidate.matched_rule_ids or []),
            created_at=cast(datetime, candidate.created_at),
            timer_started_at=candidate.auto_delete_timer_started_at,
            postponed_until=candidate.auto_delete_postponed_until,
            cancelled_at=candidate.auto_delete_cancelled_at,
            rule_actions_by_id=inputs.rule_actions_by_id,
            movie_delay_days=inputs.movie_delay_days,
            series_delay_days=inputs.series_delay_days,
            now=now,
        )
        if policy.is_enabled and policy.eligible_at <= window_end:
            due.append(candidate)
            deadlines[candidate.id] = policy.eligible_at
    if not due:
        return result

    snapshot, error = await seerr_snapshot_cache.get_request_snapshot(
        require_fresh=False, allow_stale_on_failure=True
    )
    if snapshot is None:
        # not marked, so the next scan tries again
        LOG.info(f"Requester warnings skipped: {error or 'no Seerr data'}")
        return result

    accounts = _AccountIndex(
        list((await db.execute(select(MediaUserIdentity))).scalars().all()),
        list((await db.execute(select(User))).scalars().all()),
    )
    titles_by_user: dict[int, list[LeavingTitle]] = defaultdict(list)
    unmatched: set[str] = set()
    for candidate in due:
        candidate.requester_warned_at = now
        described = await _describe(db, candidate)
        if described is None:
            continue
        tmdb_id, label, scope, season_number = described
        title = LeavingTitle(
            candidate_id=candidate.id,
            label=label,
            media_type=cast(MediaType, candidate.media_type),
            scope=scope,
            deletes_at=deadlines[candidate.id],
        )
        warned = False
        for key in _requester_keys(
            snapshot, cast(MediaType, candidate.media_type), tmdb_id, season_number
        ):
            requester = snapshot.requester_users_by_id.get(key)
            user_ids = accounts.match(requester) if requester else set()
            if len(user_ids) != 1:
                unmatched.add(key)
                continue
            titles_by_user[next(iter(user_ids))].append(title)
            warned = True
        result.warned_candidates += int(warned)
    await db.commit()

    for user_id, titles in titles_by_user.items():
        await _notify(user_id, titles, now)
    result.notified_users = len(titles_by_user)
    result.unmatched_requesters = len(unmatched)
    if result.warned_candidates or unmatched:
        LOG.info(
            f"Requester warnings: {result.warned_candidates} titles, "
            f"{result.notified_users} users notified"
            + (
                f"; {len(unmatched)} requesters could not be notified "
                "(no linked account, or more than one)"
                if unmatched
                else ""
            )
        )
    return result


async def _notify(user_id: int, titles: list[LeavingTitle], now: datetime) -> None:
    titles = sorted(titles, key=lambda t: t.deletes_at)
    if len(titles) == 1:
        only = titles[0]
        message = (
            f"{only.label} will be deleted {_when(only.deletes_at, now)}. "
            "Request protection if you still want it."
        )
    else:
        message = (
            f"{len(titles)} titles you requested will be deleted soon. "
            "Request protection for any you still want."
        )
    try:
        await notify_user(
            user_id,
            NotificationType.REQUESTER_LEAVING_SOON,
            "Leaving soon",
            message,
            context={
                "media_title": titles[0].label if len(titles) == 1 else None,
                "titles": [
                    {
                        "label": t.label,
                        "scope": t.scope,
                        "when": _when(t.deletes_at, now),
                    }
                    for t in titles
                ],
            },
        )
    except Exception as exc:
        LOG.warning(f"Could not send leaving-soon warning to user {user_id}: {exc}")
