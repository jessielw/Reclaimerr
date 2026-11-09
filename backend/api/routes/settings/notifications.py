from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import require_admin, require_page_access
from backend.core.logger import LOG
from backend.database import get_db
from backend.database.models import NotificationSetting, User
from backend.enums import NotificationChannel, NotificationType, PageAccess, UserRole
from backend.models.settings import (
    ApproveDestinationRequest,
    NotificationEmailStatus,
    NotificationSettingItem,
    NotificationTestRequest,
    RestrictedDestination,
    normalize_notification_preferences,
)
from backend.services.notification_destinations import (
    approval_error,
    describe_destination,
    destination_approval_for_user,
    is_deliverable,
    restricted_destinations,
    sync_pending_destination_notice,
    url_fingerprint,
)
from backend.services.notifications import test_notification_url, test_system_email
from backend.services.smtp import load_smtp_config

router = APIRouter(tags=["settings", "notifications"])
limiter = Limiter(key_func=get_remote_address)

# Any user with the Settings page can call the test route, and it makes the
# server send to an address of their choosing, so it is rate limited and the
# raw delivery error (which can describe internal hosts) is kept for admins.
GENERIC_TEST_FAILURE = (
    "Test notification failed. Check the destination and try again, or ask an "
    "administrator to check the logs."
)


def _approval_for(notification: NotificationSetting, user: User):
    """Approval state of an apprise row, or None for a system email row."""
    if notification.channel != NotificationChannel.APPRISE or not notification.url:
        return None
    return destination_approval_for_user(
        notification.url, user, notification.approved_url_hash
    )


def _to_item(notification: NotificationSetting, user: User) -> NotificationSettingItem:
    return NotificationSettingItem(
        id=notification.id,
        enabled=notification.enabled,
        name=notification.name,
        channel=NotificationChannel(notification.channel),
        url=notification.url,
        target_email=notification.target_email,
        new_cleanup_candidates=notification.new_cleanup_candidates,
        request_approved=notification.request_approved,
        request_declined=notification.request_declined,
        admin_message=notification.admin_message,
        task_failure=notification.task_failure,
        admin_new_delete_request=notification.admin_new_delete_request,
        admin_new_protection_request=notification.admin_new_protection_request,
        admin_request_cancelled=notification.admin_request_cancelled,
        admin_delete_execution_failed=notification.admin_delete_execution_failed,
        update_available=notification.update_available,
        delete_request_execution_succeeded=notification.delete_request_execution_succeeded,
        delete_request_execution_failed=notification.delete_request_execution_failed,
        requester_leaving_soon=notification.requester_leaving_soon,
        preferences=normalize_notification_preferences(notification.preferences),
        approval=_approval_for(notification, user),
    )


@router.get("/notifications")
async def get_notification_settings(
    current_user: Annotated[User, Depends(require_page_access(PageAccess.SETTINGS))],
    db: AsyncSession = Depends(get_db),
) -> list[NotificationSettingItem]:
    """Get all notification settings for the current user."""
    result = await db.execute(
        select(NotificationSetting).where(
            NotificationSetting.user_id == current_user.id
        )
    )
    notifications = result.scalars().all()

    return [_to_item(n, current_user) for n in notifications]


@router.get("/notifications/email-status")
async def get_notification_email_status(
    current_user: Annotated[User, Depends(require_page_access(PageAccess.SETTINGS))],
    db: AsyncSession = Depends(get_db),
) -> NotificationEmailStatus:
    """Report whether this user can add a system email destination.

    Kept separate from the list endpoint, which is typed as a bare list of
    settings and has no room for instance-level state.
    """
    smtp = await load_smtp_config(db)
    result = await db.execute(
        select(NotificationSetting.id).where(
            NotificationSetting.user_id == current_user.id,
            NotificationSetting.channel == NotificationChannel.SYSTEM_EMAIL,
        )
    )
    return NotificationEmailStatus(
        available=smtp is not None,
        account_email=current_user.email,
        already_configured=result.scalars().first() is not None,
    )


@router.post("/notifications/test")
@limiter.limit("5/minute")
async def test_notification(
    data: NotificationTestRequest,
    # named 'request' because the rate limiter looks it up by that name
    request: Request,
    current_user: Annotated[User, Depends(require_page_access(PageAccess.SETTINGS))],
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Test a notification by sending a test payload to the destination."""
    if data.channel == NotificationChannel.SYSTEM_EMAIL:
        smtp = await load_smtp_config(db)
        if smtp is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Email notifications are unavailable. Ask an administrator "
                    "to configure the SMTP server."
                ),
            )
        recipient = data.target_email or current_user.email
        if not recipient:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No recipient address. Set an email address on your account, "
                    "or enter one for this destination."
                ),
            )
        success, error_message = await test_system_email(smtp, recipient)
    else:
        if not data.url:
            raise HTTPException(
                status_code=400, detail="Apprise URL is required to test"
            )
        # a non-admin can only test a destination that is allowed for them: a
        # built-in service, or one of their own saved URLs an admin approved
        approved_hash = None
        if current_user.role is not UserRole.ADMIN:
            saved = await db.execute(
                select(NotificationSetting.approved_url_hash).where(
                    NotificationSetting.user_id == current_user.id,
                    NotificationSetting.url == data.url,
                )
            )
            approved_hash = saved.scalars().first()
        state = destination_approval_for_user(data.url, current_user, approved_hash)
        if not is_deliverable(state):
            raise HTTPException(status_code=403, detail=approval_error(state))
        success, error_message = await test_notification_url(
            data.url, follow_redirects=current_user.role is UserRole.ADMIN
        )

    if not success:
        detail = (
            error_message
            if current_user.role is UserRole.ADMIN
            else GENERIC_TEST_FAILURE
        )
        raise HTTPException(status_code=400, detail=detail)

    return {"message": "Test notification sent successfully"}


@router.post("/notifications")
async def create_or_update_notification(
    data: NotificationSettingItem,
    current_user: Annotated[User, Depends(require_page_access(PageAccess.SETTINGS))],
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create or update a notification setting."""
    # validate that non-admin users cannot enable admin-only notifications
    if current_user.role is not UserRole.ADMIN:
        enabled_types = (
            (NotificationType.ADMIN_MESSAGE, data.admin_message),
            (NotificationType.TASK_FAILURE, data.task_failure),
            (NotificationType.ADMIN_NEW_DELETE_REQUEST, data.admin_new_delete_request),
            (
                NotificationType.ADMIN_NEW_PROTECTION_REQUEST,
                data.admin_new_protection_request,
            ),
            (NotificationType.ADMIN_REQUEST_CANCELLED, data.admin_request_cancelled),
            (
                NotificationType.ADMIN_DELETE_EXECUTION_FAILED,
                data.admin_delete_execution_failed,
            ),
            (NotificationType.UPDATE_AVAILABLE, data.update_available),
        )
        if any(
            enabled and notification_type.is_admin_only()
            for notification_type, enabled in enabled_types
        ):
            raise HTTPException(
                status_code=403,
                detail="Only administrators can enable admin-only notifications",
            )

    # Non-admins may save a destination outside the built-in services, but it
    # stays pending (and is never delivered to) until an admin approves it.
    # Unparseable URLs could never be approved or delivered, so reject them.
    url_changed = True
    if data.channel == NotificationChannel.APPRISE and data.url:
        if destination_approval_for_user(data.url, current_user, None) == "invalid":
            raise HTTPException(status_code=400, detail=approval_error("invalid"))

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

        url_changed = notification.url != data.url
        if url_changed:
            # an approval is for one exact URL
            notification.approved_url_hash = None

        # update fields
        notification.enabled = data.enabled
        notification.name = data.name
        notification.channel = data.channel
        notification.url = data.url
        notification.target_email = data.target_email
        notification.new_cleanup_candidates = data.new_cleanup_candidates
        notification.request_approved = data.request_approved
        notification.request_declined = data.request_declined
        notification.admin_message = data.admin_message
        notification.task_failure = data.task_failure
        notification.admin_new_delete_request = data.admin_new_delete_request
        notification.admin_new_protection_request = data.admin_new_protection_request
        notification.admin_request_cancelled = data.admin_request_cancelled
        notification.admin_delete_execution_failed = data.admin_delete_execution_failed
        notification.update_available = data.update_available
        notification.delete_request_execution_succeeded = (
            data.delete_request_execution_succeeded
        )
        notification.delete_request_execution_failed = (
            data.delete_request_execution_failed
        )
        notification.requester_leaving_soon = data.requester_leaving_soon
        notification.preferences = normalize_notification_preferences(data.preferences)

        await db.commit()
        await db.refresh(notification)

        LOG.info(
            f"Updated notification setting {notification.id} for user {current_user.username}"
        )
        message = "Notification setting updated successfully"
    else:
        # One email destination per user is all that is useful: they all resolve
        # to the same mailbox, so a second row would just duplicate every send.
        if data.channel == NotificationChannel.SYSTEM_EMAIL:
            existing = await db.execute(
                select(NotificationSetting.id).where(
                    NotificationSetting.user_id == current_user.id,
                    NotificationSetting.channel == NotificationChannel.SYSTEM_EMAIL,
                )
            )
            if existing.scalars().first() is not None:
                raise HTTPException(
                    status_code=409,
                    detail="You already have an email destination",
                )

        # create new notification
        notification = NotificationSetting(
            user_id=current_user.id,
            enabled=data.enabled,
            name=data.name,
            channel=data.channel,
            url=data.url,
            target_email=data.target_email,
            new_cleanup_candidates=data.new_cleanup_candidates,
            request_approved=data.request_approved,
            request_declined=data.request_declined,
            admin_message=data.admin_message,
            task_failure=data.task_failure,
            admin_new_delete_request=data.admin_new_delete_request,
            admin_new_protection_request=data.admin_new_protection_request,
            admin_request_cancelled=data.admin_request_cancelled,
            admin_delete_execution_failed=data.admin_delete_execution_failed,
            update_available=data.update_available,
            delete_request_execution_succeeded=data.delete_request_execution_succeeded,
            delete_request_execution_failed=data.delete_request_execution_failed,
            requester_leaving_soon=data.requester_leaving_soon,
            preferences=normalize_notification_preferences(data.preferences),
        )
        db.add(notification)
        await db.commit()
        await db.refresh(notification)

        LOG.info(
            f"Created notification setting {notification.id} for user {current_user.username}"
        )
        message = "Notification setting created successfully"

    if notification.channel == NotificationChannel.APPRISE and url_changed:
        await sync_pending_destination_notice(db)
        await db.commit()

    return {
        "message": message,
        "data": _to_item(notification, current_user),
    }


@router.delete("/notifications/{notification_id}")
async def delete_notification(
    notification_id: int,
    current_user: Annotated[User, Depends(require_page_access(PageAccess.SETTINGS))],
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
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
    await db.flush()
    await sync_pending_destination_notice(db)
    await db.commit()

    LOG.info(
        f"Deleted notification setting {notification_id} for user {current_user.username}"
    )

    return {"message": "Notification setting deleted successfully"}


@router.get("/notifications/restricted-destinations")
async def list_restricted_destinations(
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> list[RestrictedDestination]:
    """Non-admin destinations outside the built-in services, for admin review."""
    items: list[RestrictedDestination] = []
    for notification, owner, state in await restricted_destinations(db):
        url = notification.url or ""
        info = describe_destination(url)
        if info is None or state == "invalid" or state == "not_required":
            continue
        items.append(
            RestrictedDestination(
                id=notification.id,
                username=owner.username,
                name=notification.name,
                state=state,
                service=str(info["service"]),
                host=str(info["host"]),
                redacted_url=str(info["redacted_url"]),
                internal_host=bool(info["internal_host"]),
                fingerprint=url_fingerprint(url),
            )
        )
    return items


@router.post("/notifications/{notification_id}/approval")
async def approve_destination(
    notification_id: int,
    data: ApproveDestinationRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Approve the exact URL an admin reviewed for a non-admin destination."""
    notification = await db.get(NotificationSetting, notification_id)
    if (
        notification is None
        or notification.channel != NotificationChannel.APPRISE
        or not notification.url
    ):
        raise HTTPException(status_code=404, detail="Notification setting not found")
    if describe_destination(notification.url) is None:
        raise HTTPException(status_code=400, detail=approval_error("invalid"))
    if url_fingerprint(notification.url) != data.fingerprint:
        # the owner changed the URL after the admin loaded the review list
        raise HTTPException(
            status_code=409,
            detail="This destination changed since you opened it. Review it again.",
        )
    notification.approved_url_hash = data.fingerprint
    await sync_pending_destination_notice(db)
    await db.commit()
    LOG.info(
        f"Admin {admin.username} approved notification destination {notification_id}"
    )
    return {"message": "Destination approved"}


@router.delete("/notifications/{notification_id}/approval")
async def revoke_destination_approval(
    notification_id: int,
    admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Withdraw a destination's approval; it goes back to pending."""
    notification = await db.get(NotificationSetting, notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification setting not found")
    notification.approved_url_hash = None
    await sync_pending_destination_notice(db)
    await db.commit()
    LOG.info(
        f"Admin {admin.username} revoked approval of notification destination "
        f"{notification_id}"
    )
    return {"message": "Approval revoked"}
