from __future__ import annotations

from dataclasses import dataclass

from backend.enums import MediaType, Service


@dataclass(slots=True)
class PostActionWebhookEvent:
    action: str
    media_type: MediaType
    title: str | None = None
    tmdb_id: int | None = None
    candidate_id: int | None = None
    path: str | None = None
    local_path: str | None = None
    destination_path: str | None = None
    service_type: Service | str | None = None
    service_config_id: int | None = None
    movie_version_id: int | None = None
    season_id: int | None = None
    season_number: int | None = None
