import asyncio
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import (
    COOKIE_NAME,
    get_current_user,
    get_password_hash,
    require_permission,
    verify_password,
)
from backend.core.logger import LOG
from backend.core.settings import settings
from backend.core.utils.image_handling import save_picture_from_bytes
from backend.database import get_db
from backend.database.models import User
from backend.enums import Permission, UserRole
from backend.models.auth import (
    ChangePasswordRequest,
    ChangeProfileInfoRequest,
    CreateUserRequest,
    UpdateUserRequest,
    UserInfo,
)

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("/me", response_model=UserInfo)
async def get_me(current_user: Annotated[User, Depends(get_current_user)]):
    """Get current user info."""
    return UserInfo.from_user(current_user)


@router.post("/me")
async def update_profile(
    new_info: ChangeProfileInfoRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
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
):
    """Change password."""
    if not current_user.require_password_change:
        if not request.old_password or not verify_password(
            request.old_password, current_user.password_hash
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid old password"
            )

    current_user.password_hash = get_password_hash(request.new_password)
    current_user.require_password_change = False
    current_user.bump_token_version()
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


@router.post("/users", response_model=UserInfo)
async def create_user(
    request: CreateUserRequest,
    actor: Annotated[User, Depends(require_permission(Permission.MANAGE_USERS))],
    db: AsyncSession = Depends(get_db),
):
    """Create a user (manage-users/admin)."""
    if actor.role is not UserRole.ADMIN and request.role is UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can grant admin role",
        )

    permissions = [p.value for p in request.permissions]

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
):
    """List all users (manage-users/admin)."""
    result = await db.execute(select(User))
    return [UserInfo.from_user(u) for u in result.scalars().all()]


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    actor: Annotated[User, Depends(require_permission(Permission.MANAGE_USERS))],
    db: AsyncSession = Depends(get_db),
):
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
):
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

    if request.password:
        user.password_hash = get_password_hash(request.password)
        # invalidate all existing sessions for this user
        user.bump_token_version()

    await db.commit()

    LOG.info(f"User manager {actor.username} updated user {user.username}")

    return {"message": "User updated successfully"}


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
