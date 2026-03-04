from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import get_current_user
from backend.core.logger import LOG
from backend.database import get_db
from backend.database.models import NotificationSetting, User
from backend.enums import UserRole
from backend.models.settings import NotificationSettingItem, NotificationTestRequest
from backend.services.notifications import test_notification_url

router = APIRouter(tags=["settings", "notifications"])


@router.get("/notifications")
async def get_notification_settings(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> list[NotificationSettingItem]:
    """Get all notification settings for the current user."""
    result = await db.execute(
        select(NotificationSetting).where(
            NotificationSetting.user_id == current_user.id
        )
    )
    notifications = result.scalars().all()

    return [
        NotificationSettingItem(
            id=n.id,
            enabled=n.enabled,
            name=n.name,
            url=n.url,
            new_cleanup_candidates=n.new_cleanup_candidates,
            request_approved=n.request_approved,
            request_declined=n.request_declined,
            admin_message=n.admin_message,
            task_failure=n.task_failure,
        )
        for n in notifications
    ]


@router.post("/notifications/test")
async def test_notification(
    data: NotificationTestRequest,
    _current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """Test a notification by sending a test payload to the provided URL."""
    if not data.url:
        raise HTTPException(status_code=400, detail="Apprise URL is required to test")

    success = await test_notification_url(data.url)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to send test notification")

    return {"message": "Test notification sent successfully"}


@router.post("/notifications")
async def create_or_update_notification(
    data: NotificationSettingItem,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create or update a notification setting."""
    # validate that non-admin users cannot enable admin-only notifications
    if current_user.role is not UserRole.ADMIN:
        if data.task_failure:
            raise HTTPException(
                status_code=403,
                detail="Only administrators can enable task failure notifications",
            )
        if data.admin_message:
            raise HTTPException(
                status_code=403,
                detail="Only administrators can enable admin message notifications",
            )

    if data.id:
        # update existing notification
        result = await db.execute(
            select(NotificationSetting).where(NotificationSetting.id == data.id)
        )
        notification = result.scalar_one_or_none()

        if not notification:
            raise HTTPException(
                status_code=404, detail="Notification setting not found"
            )

        # ensure user owns this notification
        if notification.user_id != current_user.id:
            raise HTTPException(
                status_code=403, detail="Not authorized to modify this notification"
            )

        # update fields
        notification.enabled = data.enabled
        notification.name = data.name
        notification.url = data.url
        notification.new_cleanup_candidates = data.new_cleanup_candidates
        notification.request_approved = data.request_approved
        notification.request_declined = data.request_declined
        notification.admin_message = data.admin_message
        notification.task_failure = data.task_failure

        await db.commit()
        await db.refresh(notification)

        LOG.info(
            f"Updated notification setting {notification.id} for user {current_user.username}"
        )
        message = "Notification setting updated successfully"
    else:
        # create new notification
        notification = NotificationSetting(
            user_id=current_user.id,
            enabled=data.enabled,
            name=data.name,
            url=data.url,
            new_cleanup_candidates=data.new_cleanup_candidates,
            request_approved=data.request_approved,
            request_declined=data.request_declined,
            admin_message=data.admin_message,
            task_failure=data.task_failure,
        )
        db.add(notification)
        await db.commit()
        await db.refresh(notification)

        LOG.info(
            f"Created notification setting {notification.id} for user {current_user.username}"
        )
        message = "Notification setting created successfully"

    return {
        "message": message,
        "data": NotificationSettingItem(
            id=notification.id,
            enabled=notification.enabled,
            name=notification.name,
            url=notification.url,
            new_cleanup_candidates=notification.new_cleanup_candidates,
            request_approved=notification.request_approved,
            request_declined=notification.request_declined,
            admin_message=notification.admin_message,
            task_failure=notification.task_failure,
        ),
    }


@router.delete("/notifications/{notification_id}")
async def delete_notification(
    notification_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove a notification setting."""
    result = await db.execute(
        select(NotificationSetting).where(NotificationSetting.id == notification_id)
    )
    notification = result.scalar_one_or_none()

    if not notification:
        raise HTTPException(status_code=404, detail="Notification setting not found")

    # ensure user owns this notification
    if notification.user_id != current_user.id:
        raise HTTPException(
            status_code=403, detail="Not authorized to delete this notification"
        )

    await db.delete(notification)
    await db.commit()

    LOG.info(
        f"Deleted notification setting {notification_id} for user {current_user.username}"
    )

    return {"message": "Notification setting deleted successfully"}
