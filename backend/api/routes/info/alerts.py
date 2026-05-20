from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import get_current_user
from backend.database import get_db
from backend.database.models import User
from backend.enums import AlertLevel, UserRole
from backend.models.alerts import SystemAlert
from backend.services.admin_notices import (
    list_active_admin_notices,
    reconcile_stale_library_notice,
)

router = APIRouter(tags=["alerts"])


@router.get("/alerts", response_model=list[SystemAlert])
async def get_system_alerts(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> list[SystemAlert]:
    """
    Return actionable system alerts visible to the requesting user.

    Each alert carries a required_permission. This only alerts the user
    that has permission for are returned. Admins bypass all permission
    checks. Checks are computed live so they're always accurate.
    """
    if current_user.role is not UserRole.ADMIN:
        return []

    await reconcile_stale_library_notice(db)
    await db.commit()

    rows = await list_active_admin_notices(db, limit=25, include_read=False)
    alerts: list[SystemAlert] = []
    for row in rows:
        alert_level = (
            AlertLevel.ERROR if row.severity == "error" else AlertLevel.WARNING
        )
        alerts.append(
            SystemAlert(
                id=f"notice_{row.id}",
                alert_level=alert_level,
                title=row.title,
                message=row.message,
                action_label=row.action_label,
                action_href=row.action_href,
            )
        )
    return alerts
