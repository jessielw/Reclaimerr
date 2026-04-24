from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import get_current_user
from backend.core.utils.datetime_utils import to_utc_isoformat
from backend.core.utils.file_utils import bytes_to_gb
from backend.database import get_db
from backend.database.models import (
    Movie,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
    Series,
    ServiceConfig,
    TaskRun,
    User,
)
from backend.enums import (
    MediaType,
    ProtectionRequestStatus,
    Service,
    Task,
    TaskStatus,
    UserRole,
)
from backend.models.dashboard import (
    DashboardActivityItem,
    DashboardKpis,
    DashboardRequestsSummary,
    DashboardResponse,
    DashboardServiceSummary,
    DashboardViewer,
)
from backend.types import MEDIA_SERVERS

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Role aware dashboard summary."""
    now = datetime.now(UTC)
    seven_days_ago = now - timedelta(days=7)
    is_admin = current_user.role is UserRole.ADMIN

    summary_row = (
        await db.execute(
            select(
                select(func.count())
                .select_from(Movie)
                .scalar_subquery()
                .label("movie_count"),
                select(func.count())
                .select_from(Series)
                .scalar_subquery()
                .label("series_count"),
                select(func.coalesce(func.sum(Movie.size), 0))
                .select_from(ReclaimCandidate)
                .join(Movie, ReclaimCandidate.movie_id == Movie.id)
                .where(ReclaimCandidate.media_type == MediaType.MOVIE)
                .scalar_subquery()
                .label("movie_size_total"),
                select(func.coalesce(func.sum(Series.size), 0))
                .select_from(ReclaimCandidate)
                .join(Series, ReclaimCandidate.series_id == Series.id)
                .where(ReclaimCandidate.media_type == MediaType.SERIES)
                .scalar_subquery()
                .label("series_size_total"),
                select(func.coalesce(func.sum(Movie.size), 0))
                .select_from(Movie)
                .where(Movie.removed_at.is_(None))
                .scalar_subquery()
                .label("all_movies_size"),
                select(func.coalesce(func.sum(Series.size), 0))
                .select_from(Series)
                .where(Series.removed_at.is_(None))
                .scalar_subquery()
                .label("all_series_size"),
                select(func.count())
                .select_from(ProtectionRequest)
                .where(ProtectionRequest.status == ProtectionRequestStatus.PENDING)
                .scalar_subquery()
                .label("pending_requests"),
                select(func.count())
                .select_from(ProtectionRequest)
                .where(
                    ProtectionRequest.status == ProtectionRequestStatus.APPROVED,
                    ProtectionRequest.reviewed_at.is_not(None),
                    ProtectionRequest.reviewed_at >= seven_days_ago,
                )
                .scalar_subquery()
                .label("approved_7d"),
                select(func.count())
                .select_from(ProtectionRequest)
                .where(
                    ProtectionRequest.status == ProtectionRequestStatus.DENIED,
                    ProtectionRequest.reviewed_at.is_not(None),
                    ProtectionRequest.reviewed_at >= seven_days_ago,
                )
                .scalar_subquery()
                .label("denied_7d"),
                select(func.count())
                .select_from(ProtectionRequest)
                .where(
                    ProtectionRequest.requested_by_user_id == current_user.id,
                    ProtectionRequest.status == ProtectionRequestStatus.PENDING,
                )
                .scalar_subquery()
                .label("mine_pending"),
                select(func.count())
                .select_from(ProtectionRequest)
                .where(
                    ProtectionRequest.requested_by_user_id == current_user.id,
                    ProtectionRequest.status == ProtectionRequestStatus.APPROVED,
                    or_(
                        ProtectionRequest.requested_expires_at.is_(None),
                        ProtectionRequest.requested_expires_at >= now,
                    ),
                )
                .scalar_subquery()
                .label("mine_active"),
                select(func.count())
                .select_from(ServiceConfig)
                .where(
                    ServiceConfig.service_type.in_(MEDIA_SERVERS),
                    ServiceConfig.enabled.is_(True),
                )
                .scalar_subquery()
                .label("media_server_count"),
            )
        )
    ).one()

    movie_count = summary_row.movie_count or 0
    series_count = summary_row.series_count or 0
    movie_size_total = summary_row.movie_size_total or 0
    series_size_total = summary_row.series_size_total or 0
    all_movies_size = summary_row.all_movies_size or 0
    all_series_size = summary_row.all_series_size or 0
    pending_requests = summary_row.pending_requests or 0
    approved_7d = summary_row.approved_7d or 0
    denied_7d = summary_row.denied_7d or 0
    mine_pending = summary_row.mine_pending or 0
    mine_active = summary_row.mine_active or 0
    media_server_configured = (summary_row.media_server_count or 0) > 0

    services: list[DashboardServiceSummary] = []
    if is_admin:
        # get last completed SYNC_MEDIA run (single unified sync task)
        last_sync_result = await db.execute(
            select(func.max(TaskRun.completed_at)).where(
                TaskRun.task == Task.SYNC_MEDIA,
                TaskRun.status == TaskStatus.COMPLETED,
            )
        )
        last_sync_at: datetime | None = last_sync_result.scalar_one_or_none()

        # get all service configs
        service_config_rows = (
            await db.execute(
                select(
                    ServiceConfig.service_type,
                    ServiceConfig.enabled,
                    ServiceConfig.base_url,
                )
            )
        ).all()

        service_conf = {
            service_type: {"enabled": enabled, "base_url": base_url or ""}
            for service_type, enabled, base_url in service_config_rows
        }

        services = [
            DashboardServiceSummary(
                name=service.value,
                url=service_conf.get(service, {}).get("base_url", ""),
                enabled=service_conf.get(service, {}).get("enabled", False),
                last_sync_at=to_utc_isoformat(last_sync_at)
                if service in MEDIA_SERVERS
                else None,
            )
            for service in sorted(Service, key=lambda s: s.value)
        ]

    request_activity = (
        select(
            literal("request").label("activity_type"),
            ProtectionRequest.id.label("source_id"),
            ProtectionRequest.created_at.label("created_at"),
            ProtectionRequest.status.label("request_status"),
            literal(None, type_=TaskRun.task.type).label("task"),
            literal(None, type_=TaskRun.status.type).label("task_status"),
            literal(None, type_=TaskRun.items_processed.type).label("items_processed"),
            ProtectionRequest.media_type.label("media_type"),
            Movie.title.label("movie_title"),
            Series.title.label("series_title"),
            User.username.label("username"),
            User.display_name.label("display_name"),
        )
        .outerjoin(User, User.id == ProtectionRequest.requested_by_user_id)
        .outerjoin(Movie, Movie.id == ProtectionRequest.movie_id)
        .outerjoin(Series, Series.id == ProtectionRequest.series_id)
    )
    if not is_admin:
        request_activity = request_activity.where(
            ProtectionRequest.requested_by_user_id == current_user.id
        )

    task_activity = select(
        literal("task").label("activity_type"),
        TaskRun.id.label("source_id"),
        TaskRun.created_at.label("created_at"),
        literal(None, type_=ProtectionRequest.status.type).label("request_status"),
        TaskRun.task.label("task"),
        TaskRun.status.label("task_status"),
        TaskRun.items_processed.label("items_processed"),
        literal(None, type_=ProtectionRequest.media_type.type).label("media_type"),
        literal(None, type_=Movie.title.type).label("movie_title"),
        literal(None, type_=Series.title.type).label("series_title"),
        literal(None, type_=User.username.type).label("username"),
        literal(None, type_=User.display_name.type).label("display_name"),
    )

    activity_parts = [request_activity, task_activity]

    if is_admin:
        protected_activity = (
            select(
                literal("protected").label("activity_type"),
                ProtectedMedia.id.label("source_id"),
                ProtectedMedia.created_at.label("created_at"),
                literal(None, type_=ProtectionRequest.status.type).label(
                    "request_status"
                ),
                literal(None, type_=TaskRun.task.type).label("task"),
                literal(None, type_=TaskRun.status.type).label("task_status"),
                literal(None, type_=TaskRun.items_processed.type).label(
                    "items_processed"
                ),
                ProtectedMedia.media_type.label("media_type"),
                Movie.title.label("movie_title"),
                Series.title.label("series_title"),
                User.username.label("username"),
                User.display_name.label("display_name"),
            )
            .outerjoin(User, User.id == ProtectedMedia.protected_by_user_id)
            .outerjoin(Movie, Movie.id == ProtectedMedia.movie_id)
            .outerjoin(Series, Series.id == ProtectedMedia.series_id)
        )
        activity_parts.append(protected_activity)

    activity_union = union_all(*activity_parts).subquery()
    activity_rows = (
        await db.execute(
            select(activity_union)
            .order_by(activity_union.c.created_at.desc())
            .limit(20)
        )
    ).all()

    activity: list[DashboardActivityItem] = []
    for row in activity_rows:
        actor_display = row.display_name or row.username if row.username else None
        media_title = (
            row.movie_title if row.media_type == MediaType.MOVIE else row.series_title
        )

        if row.activity_type == "request" and row.request_status:
            activity.append(
                DashboardActivityItem(
                    id=f"request-{row.source_id}",
                    type="request",
                    title=f"Exception request {row.request_status.value}",
                    subtitle=media_title,
                    created_at=to_utc_isoformat(row.created_at) or "",
                    actor_display=actor_display,
                    media_type=row.media_type.value if row.media_type else None,
                    media_title=media_title,
                )
            )
        elif row.activity_type == "task" and row.task and row.task_status:
            subtitle = (
                f"Processed {row.items_processed} items"
                if row.items_processed is not None
                else None
            )
            activity.append(
                DashboardActivityItem(
                    id=f"task-{row.source_id}",
                    type="task",
                    title=f"{row.task.friendly_name()} {row.task_status.value}",
                    subtitle=subtitle,
                    created_at=to_utc_isoformat(row.created_at) or "",
                )
            )
        elif row.activity_type == "protected":
            activity.append(
                DashboardActivityItem(
                    id=f"protected-{row.source_id}",
                    type="protected",
                    title="Media added to protected list",
                    subtitle=media_title,
                    created_at=to_utc_isoformat(row.created_at) or "",
                    actor_display=actor_display,
                    media_type=row.media_type.value if row.media_type else None,
                    media_title=media_title,
                )
            )

    kpis = DashboardKpis(
        total_movies=movie_count,
        total_series=series_count,
        total_movies_size_gb=round(float(bytes_to_gb(all_movies_size)), 2),
        total_series_size_gb=round(float(bytes_to_gb(all_series_size)), 2),
        reclaimable_movies_gb=round(float(bytes_to_gb(movie_size_total)), 2),
        reclaimable_series_gb=round(float(bytes_to_gb(series_size_total)), 2),
        reclaimable_total_gb=round(
            float(bytes_to_gb(movie_size_total + series_size_total)), 2
        ),
    )
    request_summary = DashboardRequestsSummary(
        pending_count=pending_requests,
        approved_7d=approved_7d,
        denied_7d=denied_7d,
        mine_pending=mine_pending,
        mine_active=mine_active,
    )
    viewer = DashboardViewer(
        role=current_user.role.value,
        can_view_admin_panels=is_admin,
    )

    return DashboardResponse(
        kpis=kpis,
        requests=request_summary,
        services=services,
        activity=activity,
        viewer=viewer,
        media_server_configured=media_server_configured,
    )
