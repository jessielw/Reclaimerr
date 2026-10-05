"""Strict parsers for native session endpoints. Invalid is never empty."""

from typing import Any

from backend.enums import Service
from backend.models.live_playback import LiveSession, PlaybackSnapshot


def _id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value).strip() or None


def plex_sessions(payload: Any) -> PlaybackSnapshot:
    if not isinstance(payload, dict) or not isinstance(
        payload.get("MediaContainer"), dict
    ):
        return PlaybackSnapshot(available=False)
    container = payload["MediaContainer"]
    rows = container.get("Metadata")
    if rows is None and container.get("size") == 0:
        return PlaybackSnapshot()
    if not isinstance(rows, list):
        return PlaybackSnapshot(available=False)
    if "size" in container and container["size"] != len(rows):
        return PlaybackSnapshot(available=False)
    sessions = []
    for row in rows:
        if not isinstance(row, dict):
            return PlaybackSnapshot(available=False)
        # Non-library audio and photos cannot be cleanup targets.
        if row.get("type") in {"track", "photo"}:
            continue
        media = row.get("Media", [])
        if not isinstance(media, list) or any(not isinstance(m, dict) for m in media):
            return PlaybackSnapshot(available=False)
        selected = [m for m in media if m.get("selected") in (True, 1, "1")]
        # Multiple unselected versions are ambiguous: preserve every possible file.
        for version in selected or media or [{}]:
            parts = version.get("Part", [])
            if not isinstance(parts, list) or any(
                not isinstance(p, dict) for p in parts
            ):
                return PlaybackSnapshot(available=False)
            paths = tuple(
                p["file"] for p in parts if isinstance(p.get("file"), str) and p["file"]
            )
            item_id = _id(row.get("ratingKey"))
            if not item_id and not paths:
                return PlaybackSnapshot(available=False)
            sessions.append(
                LiveSession(
                    item_id,
                    _id(version.get("id")),
                    paths,
                    tuple(
                        str(row[k])
                        for k in ("parentRatingKey", "grandparentRatingKey")
                        if _id(row.get(k))
                    ),
                )
            )
    return PlaybackSnapshot(tuple(sessions))


def emby_sessions(payload: Any) -> PlaybackSnapshot:
    if not isinstance(payload, list):
        return PlaybackSnapshot(available=False)
    sessions = []
    for row in payload:
        if not isinstance(row, dict):
            return PlaybackSnapshot(available=False)
        item = row.get("NowPlayingItem")
        if item is None:
            continue
        if not isinstance(item, dict):
            return PlaybackSnapshot(available=False)
        if item.get("Type") in {"Audio", "Photo"}:
            continue
        state = row.get("PlayState") or {}
        if not isinstance(state, dict):
            return PlaybackSnapshot(available=False)
        media_id = _id(state.get("MediaSourceId"))
        sources = item.get("MediaSources") or []
        if not isinstance(sources, list) or any(
            not isinstance(s, dict) for s in sources
        ):
            return PlaybackSnapshot(available=False)
        source = state.get("MediaSource")
        if source is not None:
            if not isinstance(source, dict):
                return PlaybackSnapshot(available=False)
            sources = [source]
        selected = [s for s in sources if media_id and _id(s.get("Id")) == media_id]
        paths = tuple(
            s["Path"] for s in selected if isinstance(s.get("Path"), str) and s["Path"]
        )
        # Item.Path may describe a different version; use it only without a version ID.
        if not media_id and isinstance(item.get("Path"), str):
            paths = (item["Path"],)
        item_id = _id(item.get("Id"))
        if not item_id and not paths:
            return PlaybackSnapshot(available=False)
        sessions.append(
            LiveSession(
                item_id,
                media_id,
                paths,
                tuple(
                    str(item[k])
                    for k in ("ParentId", "SeasonId", "SeriesId")
                    if _id(item.get(k))
                ),
            )
        )
    return PlaybackSnapshot(tuple(sessions))


async def native_item_paths(
    client: Any, service: Service, item_id: str, media_id: str | None = None
) -> list[tuple[str, bool]]:
    """Resolve original media paths, never transcoder output paths."""
    from urllib.parse import quote

    if service is Service.PLEX:
        payload, _ = await client._make_request(
            f"library/metadata/{quote(item_id, safe='')}", timeout=10
        )
        rows = payload["MediaContainer"]["Metadata"]
        paths = []
        for row in rows:
            for media in row.get("Media", []):
                if media_id is None or str(media.get("id")) == media_id:
                    paths.extend(
                        (p["file"], False)
                        for p in media.get("Part", [])
                        if p.get("file")
                    )
            paths.extend(
                (p["path"], True) for p in row.get("Location", []) if p.get("path")
            )
        return paths
    row = await client._make_request(f"Items/{quote(item_id, safe='')}", timeout=10)
    sources = row.get("MediaSources", [])
    paths = [
        (s["Path"], False)
        for s in sources
        if s.get("Path") and (media_id is None or str(s.get("Id")) == media_id)
    ]
    if not paths and row.get("Path") and media_id is None:
        paths.append((row["Path"], row.get("Type") in {"Series", "Season"}))
    return paths


async def resolve_session_paths(
    client: Any, service: Service, snapshot: PlaybackSnapshot
) -> PlaybackSnapshot:
    if not snapshot.available:
        return snapshot
    sessions = []
    for session in snapshot.sessions:
        if session.paths:
            sessions.append(session)
            continue
        if session.item_id is None:
            return PlaybackSnapshot(available=False)
        paths = await native_item_paths(
            client, service, session.item_id, session.media_id
        )
        if not paths:
            return PlaybackSnapshot(available=False)
        sessions.append(
            LiveSession(
                session.item_id,
                session.media_id,
                tuple(p for p, _ in paths),
                session.parent_ids,
            )
        )
    return PlaybackSnapshot(tuple(sessions))
