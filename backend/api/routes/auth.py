from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import create_access_token, verify_password
from backend.database import get_db
from backend.database.models import User
from backend.models.auth import (
    AuthResponse,
    LoginRequest,
    UserInfo,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=AuthResponse)
async def login(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Login with username and password."""
    result = await db.execute(select(User).where(User.username == request.username))
    user = result.scalar_one_or_none()

    if (
        not user
        or not user.password_hash
        or not verify_password(request.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled"
        )

    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user)

    access_token = create_access_token(data={"sub": str(user.id)})

    return AuthResponse(
        access_token=access_token,
        user=UserInfo(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            email=user.email,
            avatar_path=user.avatar_path,
            role=user.role,
            created_at=user.created_at,
            require_password_change=user.require_password_change or False,
        ),
    )
