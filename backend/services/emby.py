from backend.enums import Service

from .emby_base import EmbyServiceBase


class EmbyService(EmbyServiceBase):
    """Emby media server backend."""

    def __init__(self, api_key: str, base_url: str) -> None:
        super().__init__(
            api_key=api_key,
            service_url=base_url,
            service_type=Service.EMBY,
        )
