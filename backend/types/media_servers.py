from typing import Literal

from backend.enums import Service

MediaServerType = Literal[Service.JELLYFIN, Service.PLEX]
MEDIA_SERVERS = frozenset[Service]({Service.JELLYFIN, Service.PLEX})
