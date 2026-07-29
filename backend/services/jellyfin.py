from __future__ import annotations

from backend.enums import Service

from .emby_base import EmbyServiceBase, _authentication_headers


class JellyfinService(EmbyServiceBase):
    """Jellyfin media server backend."""

    def __init__(self, api_key: str, base_url: str) -> None:
        super().__init__(
            api_key=api_key,
            service_url=base_url,
            service_type=Service.JELLYFIN,
        )

    @staticmethod
    async def test_service(url: str, api_key: str) -> bool:
        """Test Jellyfin using its supported MediaBrowser authorization header."""
        return await JellyfinService._test_service(
            url, _authentication_headers(Service.JELLYFIN, api_key)
        )
