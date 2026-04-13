from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import (
    COOKIE_NAME,
    SESSION_TTL_SECONDS,
    create_access_token,
    verify_password,
)
from backend.core.settings import settings
from backend.database import get_db
from backend.database.models import User
from backend.models.auth import (
    AuthResponse,
    LoginRequest,
    UserInfo,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)


@router.post("/login", response_model=AuthResponse)
@limiter.limit("5/minute")
async def login(
    body: LoginRequest,
    # 'request' cannot be prefixed with a _ or it will break the rate limiter which uses the request
    # object internally
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Login with username and password."""
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()

    if (
        not user
        or not user.password_hash
        or not verify_password(body.password, user.password_hash)
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

    access_token = create_access_token(
        data={"sub": str(user.id)},
        token_version=user.token_version,
    )

    # set HttpOnly cookie
    response.set_cookie(
        key=COOKIE_NAME,
        value=access_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,  # 24 hours, matches JWT TTL
        path="/",
    )

    return AuthResponse(
        user=UserInfo.from_user(user),
    )


@router.post("/logout")
async def logout(response: Response):
    """Logout and clear authentication cookie."""
    response.delete_cookie(
        key=COOKIE_NAME,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"message": "Logged out successfully"}
