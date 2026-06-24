import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult, Result
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import (
    COOKIE_NAME,
    get_current_user,
    get_password_hash,
    get_request_session_id,
    require_permission,
    verify_password,
)
from backend.core.logger import LOG
from backend.core.settings import settings
from backend.core.utils.image_handling import save_picture_from_bytes
from backend.database import get_db
from backend.database.models import (
    GeneralSettings,
    MediaUserIdentity,
    ServiceConfig,
    User,
    UserSession,
)
from backend.enums import PageAccess, Permission, UserRole
from backend.models.auth import (
    ChangePasswordRequest,
    ChangeProfileInfoRequest,
    CreateUserRequest,
    MediaIdentityItem,
    MediaIdentityLinkRequest,
    MediaIdentityListResponse,
    RevokeOtherSessionsResponse,
    RevokeSessionResponse,
    UpdateUserRequest,
    UserInfo,
    UserSessionInfo,
)
from backend.services.admin_notices import resolve_singleton_notice
from backend.services.media_auth import media_auth_conflict_notice_key_for_source
from backend.user_types import DEFAULT_NEW_USER_ALLOWED_PAGES

router = APIRouter(prefix="/api/account", tags=["account"])


async def _default_allowed_pages_for_new_user(db: AsyncSession) -> list[str]:
    result = await db.execute(select(GeneralSettings.default_allowed_pages))
    allowed_pages = result.scalar_one_or_none()
    if not allowed_pages:
        return list(DEFAULT_NEW_USER_ALLOWED_PAGES)

    valid_pages: list[str] = []
    for page in allowed_pages:
        try:
            valid_pages.append(PageAccess(page).value)
        except ValueError:
            continue
    return valid_pages or list(DEFAULT_NEW_USER_ALLOWED_PAGES)


async def _revoke_user_sessions(
    db: AsyncSession,
    *,
    user_id: int,
    reason: str,
    exclude_session_id: str | None = None,
) -> int:
    """Mark active sessions revoked for a user."""
    statement = (
        update(UserSession)
        .where(
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC), revoked_reason=reason)
    )
    if exclude_session_id:
        statement = statement.where(UserSession.session_id != exclude_session_id)

    result = cast(CursorResult[Any], await db.execute(statement))
    return result.rowcount or 0  # pyright: ignore[reportAttributeAccessIssue]


def _serialize_media_identity(
    identity: MediaUserIdentity,
    linked_user: User | None,
    source_service_name: str | None,
) -> MediaIdentityItem:
    return MediaIdentityItem(
        id=identity.id,
        source_service=identity.source_service.value,
        source_service_config_id=identity.source_service_config_id,
        source_service_name=source_service_name or identity.source_service.value,
        source_user_id=identity.source_user_id,
        username=identity.username,
        username_normalized=identity.username_normalized,
        email=identity.email,
        display_name=identity.display_name,
        user_id=linked_user.id if linked_user else None,
        user_username=linked_user.username if linked_user else None,
        user_display_name=linked_user.display_name if linked_user else None,
        last_seen_at=identity.last_seen_at,
        linked_at=identity.linked_at,
    )


@router.get("/me", response_model=UserInfo)
async def get_me(
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserInfo:
    """Get current user info."""
    return UserInfo.from_user(current_user)


@router.post("/me")
async def update_profile(
    new_info: ChangeProfileInfoRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | None]:
    """Modify current user's profile."""
    # ensure email is not taken
    if new_info.email:
        result = await db.execute(
            select(User).where(User.email == new_info.email, User.id != current_user.id)
        )
        if result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already in use",
            )

    # update info
    current_user.display_name = new_info.display_name
    current_user.email = new_info.email
    await db.commit()

    LOG.info(f"User {current_user.username} updated their profile info")

    return {
        "message": "Profile info updated successfully",
        "email": current_user.email,
        "display_name": current_user.display_name,
    }


@router.post("/change-password")
async def change_password(
    request: ChangePasswordRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Change password."""
    requires_old_password = bool(current_user.password_hash) and not bool(
        current_user.require_password_change
    )
    if requires_old_password:
        if not request.old_password or not verify_password(
            request.old_password, current_user.password_hash
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid old password"
            )

    current_user.password_hash = get_password_hash(request.new_password)
    current_user.require_password_change = False
    current_user.bump_token_version()
    await _revoke_user_sessions(
        db,
        user_id=current_user.id,
        reason="password_changed",
    )
    await db.commit()

    # clear cookie so client is forced to re-login with the new password
    response.delete_cookie(
        key=COOKIE_NAME,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )

    return {"message": "Password changed successfully"}


@router.get("/users/media-identities", response_model=MediaIdentityListResponse)
async def list_media_identities(
    _actor: Annotated[User, Depends(require_permission(Permission.MANAGE_USERS))],
    q: str | None = Query(default=None, min_length=1, max_length=255),
    linked: bool | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> MediaIdentityListResponse:
    """List media identities with optional filtering by linked status and search query."""
    stmt = (
        select(MediaUserIdentity, User, ServiceConfig.name)
        .outerjoin(User, User.id == MediaUserIdentity.user_id)
        .outerjoin(
            ServiceConfig,
            ServiceConfig.id == MediaUserIdentity.source_service_config_id,
        )
    )

    if linked is True:
        stmt = stmt.where(MediaUserIdentity.user_id.isnot(None))
    elif linked is False:
        stmt = stmt.where(MediaUserIdentity.user_id.is_(None))

    query = (q or "").strip().lower()
    if query:
        like_query = f"%{query}%"
        stmt = stmt.where(
            or_(
                func.lower(MediaUserIdentity.username).like(like_query),
                func.lower(MediaUserIdentity.source_user_id).like(like_query),
                func.lower(func.coalesce(MediaUserIdentity.email, "")).like(like_query),
                func.lower(func.coalesce(User.username, "")).like(like_query),
                func.lower(func.coalesce(User.display_name, "")).like(like_query),
                func.lower(func.coalesce(ServiceConfig.name, "")).like(like_query),
            )
        )

    result = await db.execute(
        stmt.order_by(
            MediaUserIdentity.last_seen_at.desc(), MediaUserIdentity.id.desc()
        )
    )
    items = [
        _serialize_media_identity(identity, linked_user, service_name)
        for identity, linked_user, service_name in result.all()
    ]
    return MediaIdentityListResponse(items=items, total=len(items))


@router.post(
    "/users/media-identities/{identity_id}/link",
    response_model=MediaIdentityItem,
)
async def link_media_identity(
    identity_id: int,
    body: MediaIdentityLinkRequest,
    _actor: Annotated[User, Depends(require_permission(Permission.MANAGE_USERS))],
    db: AsyncSession = Depends(get_db),
) -> MediaIdentityItem:
    """Link a media identity to a user account."""
    identity = await db.get(MediaUserIdentity, identity_id)
    if identity is None:
        raise HTTPException(status_code=404, detail="Media identity not found")

    target_user = await db.get(User, body.target_user_id)
    if target_user is None:
        raise HTTPException(status_code=404, detail="Target user not found")

    identity.user_id = target_user.id
    if identity.linked_at is None:
        identity.linked_at = datetime.now(UTC)

    await resolve_singleton_notice(
        db,
        dedupe_key=media_auth_conflict_notice_key_for_source(
            identity.source_service,
            identity.source_service_config_id,
            identity.source_user_id,
        ),
    )
    await db.commit()

    refreshed = await db.execute(
        select(MediaUserIdentity, User, ServiceConfig.name)
        .outerjoin(User, User.id == MediaUserIdentity.user_id)
        .outerjoin(
            ServiceConfig,
            ServiceConfig.id == MediaUserIdentity.source_service_config_id,
        )
        .where(MediaUserIdentity.id == identity_id)
    )
    row = refreshed.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Media identity not found")
    return _serialize_media_identity(row[0], row[1], row[2])


@router.post(
    "/users/media-identities/{identity_id}/unlink",
    response_model=MediaIdentityItem,
)
async def unlink_media_identity(
    identity_id: int,
    _actor: Annotated[User, Depends(require_permission(Permission.MANAGE_USERS))],
    db: AsyncSession = Depends(get_db),
) -> MediaIdentityItem:
    """Unlink a media identity from a user account."""
    identity = await db.get(MediaUserIdentity, identity_id)
    if identity is None:
        raise HTTPException(status_code=404, detail="Media identity not found")

    identity.user_id = None
    await db.commit()

    refreshed = await db.execute(
        select(MediaUserIdentity, User, ServiceConfig.name)
        .outerjoin(User, User.id == MediaUserIdentity.user_id)
        .outerjoin(
            ServiceConfig,
            ServiceConfig.id == MediaUserIdentity.source_service_config_id,
        )
        .where(MediaUserIdentity.id == identity_id)
    )
    row = refreshed.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Media identity not found")
    return _serialize_media_identity(row[0], row[1], row[2])


@router.post("/users", response_model=UserInfo)
async def create_user(
    request: CreateUserRequest,
    actor: Annotated[User, Depends(require_permission(Permission.MANAGE_USERS))],
    db: AsyncSession = Depends(get_db),
) -> UserInfo:
    """Create a user (manage-users/admin)."""
    if actor.role is not UserRole.ADMIN and request.role is UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can grant admin role",
        )

    permissions = [p.value for p in request.permissions]
    if request.role is UserRole.ADMIN:
        allowed_pages = None
    elif request.use_default_page_access:
        allowed_pages = await _default_allowed_pages_for_new_user(db)
    elif request.allowed_pages is None:
        allowed_pages = None
    else:
        allowed_pages = [page.value for page in request.allowed_pages]

    # ensure we don't create a user that already exists
    if request.email:  # if user provided an email, check both username and email
        result = await db.execute(
            select(User).where(
                or_(User.username == request.username, User.email == request.email)
            )
        )
    else:  # only check username
        result = await db.execute(select(User).where(User.username == request.username))
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already exists",
        )

    # generate user
    new_user = User(
        username=request.username,
        password_hash=get_password_hash(request.password),
        email=request.email,
        display_name=request.display_name or request.username,
        role=request.role,
        permissions=permissions,
        allowed_pages=allowed_pages,
        require_password_change=request.require_password_change,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    LOG.info(f"User manager {actor.username} created user {new_user.username}")

    return UserInfo.from_user(new_user)


@router.get("/users", response_model=list[UserInfo])
async def list_users(
    _actor: Annotated[User, Depends(require_permission(Permission.MANAGE_USERS))],
    db: AsyncSession = Depends(get_db),
) -> list[UserInfo]:
    """List all users (manage-users/admin)."""
    result = await db.execute(select(User))
    return [UserInfo.from_user(u) for u in result.scalars().all()]


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    actor: Annotated[User, Depends(require_permission(Permission.MANAGE_USERS))],
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Delete a user (manage-users/admin)."""
    # prevent deleting own account
    if user_id == actor.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    # delete
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    if actor.role is not UserRole.ADMIN and user.role is UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete administrators",
        )

    await db.delete(user)
    await db.commit()

    LOG.info(f"User manager {actor.username} deleted user {user.username}")

    return {"message": "User deleted successfully"}


@router.post("/users/{user_id}")
async def update_user(
    user_id: int,
    request: UpdateUserRequest,
    actor: Annotated[User, Depends(require_permission(Permission.MANAGE_USERS))],
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Update a user (manage-users/admin)."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if request.email:
        email_result = await db.execute(
            select(User).where(User.email == request.email, User.id != user_id)
        )
        if email_result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already in use",
            )

    requested_permissions = [p.value for p in request.permissions]

    if actor.role is not UserRole.ADMIN:
        if user.role is UserRole.ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot modify administrators",
            )
        if request.role is UserRole.ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only administrators can grant admin role",
            )
    user.display_name = request.display_name
    user.email = request.email
    user.role = request.role
    user.permissions = requested_permissions
    user.allowed_pages = (
        None
        if request.role is UserRole.ADMIN or request.allowed_pages is None
        else [page.value for page in request.allowed_pages]
    )

    if request.password:
        user.password_hash = get_password_hash(request.password)
        # invalidate all existing sessions for this user
        user.bump_token_version()
        await _revoke_user_sessions(
            db,
            user_id=user.id,
            reason="password_reset_by_admin",
        )

    await db.commit()

    LOG.info(f"User manager {actor.username} updated user {user.username}")

    return {"message": "User updated successfully"}


@router.get("/sessions", response_model=list[UserSessionInfo])
async def list_sessions(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> list[UserSessionInfo]:
    """List active sessions for the current user."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(UserSession)
        .where(
            UserSession.user_id == current_user.id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
        .order_by(UserSession.last_seen_at.desc(), UserSession.created_at.desc())
    )
    sessions = result.scalars().all()
    current_session_id = get_request_session_id(request)

    return [
        UserSessionInfo(
            session_id=session.session_id,
            user_agent=session.user_agent,
            ip_address=session.ip_address,
            created_at=session.created_at,
            last_seen_at=session.last_seen_at,
            expires_at=session.expires_at,
            is_current=session.session_id == current_session_id,
        )
        for session in sessions
    ]


@router.delete("/sessions/{session_id}", response_model=RevokeSessionResponse)
async def revoke_session(
    session_id: str,
    request: Request,
    response: Response,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> RevokeSessionResponse:
    """Revoke a single session for the current user."""
    result = await db.execute(
        select(UserSession).where(
            UserSession.user_id == current_user.id,
            UserSession.session_id == session_id,
        )
    )
    user_session = result.scalar_one_or_none()
    if user_session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )

    revoked_current = get_request_session_id(request) == session_id
    if user_session.revoked_at is None:
        user_session.revoked_at = datetime.now(UTC)
        user_session.revoked_reason = "user_revoked"
        await db.commit()

    if revoked_current:
        response.delete_cookie(
            key=COOKIE_NAME,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )

    return RevokeSessionResponse(
        message="Session revoked successfully",
        revoked_current=revoked_current,
    )


@router.post("/sessions/revoke-others", response_model=RevokeOtherSessionsResponse)
async def revoke_other_sessions(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> RevokeOtherSessionsResponse:
    """Revoke all other sessions for the current user."""
    current_session_id = get_request_session_id(request)
    if current_session_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current session is not available",
        )

    revoked_count = await _revoke_user_sessions(
        db,
        user_id=current_user.id,
        reason="user_revoked_others",
        exclude_session_id=current_session_id,
    )
    await db.commit()

    return RevokeOtherSessionsResponse(
        message="Other sessions revoked successfully",
        revoked_count=revoked_count,
    )


@router.post("/avatar")
async def upload_avatar(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    avatar: UploadFile = File(...),
) -> dict[str, str]:
    """Upload user avatar."""
    # validate content type
    ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if not avatar.content_type or avatar.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be a JPEG, PNG, GIF, or WebP image",
        )

    # enforce maximum file size (5 MB)
    MAX_SIZE = 5 * 1024 * 1024
    contents = await avatar.read()
    if len(contents) > MAX_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Image file must be smaller than 5 MB",
        )

    # delete old avatar if exists (will be handled in save_picture_from_bytes)
    old_avatar_path = (
        Path(current_user.avatar_path) if current_user.avatar_path else None
    )

    # run CPU-bound image processing in thread pool to avoid blocking event loop
    avatar_filename = await asyncio.to_thread(
        save_picture_from_bytes,
        contents,
        avatar.filename or "avatar.jpg",
        old_avatar_path,
    )

    current_user.avatar_path = avatar_filename
    await db.commit()

    LOG.info(f"User {current_user.username} uploaded avatar: {avatar_filename}")

    return {
        "message": "Avatar uploaded successfully",
        "path": avatar_filename,
    }
