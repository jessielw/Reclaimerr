from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import get_password_hash
from backend.core.logger import LOG
from backend.core.setup_state import setup_state
from backend.database import get_db
from backend.database.models import User
from backend.enums import UserRole
from backend.models.setup import SetupRequest, SetupStatusResponse

router = APIRouter(prefix="/api/setup", tags=["setup"])


@router.get("/status", response_model=SetupStatusResponse)
async def setup_status():
    """Return whether initial setup is still required."""
    return SetupStatusResponse(needs_setup=setup_state.needs_setup)


@router.post("", status_code=status.HTTP_201_CREATED)
async def complete_setup(
    body: SetupRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create the initial admin account. Only callable when setup is pending."""
    if not setup_state.needs_setup:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Setup has already been completed.",
        )

    body.validate_fields()

    admin = User(
        username="admin",
        password_hash=get_password_hash(body.password),
        display_name="Admin",
        role=UserRole.ADMIN,
    )
    db.add(admin)
    await db.commit()

    setup_state.needs_setup = False
    LOG.info("Initial admin created via setup wizard")
    return {"detail": "Setup complete. You can now log in."}
