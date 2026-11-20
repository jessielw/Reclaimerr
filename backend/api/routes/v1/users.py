"""Scoped account lookup and media-server account import."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.api_tokens import ApiPrincipal, require_api_scope
from backend.database import get_db
from backend.database.models import User
from backend.models.api_v1 import UserAccountResponse
from backend.models.media_user_import import (
    MediaImportProvider,
    MediaUserImportRequest,
    MediaUserImportResponse,
)
from backend.services.media_user_identity import linked_media_identities
from backend.services.media_user_import import (
    account_summary,
    import_media_users,
    import_providers,
)

router = APIRouter(prefix="/users", tags=["v1:users"])


@router.get("", response_model=list[UserAccountResponse])
async def list_users(
    _principal: Annotated[ApiPrincipal, Depends(require_api_scope("users:read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = Query(default=None, max_length=255),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[UserAccountResponse]:
    stmt = select(User)
    if q and q.strip():
        query = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(User.username).like(query),
                func.lower(User.email).like(query),
                func.lower(User.display_name).like(query),
            )
        )
    users = (
        (await db.execute(stmt.order_by(User.id).offset(offset).limit(limit)))
        .scalars()
        .all()
    )
    identities = await linked_media_identities(db, (user.id for user in users))
    return [
        UserAccountResponse(
            **account_summary(user).model_dump(),
            media_identities=identities[user.id],
        )
        for user in users
    ]


@router.get(
    "/import-from-media-server/providers", response_model=list[MediaImportProvider]
)
async def media_import_providers(
    _principal: Annotated[ApiPrincipal, Depends(require_api_scope("users:read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[MediaImportProvider]:
    return await import_providers(db)


@router.post("/import-from-media-server", response_model=MediaUserImportResponse)
async def import_users_from_media_server(
    body: MediaUserImportRequest,
    principal: Annotated[ApiPrincipal, Depends(require_api_scope("users:read"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    preview: bool = Query(default=True),
) -> MediaUserImportResponse:
    if not preview and not principal.has_scope("users:manage"):
        raise HTTPException(
            status_code=403, detail="API token requires the users:manage scope"
        )
    return await import_media_users(
        db,
        body,
        preview=preview,
        actor=None,
        actor_label=f"API token {principal.token_id}",
    )
