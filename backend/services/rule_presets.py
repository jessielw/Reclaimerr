"""Bundled starting points. These templates never create or execute saved rules."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.enums import MediaType
from backend.models.cleanup import CleanupRuleCreate


class RulePreset(BaseModel):
    id: str
    version: int = 1
    title: str
    description: str
    prerequisites: list[str]
    required_services: list[Literal["sonarr", "seerr"]] = Field(default_factory=list)
    target_scopes: list[str]
    required_inputs: list[Literal["library.id", "playback.fully_watched_usernames"]]
    rule: CleanupRuleCreate


def _condition(field: str, operator: str, value: Any = None) -> dict[str, Any]:
    return {"type": "condition", "field": field, "operator": operator, "value": value}


def _preset(
    id: str,
    title: str,
    description: str,
    scope: str,
    conditions: list[dict[str, Any]],
    *,
    prerequisites: list[str],
    users: bool = False,
    outcome: Literal["candidate", "protect"] = "candidate",
    required_services: list[Literal["sonarr", "seerr"]] | None = None,
    target_scopes: list[str] | None = None,
) -> RulePreset:
    return RulePreset(
        id=id,
        title=title,
        description=description,
        prerequisites=prerequisites,
        required_services=required_services or [],
        target_scopes=target_scopes or [scope],
        required_inputs=(
            ["library.id", "playback.fully_watched_usernames"]
            if users
            else ["library.id"]
        ),
        rule=CleanupRuleCreate(
            name=title,
            description=description,
            media_type=MediaType.MOVIE
            if scope == "movie_version"
            else MediaType.SERIES,
            target_scope=scope,
            enabled=False,
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        _condition("library.id", "contains_any", []),
                        *conditions,
                    ],
                },
            },
            action={
                "outcome": outcome,
                "candidate": outcome == "candidate",
                "tag_enabled": False,
                "arr_tag": None,
                "arr_action": "delete",
                "media_server_action": "delete" if outcome == "candidate" else None,
                "auto_delete_enabled": False,
                "auto_delete_delay_days": None,
                "move_instead_of_delete": False,
                "quality_profile_id": None,
                "trigger_search": False,
                "radarr_service_config_id": None,
                "sonarr_service_config_id": None,
                "radarr_service_config_ids": [],
                "sonarr_service_config_ids": [],
            },
        ),
    )


def rule_presets() -> list[RulePreset]:
    """Return independent templates with empty, required installation inputs.

    Empty library/user conditions deliberately fail normal rule validation until
    configured. Never remove these conditions to make an incomplete draft valid.
    """
    return [
        _preset(
            "old-unwatched-movies",
            "Old unwatched movies",
            "Movie versions added more than 180 days ago with no recorded viewing "
            "of the current copy. "
            "Missing imported watch history is not proof that nobody watched a movie.",
            "movie_version",
            [
                _condition("media.days_since_added", "greater_than", 180),
                _condition("watch.never_watched", "is_true"),
            ],
            prerequisites=["Sync movie libraries and watch state before previewing."],
        ),
        _preset(
            "watched-movies-not-revisited",
            "Watched movies not revisited",
            "Movie versions with recorded viewing whose last watched date is more "
            "than 90 days ago. Movies without a last watched date do not match.",
            "movie_version",
            [
                _condition("watch.never_watched", "is_false"),
                _condition("watch.days_since_last_watched", "greater_than", 90),
            ],
            prerequisites=[
                "Sync movie libraries and dated watch history before previewing."
            ],
        ),
        _preset(
            "fully-watched-seasons",
            "Fully watched seasons",
            "Regular seasons completed by every selected user, with the last "
            "recorded playback activity across all users more than 30 days ago. "
            "Unknown completion or activity does not match.",
            "season",
            [
                _condition("season.season_number", "greater_than", 0),
                _condition("playback.fully_watched_usernames", "contains_all", []),
                _condition("playback.days_since_last_activity", "greater_than", 30),
            ],
            prerequisites=[
                "Sync Sonarr's episode inventory and per-user watch state; completion uses the full episode list.",
                "Choose users with imported watch data. Recorded playback activity must have a date.",
            ],
            users=True,
            required_services=["sonarr"],
        ),
        _preset(
            "keep-newest-two-seasons",
            "Keep the newest two seasons",
            "Regular seasons older than the newest two known regular seasons, "
            "ordered by season number. Specials are excluded. Known seasons "
            "without files can count toward the two retained seasons.",
            "season",
            [
                _condition("season.season_number", "greater_than", 0),
                _condition("season.seasons_from_latest", "greater_than_or_equal", 2),
            ],
            prerequisites=[
                "Sync series and seasons. Preview the known season list before enabling."
            ],
        ),
        _preset(
            "large-movies-for-review",
            "Large movies for review",
            "Movie versions larger than 40 GiB for manual review. Change the size "
            "threshold in the editor; automatic deletion starts off.",
            "movie_version",
            [_condition("media.size", "greater_than", 40 * 1024**3)],
            prerequisites=["Sync movie file sizes before previewing."],
        ),
        _preset(
            "protect-favorites-watchlists",
            "Protect favorites and watchlists",
            "Protect movies or whole shows favorited or watchlisted by any user "
            "in the imported favorites/watchlist data. Protection takes precedence "
            "over matching cleanup rules while this rule matches.",
            "movie_version",
            [_condition("favorites.exists", "is_true")],
            outcome="protect",
            target_scopes=["movie_version", "series"],
            prerequisites=[
                "Sync Jellyfin/Emby favorites or linked Plex users' watchlists.",
                "Choose movies or shows. Save a separate rule for each type if you want both protected.",
            ],
        ),
        _preset(
            "finished-ended-shows",
            "Finished, ended shows",
            "Whole shows marked ended in Sonarr, completed by every selected user, "
            "with last recorded playback activity across all users more than 90 "
            "days ago. Completion covers all regular seasons; specials are excluded.",
            "series",
            [
                _condition("sonarr.series_status", "equals", "ended"),
                _condition("playback.fully_watched_usernames", "contains_all", []),
                _condition("playback.days_since_last_activity", "greater_than", 90),
            ],
            users=True,
            required_services=["sonarr"],
            prerequisites=[
                "Sync Sonarr's full episode inventory and series status, plus per-user watch state.",
                "Unknown status, completion, or playback activity does not match. Whole-show cleanup also removes specials.",
            ],
        ),
        _preset(
            "requested-movies-watched",
            "Requested movies already watched",
            "Requested movies completed by at least one requester, with last "
            "recorded playback activity across all users more than 30 days ago. "
            "Other requesters may not have watched yet; completion need not be after the request.",
            "movie_version",
            [
                _condition("seerr.requested", "is_true"),
                _condition("seerr.requester_has_watched", "is_true"),
                _condition("playback.days_since_last_activity", "greater_than", 30),
            ],
            required_services=["seerr"],
            prerequisites=[
                "Configure and sync Seerr and requester watch-user identities under Settings → User Signals.",
                "Sync completion and dated playback data. Unknown request, completion, or activity does not match.",
            ],
        ),
    ]
