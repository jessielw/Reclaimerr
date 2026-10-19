"""Rule snapshots from the selected durable providers, without native state."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    PlaybackHistoryEvent,
    Season,
    Series,
)
from backend.enums import MediaType, Service
from backend.services.media_identity import load_media_identity_ownership
from backend.services.playback_history import (
    PLAYBACK_TARGET_SCOPES,
    PlaybackRefreshResult,
    PlaybackRuleSnapshot,
    PlaybackTargetKey,
    _Aggregate,
    _identity_maps,
)
from backend.services.playback_selection import load_history_sources


def history_values(
    events: list[PlaybackHistoryEvent], *, completion_supported: bool
) -> dict[str, object]:
    """Unknown completion flags must not turn into evidence of no completion."""
    aggregate = _Aggregate(media_type=MediaType.MOVIE)
    for event in events:
        aggregate.add(event)
    now = datetime.now(UTC).replace(tzinfo=None)
    values: dict[str, object] = {
        "has_activity": bool(events),
        "play_count": aggregate.play_count,
        "last_activity_at": aggregate.last_activity_at,
        "days_since_last_activity": (now - aggregate.last_activity_at).days
        if aggregate.last_activity_at
        else None,
        "total_duration_minutes": aggregate.total_duration_seconds / 60,
        "longest_duration_minutes": aggregate.longest_duration_seconds / 60,
        "unique_user_count": len(aggregate.users),
    }
    if aggregate.usernames_complete:
        values["usernames"] = sorted(aggregate.usernames.values(), key=str.casefold)
    completed = [event.played_at for event in events if event.completed is True]
    if completed:
        values["has_completed_watch"] = True
    if completion_supported:
        unknown = [event.played_at for event in events if event.completed is None]
        last = max(completed, default=None)
        if last is not None or not unknown:
            values["has_completed_watch"] = last is not None
        if not unknown or (last is not None and max(unknown) <= last):
            values["last_completed_at"] = last
            values["days_since_last_completed"] = (now - last).days if last else None
    return {f"playback_history.{key}": value for key, value in values.items()}


async def load_selected_history_snapshot(
    db: AsyncSession,
    refresh: PlaybackRefreshResult,
    requested_fields: set[str] | None = None,
) -> PlaybackRuleSnapshot:
    completion_requested = requested_fields is None or bool(
        requested_fields
        & {
            "playback_history.has_completed_watch",
            "playback_history.last_completed_at",
            "playback_history.days_since_last_completed",
        }
    )
    sources = await load_history_sources(db)
    ownership = await load_media_identity_ownership(db)
    versions: dict[int, list[int]] = defaultdict(list)
    targets: dict[str, set[int]] = {scope: set() for scope in PLAYBACK_TARGET_SCOPES}
    for version_id, movie_id in (
        await db.execute(
            select(MovieVersion.id, MovieVersion.movie_id)
            .join(Movie)
            .where(Movie.removed_at.is_(None))
        )
    ).all():
        versions[movie_id].append(version_id)
        targets["movie_version"].add(version_id)
    for scope, model in (("series", Series), ("season", Season), ("episode", Episode)):
        query = select(model.id)
        if scope == "episode":
            query = query.join(Season).join(Series)
        elif scope == "season":
            query = query.join(Series)
        query = query.where(Series.removed_at.is_(None))
        targets[scope].update((await db.scalars(query)).all())

    coverage: dict[PlaybackTargetKey, set[int]] = defaultdict(set)
    source_maps = {}
    for server_id, source in sources.items():
        movies, episodes = await _identity_maps(
            db, source.server_service, server_id, ownership=ownership
        )
        movie_targets = {
            item: [
                ("movie_version", version) for version in versions.get(identity[0], [])
            ]
            for item, identity in movies.items()
        }
        episode_targets = {
            item: [
                key
                for identity in identities
                for key in (
                    ("episode", identity[0]),
                    ("season", identity[1]),
                    ("series", identity[2]),
                )
            ]
            for item, identities in episodes.items()
        }
        # Provider item ids may change after a library rebuild. Retained stable
        # identities may resolve only against titles this same server contains.
        stable_movies = {
            identity[1]: movie_targets[item] for item, identity in movies.items()
        }
        stable_episodes = {
            (identity[3], identity[4], identity[5]): episode_targets[item]
            for item, identities in episodes.items()
            for identity in identities
        }
        source_maps[server_id] = (
            movie_targets,
            episode_targets,
            stable_movies,
            stable_episodes,
        )
        for keys in [*movie_targets.values(), *episode_targets.values()]:
            for key in keys:
                coverage[key].add(server_id)

    healthy = {
        server_id
        for server_id, source in sources.items()
        if source.error is None
        and any(
            status.available
            and status.config_id == source.provider_config_id
            and status.observed_config_id == server_id
            and status.provider == source.provider
            for status in refresh.statuses
        )
    }
    by_target: dict[PlaybackTargetKey, list[PlaybackHistoryEvent]] = defaultdict(list)
    unmatched_sources: set[int] = set()
    for event in (await db.scalars(select(PlaybackHistoryEvent))).all():
        source = next(
            (
                candidate
                for candidate in sources.values()
                if candidate.event_matches(event)
            ),
            None,
        )
        if source is None or not source.event_matches(event):
            continue
        movies, episodes, stable_movies, stable_episodes = source_maps[source.server_id]
        if event.provider_media_type == "movie":
            keys = movies.get(
                event.source_item_id, stable_movies.get(event.tmdb_id, [])
            )
        else:
            keys = episodes.get(
                event.source_item_id,
                stable_episodes.get(
                    (event.tmdb_id, event.season_number, event.episode_number), []
                ),
            )
        if not keys and event.tmdb_id is None:
            unmatched_sources.add(source.server_id)
        for key in set(keys):
            by_target[key].append(event)

    values = {}
    reasons = {scope: {} for scope in PLAYBACK_TARGET_SCOPES}
    samples: dict[str, list[int]] = {scope: [] for scope in PLAYBACK_TARGET_SCOPES}
    for scope, ids in targets.items():
        for target_id in ids:
            key = (scope, target_id)
            servers = coverage.get(key, set())
            reason = None
            if not servers:
                reason = "missing media-server identity"
            elif not servers.issubset(healthy):
                reason = "selected history provider unavailable or not synced"
            elif servers & unmatched_sources:
                reason = "unmatched provider history prevents complete totals"
            if reason:
                reasons[scope][reason] = reasons[scope].get(reason, 0) + 1
                samples[scope].append(target_id)
                continue
            completion_supported = all(
                sources[s].provider in {Service.TRACEARR, Service.TAUTULLI}
                for s in servers
            )
            values[key] = history_values(
                by_target[key], completion_supported=completion_supported
            )
            if completion_requested and not completion_supported:
                detail = "selected provider does not report completion"
                reasons[scope][detail] = reasons[scope].get(detail, 0) + 1
            elif (
                completion_requested
                and "playback_history.last_completed_at" not in values[key]
            ):
                detail = "provider completion flags are unknown"
                reasons[scope][detail] = reasons[scope].get(detail, 0) + 1
            if (
                requested_fields is None
                or "playback_history.usernames" in requested_fields
            ) and "playback_history.usernames" not in values[key]:
                detail = "provider usernames are incomplete"
                reasons[scope][detail] = reasons[scope].get(detail, 0) + 1
            values[key]["_history_sources"] = [
                {
                    "server": sources[s].server_name,
                    "provider": sources[s].provider_key or "unknown",
                }
                for s in sorted(servers)
            ]
    relevant_servers = {
        server_id for servers in coverage.values() for server_id in servers
    }
    errors = [
        status.error
        for status in refresh.statuses
        if status.error and status.observed_config_id in relevant_servers
    ]
    errors.extend(
        source.error
        for server_id, source in sources.items()
        if server_id in relevant_servers and source.error
    )
    return PlaybackRuleSnapshot(
        values_by_target=values,
        available_targets=set(values),
        target_counts={scope: len(ids) for scope, ids in targets.items()},
        errors=errors,
        has_configured_provider=refresh.has_configured_provider,
        provider_statuses=refresh.statuses,
        unavailable_reasons=reasons,
        unavailable_target_samples={scope: ids[:10] for scope, ids in samples.items()},
        available_fields_by_target={
            key: set(value) - {"_history_sources"} for key, value in values.items()
        },
        target_ids_by_scope=targets,
    )
