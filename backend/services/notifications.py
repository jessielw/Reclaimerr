from __future__ import annotations

import apprise
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    retry_if_result,
    stop_after_attempt,
    wait_exponential,
)

from backend.core.logger import LOG
from backend.database import async_db
from backend.database.models import NotificationSetting, User
from backend.enums import LogLevel, NotificationType, UserRole

__all__ = [
    "notify_task_failure",
    "notify_user",
    "notify_users",
    "notify_all_users",
    "notify_admins",
]


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=(retry_if_exception_type(Exception) | retry_if_result(lambda x: x is False)),
    before_sleep=before_sleep_log(LOG.logger, LogLevel.WARNING.value),
    reraise=False,  # don't raise after retries exhausted, return False
)
async def send_notification(
    url: str,
    title: str,
    message: str,
    body_format: apprise.NotifyFormat = apprise.NotifyFormat.MARKDOWN,
) -> bool:
    """
    Send a single notification to a specific URL with automatic retry logic.

    Uses tenacity for exponential backoff. Retries up to 3 times (4 total attempts)
    with delays: 1s, 2s, 4s between retries.

    Args:
        url: Apprise notification URL (e.g., discord://webhook_id/webhook_token)
        title: Notification title
        message: Notification body/message
        body_format: Format of the message body (MARKDOWN, TEXT, or HTML)

    Returns:
        bool: True if notification was sent successfully, False otherwise
    """
    ap = apprise.Apprise()
    ap.add(url)

    try:
        result = await ap.async_notify(
            body=message,
            title=title,
            body_format=body_format,
        )
        if not result:
            LOG.warning(f"Apprise returned False for notification to {url}")
        return bool(result)
    except Exception as e:
        LOG.error(f"Failed to send notification to {url}: {e}")
        raise  # let tenacity handle the retry


async def notify_user(
    user_id: int,
    notification_type: NotificationType,
    title: str,
    message: str,
    body_format: apprise.NotifyFormat = apprise.NotifyFormat.MARKDOWN,
) -> dict[str, int]:
    """
    Send a notification to a specific user based on their notification preferences.

    Args:
        user_id: ID of the user to notify
        notification_type: Type of notification being sent
        title: Notification title
        message: Notification body/message
        body_format: Format of the message body

    Returns:
        dict with 'sent' and 'failed' counts
    """
    results = {"sent": 0, "failed": 0}

    async with async_db() as session:
        # get all enabled notification settings for this user that have this type enabled
        result = await session.execute(
            select(NotificationSetting).where(
                NotificationSetting.user_id == user_id,
                NotificationSetting.enabled == True,
            )
        )
        settings = result.scalars().all()

        # filter settings based on notification type
        type_field = _notification_type_to_field(notification_type)
        eligible_settings = [s for s in settings if getattr(s, type_field, False)]

        if not eligible_settings:
            LOG.debug(
                f"No notification settings enabled for user {user_id} and type {notification_type}"
            )
            return results

        # send to all eligible notification endpoints
        for setting in eligible_settings:
            success = await send_notification(
                url=setting.url,
                title=title,
                message=message,
                body_format=body_format,
            )

            if success:
                results["sent"] += 1
                LOG.info(
                    f"Sent {notification_type} notification to user {user_id} via {setting.name or setting.url}"
                )
            else:
                results["failed"] += 1
                LOG.warning(
                    f"Failed to send {notification_type} notification to user {user_id} via {setting.name or setting.url}"
                )

    return results


async def notify_admins(
    notification_type: NotificationType,
    title: str,
    message: str,
    body_format: apprise.NotifyFormat = apprise.NotifyFormat.MARKDOWN,
) -> dict[str, int]:
    """
    Send a notification to all admin users who have this notification type enabled.

    Args:
        notification_type: Type of notification being sent
        title: Notification title
        message: Notification body/message
        body_format: Format of the message body

    Returns:
        dict with 'sent' and 'failed' counts
    """
    results = {"sent": 0, "failed": 0}

    async with async_db() as session:
        # get all admin users
        result = await session.execute(
            select(User).where(
                User.role == UserRole.ADMIN,
                User.is_active == True,  # noqa: E712
            )
        )
        admin_users = result.scalars().all()

    if not admin_users:
        LOG.warning("No active admin users found to send notification to")
        return results

    # send to each admin user (each creates its own session)
    for user in admin_users:
        user_results = await notify_user(
            user_id=user.id,
            notification_type=notification_type,
            title=title,
            message=message,
            body_format=body_format,
        )
        results["sent"] += user_results["sent"]
        results["failed"] += user_results["failed"]

    return results


async def notify_users(
    user_ids: list[int],
    notification_type: NotificationType,
    title: str,
    message: str,
    body_format: apprise.NotifyFormat = apprise.NotifyFormat.MARKDOWN,
) -> dict[str, int]:
    """
    Send a notification to multiple specific users.

    Args:
        user_ids: List of user IDs to notify
        notification_type: Type of notification being sent
        title: Notification title
        message: Notification body/message
        body_format: Format of the message body

    Returns:
        dict with 'sent' and 'failed' counts
    """
    results = {"sent": 0, "failed": 0}

    for user_id in user_ids:
        user_results = await notify_user(
            user_id=user_id,
            notification_type=notification_type,
            title=title,
            message=message,
            body_format=body_format,
        )
        results["sent"] += user_results["sent"]
        results["failed"] += user_results["failed"]

    return results


async def notify_all_users(
    notification_type: NotificationType,
    title: str,
    message: str,
    body_format: apprise.NotifyFormat = apprise.NotifyFormat.MARKDOWN,
) -> dict[str, int]:
    """
    Send a notification to all active users who have this notification type enabled.

    This function optimizes database access by loading all users and their notification
    settings in a single query, then processing them directly without additional queries.

    Args:
        notification_type: Type of notification being sent
        title: Notification title
        message: Notification body/message
        body_format: Format of the message body

    Returns:
        dict with 'sent' and 'failed' counts
    """
    results = {"sent": 0, "failed": 0}

    async with async_db() as session:
        # get all active users with their notification settings in one query
        stmt = (
            select(User)
            .where(User.is_active == True)
            .options(selectinload(User.notification_settings))
        )
        result = await session.execute(stmt)
        users = result.scalars().all()

    if not users:
        LOG.debug("No active users found to send notification to")
        return results

    # determine which field to check for this notification type
    type_field = _notification_type_to_field(notification_type)

    # process each user's notification settings directly (already loaded via selectinload)
    for user in users:
        # filter enabled settings that support this notification type
        eligible_settings = [
            s
            for s in user.notification_settings
            if s.enabled and getattr(s, type_field, False)
        ]

        if not eligible_settings:
            LOG.debug(
                f"No notification settings enabled for user {user.id} and type {notification_type}"
            )
            continue

        # send to all eligible notification endpoints for this user
        for setting in eligible_settings:
            success = await send_notification(
                url=setting.url,
                title=title,
                message=message,
                body_format=body_format,
            )

            if success:
                results["sent"] += 1
                LOG.info(
                    f"Sent {notification_type} notification to user {user.id} via {setting.name or setting.url}"
                )
            else:
                results["failed"] += 1
                LOG.warning(
                    f"Failed to send {notification_type} notification to user {user.id} via {setting.name or setting.url}"
                )

    return results


def _notification_type_to_field(notification_type: NotificationType) -> str:
    """
    Map NotificationType enum to the corresponding NotificationSetting field name.

    Args:
        notification_type: The notification type enum value

    Returns:
        Field name in NotificationSetting model
    """
    mapping = {
        NotificationType.NEW_CLEANUP_CANDIDATES: "new_cleanup_candidates",
        NotificationType.REQUEST_APPROVED: "request_approved",
        NotificationType.REQUEST_DECLINED: "request_declined",
        NotificationType.ADMIN_MESSAGE: "admin_message",
        NotificationType.TASK_FAILURE: "task_failure",
    }

    return mapping.get(notification_type, "")


async def notify_task_failure(
    task_name: str,
    error_message: str,
) -> dict[str, int]:
    """
    Send a task failure notification to all admins.

    Args:
        task_name: Name of the task that failed
        error_message: Error message from the task

    Returns:
        dict with 'sent' and 'failed' counts
    """
    return await notify_admins(
        notification_type=NotificationType.TASK_FAILURE,
        title="Task Failed",
        message=f"**Task:** {task_name}\n\n  **Error:**\n  ```\n{error_message}\n```",
        body_format=apprise.NotifyFormat.MARKDOWN,
    )


async def test_notification_url(
    url: str,
) -> bool:
    """
    Test a notification by sending a test payload to the provided URL.

    Args:
        url: Apprise notification URL to test
    """
    return await send_notification(
        url=url,
        title="This is a test notification from Reclaimerr",
        message="If you received this, your notification settings are working correctly!",
        body_format=apprise.NotifyFormat.MARKDOWN,
    )
