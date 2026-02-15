import asyncio
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import (
    get_current_user,
    get_password_hash,
    require_admin,
    verify_password,
)
from backend.core.logger import LOG
from backend.core.utils.image_handling import save_picture_from_bytes
from backend.database import get_db
from backend.database.models import User
from backend.models.auth import (
    ChangePasswordRequest,
    ChangeProfileInfoRequest,
    CreateUserRequest,
    UserInfo,
)

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("/me", response_model=UserInfo)
async def get_me(current_user: Annotated[User, Depends(get_current_user)]):
    """Get current user info."""
    return UserInfo(
        id=current_user.id,
        username=current_user.username,
        display_name=current_user.display_name,
        email=current_user.email,
        avatar_path=current_user.avatar_path,
        role=current_user.role,
        created_at=current_user.created_at,
        require_password_change=current_user.require_password_change or False,
    )


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
    db: AsyncSession = Depends(get_db),
):
    """Change password."""
    if not current_user.require_password_change and request.old_password:
        if not current_user.password_hash or not verify_password(
            request.old_password, current_user.password_hash
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid old password"
            )

    current_user.password_hash = get_password_hash(request.new_password)
    current_user.require_password_change = False
    await db.commit()

    return {"message": "Password changed successfully"}


@router.post("/users", response_model=UserInfo)
async def create_user(
    request: CreateUserRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Admin creates a user."""
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
        display_name=request.username,
        role=request.role,
        require_password_change=request.require_password_change,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    LOG.info(f"Admin {admin.username} created user {new_user.username}")

    return UserInfo(
        id=new_user.id,
        username=new_user.username,
        display_name=new_user.display_name,
        email=new_user.email,
        avatar_path=new_user.avatar_path,
        role=new_user.role,
        created_at=new_user.created_at,
        require_password_change=new_user.require_password_change or False,
    )


@router.get("/users", response_model=list[UserInfo])
async def list_users(
    _admin: Annotated[User, Depends(require_admin)], db: AsyncSession = Depends(get_db)
):
    """Admin lists all users."""
    result = await db.execute(select(User))
    return [
        UserInfo(
            id=u.id,
            username=u.username,
            display_name=u.display_name,
            email=u.email,
            avatar_path=u.avatar_path,
            role=u.role,
            created_at=u.created_at,
            require_password_change=u.require_password_change or False,
        )
        for u in result.scalars().all()
    ]


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Admin deletes a user."""
    # prevent deleting own account
    if user_id == admin.id:
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
    await db.delete(user)
    await db.commit()

    LOG.info(f"Admin {admin.username} deleted user {user.username}")

    return {"message": "User deleted successfully"}


@router.post("/users/{user_id}")
async def update_user(
    user_id: int,
    admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Admin updates a user."""
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="User update not yet implemented",
    )


@router.post("/avatar")
async def upload_avatar(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    avatar: UploadFile = File(...),
):
    """Upload user avatar."""
    # validate file type
    if not avatar.content_type or not avatar.content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be an image",
        )

    # read file contents
    contents = await avatar.read()

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
        "avatar_path": avatar_filename,
    }
