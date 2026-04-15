from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import get_current_user, has_permission
from backend.database import get_db
from backend.database.models import ReclaimRule, ServiceMediaLibrary, User
from backend.enums import AlertLevel, Permission
from backend.models.alerts import SystemAlert

router = APIRouter(tags=["alerts"])


@router.get("/alerts", response_model=list[SystemAlert])
async def get_system_alerts(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> list[SystemAlert]:
    """
    Return actionable system alerts visible to the requesting user.

    Each alert carries a required_permission. This only alerts the user
    that has permission for are returned. Admins bypass all permission
    checks. Checks are computed live so they're always accurate.
    """
    alerts: list[SystemAlert] = []

    # --- test alerts ---
    # alerts.append(
    #     SystemAlert(
    #         id="test_warning",
    #         alert_level=AlertLevel.WARNING,
    #         title="Test warning alert",
    #         message="This is a test warning banner to preview styling. Remove this before deploying.",
    #         action_label="Go to Settings",
    #         action_href="#/settings",
    #     )
    # )
    # alerts.append(
    #     SystemAlert(
    #         id="test_error",
    #         alert_level=AlertLevel.ERROR,
    #         title="Test error alert",
    #         message="This is a test error banner with no action link.",
    #     )
    # )

    #### stale library IDs in rules ####
    # Occurs when the main media server is swapped: old library IDs stored in
    # ReclaimRule.library_ids no longer exist in ServiceMediaLibrary.
    known_lib_result = await db.execute(select(ServiceMediaLibrary.library_id))
    known_library_ids: set[str] = {row[0] for row in known_lib_result.all()}

    rules_result = await db.execute(
        select(ReclaimRule).where(
            ReclaimRule.enabled == True,
            ReclaimRule.library_ids.is_not(None),
        )
    )
    stale_rule_names: list[str] = []
    for rule in rules_result.scalars().all():
        if rule.library_ids and any(
            lid not in known_library_ids for lid in rule.library_ids
        ):
            stale_rule_names.append(rule.name)

    if stale_rule_names:
        names = ", ".join(f'"{n}"' for n in stale_rule_names[:3])
        suffix = (
            f" and {len(stale_rule_names) - 3} more"
            if len(stale_rule_names) > 3
            else ""
        )
        alerts.append(
            SystemAlert(
                id="stale_library_ids",
                alert_level=AlertLevel.WARNING,
                title="Stale library filters in cleanup rules",
                message=(
                    f"The following rules have library filters referencing libraries that no "
                    f"longer exist (likely from a previous media server): {names}{suffix}. "
                    f"Cleanup scans will produce no candidates until the library filters are "
                    f"updated or cleared."
                ),
                action_label="Go to Rules",
                action_href="#/settings",
                required_permission=Permission.MANAGE_RECLAIM,
            )
        )

    # filter to only alerts this user is permitted to see, then strip the
    # internal required_permission field before returning
    visible = [
        alert
        for alert in alerts
        if alert.required_permission is None
        or has_permission(current_user, alert.required_permission)
    ]

    # exclude internal field from the response
    return [
        SystemAlert(
            **{
                k: v
                for k, v in alert.model_dump().items()
                if k != "required_permission"
            }
        )
        for alert in visible
    ]
