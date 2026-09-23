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
from backend.database.models import SMTPSettings, User
from backend.models.settings import (
    SMTPCoverageResponse,
    SMTPEnableAllResponse,
    SMTPSettingsResponse,
    SMTPSettingsUpdate,
    SMTPTestRequest,
)
from backend.services.notifications import test_system_email
from backend.services.smtp import (
    SmtpConfig,
    email_destination_user_ids,
    new_system_email_destination,
)

router = APIRouter(tags=["settings", "smtp"])


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
    was_enabled = row.enabled

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

    # turning email on backfills every existing user; users created later are
    # handled as they arrive (see auto_enable_system_email)
    auto_enabled_users = 0
    if row.enabled and not was_enabled:
        auto_enabled_users, _ = await _enable_for_all_users(db)
        LOG.info(f"Enabled system email notifications for {auto_enabled_users} user(s)")

    response = _to_response(row)
    response.auto_enabled_users = auto_enabled_users
    return response


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


async def _enable_for_all_users(db: AsyncSession) -> tuple[int, int]:
    """Create a system email destination for every active user lacking one.

    Returns (created, skipped_no_email). A user who already has an email
    destination is left exactly as they configured it.
    """
    result = await db.execute(select(User).where(User.is_active == True))
    users = result.scalars().all()
    configured = await email_destination_user_ids(db)

    created = 0
    skipped_no_email = 0
    for user in users:
        if not user.email:
            skipped_no_email += 1
            continue
        if user.id in configured:
            continue
        db.add(new_system_email_destination(user))
        created += 1

    if created:
        await db.commit()
    return created, skipped_no_email


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
    configured = await email_destination_user_ids(db)

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
    created, skipped_no_email = await _enable_for_all_users(db)
    LOG.info(
        f"{admin.username} enabled system email notifications for {created} user(s)"
    )
    return SMTPEnableAllResponse(created=created, skipped_no_email=skipped_no_email)
