from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from backend.api.routes import config, radarr
from backend.api.routes.account import router as account_router
from backend.api.routes.auth import router as auth_router
from backend.api.routes.dashboard import router as dashboard_router
from backend.api.routes.tasks import router as tasks_router
from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.settings import settings
from backend.database import close_db, get_db, init_db
from backend.database.models import ServiceConfig
from backend.enums import Service
from backend.scheduler import shutdown_scheduler, start_scheduler
from backend.utils.create_admin import create_initial_admin


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize client manager on startup."""
    LOG.info("Starting vacuumerr API server")
    LOG.info(f"Log level: {settings.log_level_enum}")

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
                    config.base_url, config.api_key
                )
            elif config.service_type is Service.PLEX:
                await service_manager.initialize_plex(config.base_url, config.api_key)
            elif config.service_type is Service.RADARR:
                await service_manager.initialize_radarr(config.base_url, config.api_key)
            elif config.service_type is Service.SONARR:
                await service_manager.initialize_sonarr(config.base_url, config.api_key)
            elif config.service_type is Service.SEERR:
                await service_manager.initialize_seerr(config.base_url, config.api_key)

        break  # Only need first iteration to get db session

    # start scheduler
    start_scheduler()

    LOG.info("vacuumerr API ready")

    try:
        yield
    finally:
        # Cleanup - this runs on both normal shutdown and signal interruption
        LOG.info("Shutting down vacuumerr API")

        # Stop scheduler and wait for jobs to complete (with timeout)
        await shutdown_scheduler()

        # Close all service HTTP sessions
        await service_manager.clear_all()

        # Close database connections
        await close_db()

        LOG.info("vacuumerr API shutdown complete")


app = FastAPI(
    title="vacuumerr API",
    description="Media server cleanup and deletion management tool",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware for Svelte frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all exception handler with logging."""
    LOG.exception(f"Unhandled exception on {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "path": str(request.url.path)},
    )


# routers
app.include_router(dashboard_router)
app.include_router(auth_router)
app.include_router(account_router)
app.include_router(config.router)
app.include_router(radarr.router)
app.include_router(tasks_router)


# mount static files LAST - after all routes
app.mount("/static", StaticFiles(directory=settings.static_dir_path), name="static")
app.mount("/avatars", StaticFiles(directory=settings.avatars_dir_path), name="avatars")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
