from __future__ import annotations

import asyncio
import os
import socket
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.util import get_remote_address

from backend.api.routes.account import router as account_router
from backend.api.routes.auth import router as auth_router
from backend.api.routes.background_jobs import router as background_jobs_router
from backend.api.routes.dashboard import router as dashboard_router
from backend.api.routes.info import router as info_router
from backend.api.routes.media import router as media_router
from backend.api.routes.protected import router as protected_router
from backend.api.routes.requests import router as requests_router
from backend.api.routes.rules import router as rules_router
from backend.api.routes.settings import router as settings_router
from backend.api.routes.setup import router as setup_router
from backend.api.routes.tasks import router as tasks_router
from backend.api.utils.exception_handlers import register_exception_handlers
from backend.api.utils.middleware import (
    cors_middleware,
    security_headers_middleware,
    setup_guard_middleware,
    sliding_session_middleware,
)
from backend.core.logger import LOG
from backend.core.service_bootstrap import load_enabled_services
from backend.core.service_manager import service_manager
from backend.core.settings import settings
from backend.core.worker import worker_loop
from backend.database import close_db, init_db
from backend.scheduler import shutdown_scheduler, start_scheduler
from backend.utils.create_admin import create_initial_admin

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize client manager on startup."""
    worker_task: asyncio.Task | None = None
    scheduler_started = False

    try:
        LOG.info("Starting reclaimerr API server")
        LOG.info(f"Log level: {settings.log_level_enum}")

        if settings.cors_origins == "*":
            LOG.warning(
                "CORS is configured to allow ALL origins ('*'). "
                "This is insecure for production. Set CORS_ORIGINS to your frontend URL."
            )

        # init db
        await init_db()

        # check if admin account needs created
        await create_initial_admin()

        # load service configs from database and initialize clients
        await load_enabled_services()

        # start scheduler
        await start_scheduler()
        scheduler_started = True

        # start in-process background worker
        _worker_id = f"{socket.gethostname()}:{os.getpid()}"
        worker_task = asyncio.create_task(
            worker_loop(_worker_id), name="background-worker"
        )

        LOG.info("reclaimerr API ready")

        yield

    except Exception as e:
        LOG.exception(f"Error in API lifespan: {e}")
        raise
    finally:
        LOG.info("Shutting down reclaimerr API")

        if worker_task is not None:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

        if scheduler_started:
            await shutdown_scheduler()

        await service_manager.clear_all()
        await close_db()

        LOG.info("reclaimerr API shutdown complete")
        LOG.stop()  # flush and join the logging background thread


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

# add middleware (order matters: outermost middleware added last)
cors_middleware(app)
security_headers_middleware(app)
sliding_session_middleware(app)
setup_guard_middleware(app)

# routers
app.include_router(setup_router)
app.include_router(info_router)
app.include_router(settings_router)
app.include_router(dashboard_router)
app.include_router(auth_router)
app.include_router(rules_router)
app.include_router(account_router)
app.include_router(tasks_router)
app.include_router(background_jobs_router)
app.include_router(media_router)
app.include_router(requests_router)
app.include_router(protected_router)


# mount static files LAST
app.mount("/static", StaticFiles(directory=settings.static_dir_path), name="static")
app.mount("/avatars", StaticFiles(directory=settings.avatars_dir_path), name="avatars")

# serve built frontend (desktop mode) when FRONTEND_DIST is set
if settings.frontend_dist and settings.frontend_dist.is_dir():
    fe_dist = settings.frontend_dist
    app.mount(
        "/assets", StaticFiles(directory=fe_dist / "assets"), name="frontend-assets"
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        candidate = fe_dist / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(fe_dist / "index.html")
