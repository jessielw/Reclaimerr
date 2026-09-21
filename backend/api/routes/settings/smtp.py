from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import require_admin
from backend.core.encryption import fer_decrypt, fer_encrypt
from backend.core.logger import LOG
from backend.database import get_db
from backend.database.models import NotificationSetting, SMTPSettings, User
from backend.enums import NotificationChannel, UserRole
from backend.models.settings import (
    SMTPCoverageResponse,
    SMTPEnableAllResponse,
    SMTPSettingsResponse,
    SMTPSettingsUpdate,
    SMTPTestRequest,
)
from backend.services.notifications import test_system_email
from backend.services.smtp import SmtpConfig

router = APIRouter(tags=["settings", "smtp"])

# What a bulk-enabled destination opts into. These are the types a person
# actually wants in their inbox; the noisier admin-only ones are added for
# admins, who can still turn any of them off afterwards.
_DEFAULT_USER_TYPES: tuple[str, ...] = (
    "new_cleanup_candidates",
    "request_approved",
    "request_declined",
    "admin_message",
    "delete_request_execution_succeeded",
    "delete_request_execution_failed",
)
_DEFAULT_ADMIN_TYPES: tuple[str, ...] = (
    "task_failure",
    "admin_new_delete_request",
    "admin_new_protection_request",
    "admin_request_cancelled",
    "admin_delete_execution_failed",
)


async def _get_or_create_smtp_settings(db: AsyncSession) -> SMTPSettings:
    result = await db.execute(select(SMTPSettings))
    existing = result.scalars().first()
    if existing is not None:
        return existing

    created = SMTPSettings()
    db.add(created)
    await db.commit()
    await db.refresh(created)
    return created


def _to_response(row: SMTPSettings) -> SMTPSettingsResponse:
    return SMTPSettingsResponse(
        enabled=row.enabled,
        host=row.host or "",
        port=row.port or 587,
        security=row.security or "starttls",  # pyright: ignore[reportArgumentType]
        username=row.username or "",
        from_address=row.from_address or "",
        from_name=row.from_name or "Reclaimerr",
        reply_to=row.reply_to,
        password_configured=bool(row.password),
        updated_at=row.updated_at,
    )


def _resolve_password(request: SMTPSettingsUpdate, row: SMTPSettings) -> str:
    """Return the password to use: the supplied one, else the stored one."""
    if request.password is not None:
        return request.password
    if row.password:
        try:
            return fer_decrypt(row.password)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Stored SMTP password could not be decrypted. "
                    "Please re-enter and save the password."
                ),
            ) from exc
    return ""


def _validate_enabled_config(request: SMTPSettingsUpdate) -> None:
    if not request.enabled:
        return
    if not request.host:
        raise HTTPException(status_code=400, detail="SMTP host is required")
    if not request.from_address:
        raise HTTPException(status_code=400, detail="From address is required")


@router.get("/smtp", response_model=SMTPSettingsResponse)
async def get_smtp_settings(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SMTPSettingsResponse:
    """Get the instance SMTP settings, creating the single row if needed."""
    return _to_response(await _get_or_create_smtp_settings(db))


@router.put("/smtp", response_model=SMTPSettingsResponse)
async def update_smtp_settings(
    request: SMTPSettingsUpdate,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SMTPSettingsResponse:
    """Update the instance SMTP settings.

    A null password keeps whatever is stored, matching how the OIDC client
    secret behaves, so the UI never has to round-trip the secret back to us.
    """
    _validate_enabled_config(request)
    row = await _get_or_create_smtp_settings(db)

    row.enabled = request.enabled
    row.host = request.host
    row.port = request.port
    row.security = request.security
    row.username = request.username
    row.from_address = request.from_address
    row.from_name = request.from_name
    row.reply_to = request.reply_to
    if request.password is not None:
        row.password = fer_encrypt(request.password)
    row.updated_at = datetime.now(UTC)
    row.updated_by_user_id = admin.id

    db.add(row)
    await db.commit()
    await db.refresh(row)

    LOG.info(f"SMTP settings updated by {admin.username} (enabled={row.enabled})")
    return _to_response(row)


@router.post("/smtp/test")
async def test_smtp_settings(
    request: SMTPTestRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """Send a test email using the supplied settings, without saving them."""
    settings = request.settings
    if not settings.host:
        raise HTTPException(status_code=400, detail="SMTP host is required")
    if not settings.from_address:
        raise HTTPException(status_code=400, detail="From address is required")

    recipient = request.to or admin.email
    if not recipient:
        raise HTTPException(
            status_code=400,
            detail=(
                "No recipient to test with. Enter an address, or set an email "
                "address on your account."
            ),
        )

    row = await _get_or_create_smtp_settings(db)
    password = _resolve_password(settings, row)
    config = SmtpConfig(
        host=settings.host,
        port=settings.port,
        security=settings.security,
        username=settings.username,
        password=password,
        from_address=settings.from_address,
        from_name=settings.from_name,
        reply_to=settings.reply_to,
    )

    success, error_message = await test_system_email(config, recipient)
    if not success:
        raise HTTPException(
            status_code=400, detail=error_message or "Failed to send test email"
        )

    return {"message": f"Test email sent to {recipient}"}


async def _email_destination_user_ids(db: AsyncSession) -> set[int]:
    result = await db.execute(
        select(NotificationSetting.user_id).where(
            NotificationSetting.channel == NotificationChannel.SYSTEM_EMAIL
        )
    )
    return set(result.scalars().all())


@router.get("/smtp/coverage", response_model=SMTPCoverageResponse)
async def get_smtp_coverage(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SMTPCoverageResponse:
    """Report how many active users could be emailed, for the bulk control."""
    total = await db.scalar(
        select(func.count()).select_from(User).where(User.is_active == True)
    )
    result = await db.execute(
        select(User.id).where(User.is_active == True, User.email.is_not(None))
    )
    with_email = set(result.scalars().all())
    configured = await _email_destination_user_ids(db)

    return SMTPCoverageResponse(
        total_users=total or 0,
        with_email=len(with_email),
        already_enabled=len(with_email & configured),
        eligible=len(with_email - configured),
    )


@router.post("/smtp/enable-all", response_model=SMTPEnableAllResponse)
async def enable_email_for_all_users(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SMTPEnableAllResponse:
    """Create a system email destination for every user who lacks one.

    Idempotent: a user who already has an email destination is left exactly as
    they configured it, so running this twice never resets anyone's choices.
    """
    result = await db.execute(select(User).where(User.is_active == True))
    users = result.scalars().all()
    configured = await _email_destination_user_ids(db)

    created = 0
    skipped_no_email = 0
    for user in users:
        if not user.email:
            skipped_no_email += 1
            continue
        if user.id in configured:
            continue

        destination = NotificationSetting(
            user_id=user.id,
            enabled=True,
            name="Email",
            channel=NotificationChannel.SYSTEM_EMAIL,
        )
        enabled_types = list(_DEFAULT_USER_TYPES)
        if user.role is UserRole.ADMIN:
            enabled_types += _DEFAULT_ADMIN_TYPES
        for field in enabled_types:
            setattr(destination, field, True)

        db.add(destination)
        created += 1

    if created:
        await db.commit()

    LOG.info(
        f"{admin.username} enabled system email notifications for {created} user(s)"
    )
    return SMTPEnableAllResponse(created=created, skipped_no_email=skipped_no_email)
