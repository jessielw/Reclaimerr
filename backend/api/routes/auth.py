from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import (
    COOKIE_NAME,
    SESSION_TTL_SECONDS,
    build_session_expires_at,
    create_access_token,
    decode_token,
    generate_session_id,
    get_request_client_ip,
    get_request_user_agent,
    get_session_id_from_payload,
    verify_password,
)
from backend.core.settings import settings
from backend.database import get_db
from backend.database.models import User, UserSession
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

    session_id = generate_session_id()
    now = datetime.now(UTC)
    user.last_login_at = now
    user_session = UserSession(
        user_id=user.id,
        session_id=session_id,
        expires_at=build_session_expires_at(),
        user_agent=get_request_user_agent(request),
        ip_address=get_request_client_ip(request),
        last_seen_at=now,
    )
    db.add(user_session)

    await db.commit()
    await db.refresh(user)

    access_token = create_access_token(
        data={"sub": str(user.id)},
        token_version=user.token_version,
        session_id=session_id,
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
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Logout and clear authentication cookie."""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            payload = decode_token(token)
            session_id = get_session_id_from_payload(payload)
            if session_id is not None:
                result = await db.execute(
                    select(UserSession).where(UserSession.session_id == session_id)
                )
                current_session = result.scalar_one_or_none()
                if current_session is not None and current_session.revoked_at is None:
                    current_session.revoked_at = datetime.now(UTC)
                    current_session.revoked_reason = "logout"
                    await db.commit()
        except HTTPException:
            # invalid/expired tokens are effectively logged out once cookie is removed
            pass

    response.delete_cookie(
        key=COOKIE_NAME,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"message": "Logged out successfully"}
