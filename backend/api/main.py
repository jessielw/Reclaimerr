from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from granian.utils.proxies import wrap_asgi_with_proxy_headers
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.api.routes.account import router as account_router
from backend.api.routes.auth import router as auth_router
from backend.api.routes.background_jobs import router as background_jobs_router
from backend.api.routes.dashboard import router as dashboard_router
from backend.api.routes.delete_requests import router as delete_requests_router
from backend.api.routes.info import router as info_router
from backend.api.routes.media import router as media_router
from backend.api.routes.protected import router as protected_router
from backend.api.routes.requests import router as requests_router
from backend.api.routes.rules import router as rules_router
from backend.api.routes.settings import router as settings_router
from backend.api.routes.setup import router as setup_router
from backend.api.routes.system import router as system_router
from backend.api.routes.tasks import router as tasks_router
from backend.api.utils.exception_handlers import register_exception_handlers
from backend.api.utils.middleware import (
    cors_middleware,
    oidc_session_middleware,
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
from backend.enums import BackgroundJobType
from backend.jobs import reset_stale_jobs
from backend.scheduler import shutdown_scheduler, start_scheduler
from backend.utils.create_admin import create_initial_admin

limiter = Limiter(key_func=get_remote_address)


def _wrap_proxy_headers(
    asgi_app: ASGIApp, *, trusted_hosts: list[str] | str
) -> ASGIApp:
    """Return an ASGI3-compatible proxy-header wrapper for the given app."""
    wrapped_app = wrap_asgi_with_proxy_headers(
        asgi_app,
        trusted_hosts=trusted_hosts,
    )

    async def _asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await wrapped_app(scope, receive, send)

    return _asgi


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize client manager on startup."""
    worker_tasks: list[asyncio.Task[Any]] = []
    scheduler_started = False

    try:
        LOG.set_log_level(settings.log_level_enum)
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

        # reset any jobs left in RUNNING state from a previous process
        await reset_stale_jobs()

        # background workers (in process)
        host_pid = f"{socket.gethostname()}:{os.getpid()}"
        worker_tasks = [
            asyncio.create_task(
                worker_loop(
                    f"{host_pid}:default",
                    allowed_job_types={
                        BackgroundJobType.SERVICE_TOGGLE,
                        BackgroundJobType.TASK_RUN,
                    },
                ),
                name="background-worker-default",
            ),
            asyncio.create_task(
                worker_loop(
                    f"{host_pid}:file-ops",
                    allowed_job_types={BackgroundJobType.CANDIDATE_FILE_OP},
                ),
                name="background-worker-file-ops",
            ),
        ]

        LOG.info("reclaimerr API ready")

        yield

    except Exception as e:
        LOG.exception(f"Error in API lifespan: {e}")
        raise
    finally:
        LOG.info("Shutting down reclaimerr API")

        for worker_task in worker_tasks:
            worker_task.cancel()
        for worker_task in worker_tasks:
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


fastapi_app = FastAPI(
    title="reclaimerr API",
    description="Media server cleanup and deletion management tool",
    version="0.1.6",
    lifespan=lifespan,
)

# register exception handlers
register_exception_handlers(fastapi_app)

# setup limiter
fastapi_app.state.limiter = limiter

# add middleware (order matters: outermost middleware added last)
cors_middleware(fastapi_app)
security_headers_middleware(fastapi_app)
oidc_session_middleware(fastapi_app)
sliding_session_middleware(fastapi_app)
setup_guard_middleware(fastapi_app)

# routers
fastapi_app.include_router(setup_router)
fastapi_app.include_router(info_router)
fastapi_app.include_router(settings_router)
fastapi_app.include_router(dashboard_router)
fastapi_app.include_router(auth_router)
fastapi_app.include_router(rules_router)
fastapi_app.include_router(account_router)
fastapi_app.include_router(tasks_router)
fastapi_app.include_router(background_jobs_router)
fastapi_app.include_router(media_router)
fastapi_app.include_router(requests_router)
fastapi_app.include_router(delete_requests_router)
fastapi_app.include_router(protected_router)
fastapi_app.include_router(system_router)


# mount static files LAST
fastapi_app.mount(
    "/static", StaticFiles(directory=settings.static_dir_path), name="static"
)
fastapi_app.mount(
    "/avatars", StaticFiles(directory=settings.avatars_dir_path), name="avatars"
)

# serve built frontend (desktop mode) when FRONTEND_DIST is set
if settings.frontend_dist and settings.frontend_dist.is_dir():
    fe_dist = settings.frontend_dist
    fastapi_app.mount(
        "/assets", StaticFiles(directory=fe_dist / "assets"), name="frontend-assets"
    )

    @fastapi_app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str) -> FileResponse:
        candidate = fe_dist / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(fe_dist / "index.html")


app = _wrap_proxy_headers(
    fastapi_app.__call__,
    trusted_hosts=settings.proxy_trusted_hosts_list,
)
