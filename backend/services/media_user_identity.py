"""Read persisted identity links without contacting media servers."""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import MediaUserIdentity, ServiceConfig
from backend.models.media_identity import LinkedMediaIdentity


async def linked_media_identities(
    db: AsyncSession, user_ids: Iterable[int]
) -> dict[int, list[LinkedMediaIdentity]]:
    """Fetch linked identities in one query, scoped to the requested accounts."""
    by_user: dict[int, list[LinkedMediaIdentity]] = {
        user_id: [] for user_id in user_ids
    }
    if not by_user:
        return by_user

    rows = await db.execute(
        select(
            MediaUserIdentity.user_id,
            MediaUserIdentity.source_service,
            MediaUserIdentity.source_service_config_id,
            ServiceConfig.name.label("source_service_name"),
            MediaUserIdentity.source_user_id,
            MediaUserIdentity.username,
            MediaUserIdentity.display_name,
        )
        .join(
            ServiceConfig,
            ServiceConfig.id == MediaUserIdentity.source_service_config_id,
        )
        .where(MediaUserIdentity.user_id.in_(by_user))
        .order_by(
            MediaUserIdentity.source_service_config_id,
            MediaUserIdentity.source_user_id,
        )
    )
    for row in rows:
        by_user[row.user_id].append(
            LinkedMediaIdentity(
                source_service=row.source_service,
                source_service_config_id=row.source_service_config_id,
                source_service_name=row.source_service_name or row.source_service.value,
                source_user_id=row.source_user_id,
                username=row.username,
                display_name=row.display_name,
            )
        )
    return by_user
