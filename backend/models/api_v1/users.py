from pydantic import Field

from backend.models.media_identity import LinkedMediaIdentity
from backend.models.media_user_import import MediaImportAccount


class UserAccountResponse(MediaImportAccount):
    """Local account details and current persisted media-server links."""

    media_identities: list[LinkedMediaIdentity] = Field(
        default_factory=list,
        description="Current linked media-server identities; empty for unlinked accounts.",
    )
