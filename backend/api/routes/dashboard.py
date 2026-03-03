from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import get_current_user
from backend.core.utils.file_utils import bytes_to_gb
from backend.database import get_db
from backend.database.models import (
    ExceptionRequest,
    MediaBlacklist,
    Movie,
    ReclaimCandidate,
    Series,
    ServiceConfig,
    TaskRun,
    User,
)
from backend.enums import (
    ExceptionRequestStatus,
    MediaType,
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

router = APIRouter(prefix="/api", tags=["dashboard"])


def _to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Role aware dashboard summary."""

    now = datetime.now(timezone.utc)
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
                select(func.count())
                .select_from(ExceptionRequest)
                .where(ExceptionRequest.status == ExceptionRequestStatus.PENDING)
                .scalar_subquery()
                .label("pending_requests"),
                select(func.count())
                .select_from(ExceptionRequest)
                .where(
                    ExceptionRequest.status == ExceptionRequestStatus.APPROVED,
                    ExceptionRequest.reviewed_at.is_not(None),
                    ExceptionRequest.reviewed_at >= seven_days_ago,
                )
                .scalar_subquery()
                .label("approved_7d"),
                select(func.count())
                .select_from(ExceptionRequest)
                .where(
                    ExceptionRequest.status == ExceptionRequestStatus.DENIED,
                    ExceptionRequest.reviewed_at.is_not(None),
                    ExceptionRequest.reviewed_at >= seven_days_ago,
                )
                .scalar_subquery()
                .label("denied_7d"),
                select(func.count())
                .select_from(ExceptionRequest)
                .where(
                    ExceptionRequest.requested_by_user_id == current_user.id,
                    ExceptionRequest.status == ExceptionRequestStatus.PENDING,
                )
                .scalar_subquery()
                .label("mine_pending"),
                select(func.count())
                .select_from(ExceptionRequest)
                .where(
                    ExceptionRequest.requested_by_user_id == current_user.id,
                    ExceptionRequest.status == ExceptionRequestStatus.APPROVED,
                    or_(
                        ExceptionRequest.requested_expires_at.is_(None),
                        ExceptionRequest.requested_expires_at >= now,
                    ),
                )
                .scalar_subquery()
                .label("mine_active"),
            )
        )
    ).one()

    movie_count = summary_row.movie_count or 0
    series_count = summary_row.series_count or 0
    movie_size_total = summary_row.movie_size_total or 0
    series_size_total = summary_row.series_size_total or 0
    pending_requests = summary_row.pending_requests or 0
    approved_7d = summary_row.approved_7d or 0
    denied_7d = summary_row.denied_7d or 0
    mine_pending = summary_row.mine_pending or 0
    mine_active = summary_row.mine_active or 0

    services: list[DashboardServiceSummary] = []
    if is_admin:
        service_task_map: dict[Service, Task] = {
            Service.PLEX: Task.SYNC_PLEX_MEDIA,
            Service.JELLYFIN: Task.SYNC_JELLYFIN_MEDIA,
        }

        task_last_sync_subquery = (
            select(TaskRun.task, func.max(TaskRun.completed_at).label("last_sync_at"))
            .where(
                TaskRun.status == TaskStatus.COMPLETED,
                TaskRun.task.in_(list(service_task_map.values())),
            )
            .group_by(TaskRun.task)
            .subquery()
        )

        service_to_task = case(
            (ServiceConfig.service_type == Service.PLEX, Task.SYNC_PLEX_MEDIA),
            (ServiceConfig.service_type == Service.JELLYFIN, Task.SYNC_JELLYFIN_MEDIA),
            else_=None,
        )

        service_status_rows = (
            await db.execute(
                select(
                    ServiceConfig.service_type,
                    ServiceConfig.enabled,
                    task_last_sync_subquery.c.last_sync_at,
                ).outerjoin(
                    task_last_sync_subquery,
                    task_last_sync_subquery.c.task == service_to_task,
                )
            )
        ).all()

        enabled_map: dict[Service, bool] = {}
        service_last_sync_map: dict[Service, datetime | None] = {}
        for service_type, enabled, last_sync_at in service_status_rows:
            enabled_map[service_type] = enabled_map.get(service_type, False) or bool(
                enabled
            )
            if last_sync_at is not None:
                service_last_sync_map[service_type] = last_sync_at

        for service in sorted(Service, key=lambda service_item: service_item.value):
            enabled = bool(enabled_map.get(service, False))
            last_sync_at = service_last_sync_map.get(service)
            services.append(
                DashboardServiceSummary(
                    name=service.value,
                    enabled=enabled,
                    status="healthy" if enabled else "disabled",
                    last_sync_at=_to_iso(last_sync_at),
                )
            )

    request_activity = (
        select(
            literal("request").label("activity_type"),
            ExceptionRequest.id.label("source_id"),
            ExceptionRequest.created_at.label("created_at"),
            ExceptionRequest.status.label("request_status"),
            literal(None, type_=TaskRun.task.type).label("task"),
            literal(None, type_=TaskRun.status.type).label("task_status"),
            literal(None, type_=TaskRun.items_processed.type).label("items_processed"),
            ExceptionRequest.media_type.label("media_type"),
            Movie.title.label("movie_title"),
            Series.title.label("series_title"),
            User.username.label("username"),
            User.display_name.label("display_name"),
        )
        .outerjoin(User, User.id == ExceptionRequest.requested_by_user_id)
        .outerjoin(Movie, Movie.id == ExceptionRequest.movie_id)
        .outerjoin(Series, Series.id == ExceptionRequest.series_id)
    )
    if not is_admin:
        request_activity = request_activity.where(
            ExceptionRequest.requested_by_user_id == current_user.id
        )

    task_activity = select(
        literal("task").label("activity_type"),
        TaskRun.id.label("source_id"),
        TaskRun.created_at.label("created_at"),
        literal(None, type_=ExceptionRequest.status.type).label("request_status"),
        TaskRun.task.label("task"),
        TaskRun.status.label("task_status"),
        TaskRun.items_processed.label("items_processed"),
        literal(None, type_=ExceptionRequest.media_type.type).label("media_type"),
        literal(None, type_=Movie.title.type).label("movie_title"),
        literal(None, type_=Series.title.type).label("series_title"),
        literal(None, type_=User.username.type).label("username"),
        literal(None, type_=User.display_name.type).label("display_name"),
    )

    activity_parts = [request_activity, task_activity]

    if is_admin:
        blacklist_activity = (
            select(
                literal("blacklist").label("activity_type"),
                MediaBlacklist.id.label("source_id"),
                MediaBlacklist.created_at.label("created_at"),
                literal(None, type_=ExceptionRequest.status.type).label(
                    "request_status"
                ),
                literal(None, type_=TaskRun.task.type).label("task"),
                literal(None, type_=TaskRun.status.type).label("task_status"),
                literal(None, type_=TaskRun.items_processed.type).label(
                    "items_processed"
                ),
                MediaBlacklist.media_type.label("media_type"),
                Movie.title.label("movie_title"),
                Series.title.label("series_title"),
                User.username.label("username"),
                User.display_name.label("display_name"),
            )
            .outerjoin(User, User.id == MediaBlacklist.blacklisted_by_user_id)
            .outerjoin(Movie, Movie.id == MediaBlacklist.movie_id)
            .outerjoin(Series, Series.id == MediaBlacklist.series_id)
        )
        activity_parts.append(blacklist_activity)

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
                    created_at=_to_iso(row.created_at) or "",
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
                    created_at=_to_iso(row.created_at) or "",
                )
            )
        elif row.activity_type == "blacklist":
            activity.append(
                DashboardActivityItem(
                    id=f"blacklist-{row.source_id}",
                    type="blacklist",
                    title="Media added to blacklist",
                    subtitle=media_title,
                    created_at=_to_iso(row.created_at) or "",
                    actor_display=actor_display,
                    media_type=row.media_type.value if row.media_type else None,
                    media_title=media_title,
                )
            )

    kpis = DashboardKpis(
        total_movies=movie_count,
        total_series=series_count,
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
    )
