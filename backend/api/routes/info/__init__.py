from fastapi import APIRouter

from .alerts import router as alerts_router
from .info import router as info_router
from .notices import router as notices_router

router = APIRouter(prefix="/api/info", tags=["info"])
router.include_router(info_router)
router.include_router(alerts_router)
router.include_router(notices_router)
