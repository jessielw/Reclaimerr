"""Credential-free details for an account's linked media-server identities."""

from pydantic import BaseModel

from backend.enums import Service


class LinkedMediaIdentity(BaseModel):
    """A provider user ID scoped to its server, with the last stored names."""

    source_service: Service
    source_service_config_id: int
    source_service_name: str
    source_user_id: str
    username: str
    display_name: str | None = None
