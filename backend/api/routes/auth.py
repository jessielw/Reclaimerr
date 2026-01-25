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


# @router.post("/link-jellyfin")
# async def link_jellyfin(
#     request: LinkJellyfinRequest,
#     current_user: Annotated[User, Depends(get_current_user)],
#     db: AsyncSession = Depends(get_db),
# ):
#     """Link Jellyfin account (optional)."""
#     result = await db.execute(
#         select(ServiceConfig).where(
#             ServiceConfig.service_type == Service.JELLYFIN,
#             ServiceConfig.enabled == True,
#         )
#     )
#     config = result.scalar_one_or_none()

#     if not config:
#         raise HTTPException(
#             status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
#             detail="Jellyfin not configured",
#         )

#     try:
#         async with niquests.AsyncSession() as session:
#             response = await session.post(
#                 f"{config.base_url}/Users/AuthenticateByName",
#                 json={"Username": request.username, "Pw": request.password},
#                 headers={
#                     "Content-Type": "application/json",
#                     "X-Emby-Authorization": 'MediaBrowser Client="vacuumerr", Device="Web", DeviceId="vacuumerr", Version="1.0.0"',
#                 },
#             )
#             response.raise_for_status()
#             data = response.json()

#             current_user.jellyfin_id = data["User"]["Id"]
#             current_user.jellyfin_token = data["AccessToken"]
#             await db.commit()

#             return {"message": "Jellyfin linked successfully"}
#     except Exception as e:
#         LOG.error(f"Jellyfin linking failed: {e}")
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Invalid Jellyfin credentials",
#         )
