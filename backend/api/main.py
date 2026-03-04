from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import select

from backend.api.routes.account import router as account_router
from backend.api.routes.auth import router as auth_router
from backend.api.routes.blacklist import router as blacklist_router
from backend.api.routes.dashboard import router as dashboard_router
from backend.api.routes.info import router as info_router
from backend.api.routes.media import router as media_router
from backend.api.routes.requests import router as requests_router
from backend.api.routes.rules import router as rules_router
from backend.api.routes.settings import router as settings_router
from backend.api.routes.tasks import router as tasks_router
from backend.api.utils.exception_handlers import register_exception_handlers
from backend.api.utils.middleware import (
    cors_middleware,
    security_headers_middleware,
    sliding_session_middleware,
)
from backend.core.encryption import fer_decrypt
from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.settings import settings
from backend.database import close_db, get_db, init_db
from backend.database.models import ServiceConfig
from backend.enums import Service
from backend.scheduler import shutdown_scheduler, start_scheduler
from backend.utils.create_admin import create_initial_admin

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize client manager on startup."""
    LOG.info("Starting reclaimerr API server")
    LOG.info(f"Log level: {settings.log_level_enum}")

    if settings.cors_origins == "*":
        LOG.warning(
            "CORS is configured to allow ALL origins ('*'). "
            "This is insecure for production. Set CORS_ORIGINS to your frontend URL."
        )

    # # Setup signal handlers for graceful shutdown # TODO: potentially?
    # def signal_handler(signum, frame):
    #     LOG.warning(f"Received signal {signum}, initiating graceful shutdown...")
    #     # The actual cleanup will happen in the finally block below
    #     sys.exit(0)

    # # Register signal handlers (SIGINT = Ctrl+C, SIGTERM = Docker stop)
    # signal.signal(signal.SIGINT, signal_handler)
    # signal.signal(signal.SIGTERM, signal_handler)

    # init db
    await init_db()

    # check if admin account needs created
    await create_initial_admin()

    # Load service configs from database and initialize clients
    async for db in get_db():
        result = await db.execute(
            select(ServiceConfig).where(ServiceConfig.enabled == True)
        )
        service_configs = result.scalars().all()

        for config in service_configs:
            if config.service_type is Service.JELLYFIN:
                await service_manager.initialize_jellyfin(
                    config.base_url, fer_decrypt(config.api_key)
                )
            elif config.service_type is Service.PLEX:
                await service_manager.initialize_plex(
                    config.base_url, fer_decrypt(config.api_key)
                )
            elif config.service_type is Service.RADARR:
                await service_manager.initialize_radarr(
                    config.base_url, fer_decrypt(config.api_key)
                )
            elif config.service_type is Service.SONARR:
                await service_manager.initialize_sonarr(
                    config.base_url, fer_decrypt(config.api_key)
                )
            elif config.service_type is Service.SEERR:
                await service_manager.initialize_seerr(
                    config.base_url, fer_decrypt(config.api_key)
                )

        break  # Only need first iteration to get db session

    # start scheduler
    await start_scheduler()

    LOG.info("reclaimerr API ready")

    try:
        yield
    finally:
        # Cleanup - this runs on both normal shutdown and signal interruption
        LOG.info("Shutting down reclaimerr API")

        # Stop scheduler and wait for jobs to complete (with timeout)
        await shutdown_scheduler()

        # Close all service HTTP sessions
        await service_manager.clear_all()

        # Close database connections
        await close_db()

        LOG.info("reclaimerr API shutdown complete")


app = FastAPI(
    title="reclaimerr API",
    description="Media server cleanup and deletion management tool",
    version="0.1.0",
    lifespan=lifespan,
)

# register exception handlers
register_exception_handlers(app)

# setup limiter
app.state.limiter = limiter

# add middleware
cors_middleware(app)
security_headers_middleware(app)
sliding_session_middleware(app)

# routers
app.include_router(info_router)
app.include_router(settings_router)
app.include_router(dashboard_router)
app.include_router(auth_router)
app.include_router(rules_router)
app.include_router(account_router)
app.include_router(tasks_router)
app.include_router(media_router)
app.include_router(requests_router)
app.include_router(blacklist_router)


# mount static files LAST - after all routes
app.mount("/static", StaticFiles(directory=settings.static_dir_path), name="static")
app.mount("/avatars", StaticFiles(directory=settings.avatars_dir_path), name="avatars")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
