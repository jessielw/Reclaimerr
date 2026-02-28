from fastapi import APIRouter

from .general import router as general_router
from .notifications import router as notifications_router
from .services import router as services_router

router = APIRouter(prefix="/api/settings", tags=["settings"])
router.include_router(general_router)
router.include_router(services_router)
router.include_router(notifications_router)
