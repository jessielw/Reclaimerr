from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import require_admin
from backend.database import get_db
from backend.database.models import User
from backend.models.info import AdminNoticeResponse, AdminNoticesResponse
from backend.services.admin_notices import (
    count_unread_active_notices,
    list_active_admin_notices,
    mark_admin_notice_read,
    mark_admin_notice_unread,
    reconcile_stale_library_notice,
)

router = APIRouter(tags=["notices"])


def _to_notice_response(row) -> AdminNoticeResponse:
    return AdminNoticeResponse(
        id=row.id,
        kind=row.kind,
        severity=row.severity,
        title=row.title,
        message=row.message,
        action_label=row.action_label,
        action_href=row.action_href,
        is_read=bool(row.is_read),
        is_active=bool(row.is_active),
        read_at=row.read_at.isoformat() if row.read_at else None,
        last_occurred_at=row.last_occurred_at.isoformat()
        if row.last_occurred_at
        else None,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@router.get("/notices", response_model=AdminNoticesResponse)
async def get_admin_notices(
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AdminNoticesResponse:
    """Get active admin notices, ordered by severity and last occurred time."""
    # keep the stale library singleton aligned before rendering UI.
    await reconcile_stale_library_notice(db)
    await db.commit()

    rows = await list_active_admin_notices(db, limit=limit)
    unread_count = await count_unread_active_notices(db)
    return AdminNoticesResponse(
        unread_count=unread_count,
        items=[_to_notice_response(row) for row in rows],
    )


@router.post("/notices/{notice_id}/read")
async def mark_notice_read(
    notice_id: int,
    admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    """Mark the notice as read by the current admin user."""
    row = await mark_admin_notice_read(db, notice_id=notice_id, admin_user_id=admin.id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Notice not found"
        )
    await db.commit()
    return {"ok": True}


@router.post("/notices/{notice_id}/unread")
async def mark_notice_unread(
    notice_id: int,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    """Mark the notice as unread by the current admin user."""
    row = await mark_admin_notice_unread(db, notice_id=notice_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Notice not found"
        )
    await db.commit()
    return {"ok": True}
