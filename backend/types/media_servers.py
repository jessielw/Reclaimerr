from backend.enums import Service

MEDIA_SERVERS = frozenset[Service]({Service.JELLYFIN, Service.PLEX})
