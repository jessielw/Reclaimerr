from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import require_admin
from backend.core.logger import LOG
from backend.database import get_db
from backend.database.models import (
    Movie,
    ReclaimRule,
    Series,
    ServiceMediaLibrary,
    User,
)
from backend.models.cleanup import (
    CleanupRuleCreate,
    CleanupRuleResponse,
    CleanupRuleUpdate,
)

router = APIRouter(prefix="/api", tags=["rules"])


@router.get("/rules", response_model=list[CleanupRuleResponse])
async def get_rules(
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Get all cleanup rules."""
    result = await db.execute(select(ReclaimRule))
    rules = result.scalars().all()
    return rules


@router.post(
    "/rules", response_model=CleanupRuleResponse, status_code=status.HTTP_201_CREATED
)
async def create_rule(
    rule_data: CleanupRuleCreate,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Create a new cleanup rule."""
    new_rule = ReclaimRule(
        name=rule_data.name,
        media_type=rule_data.media_type,
        enabled=rule_data.enabled,
        library_ids=rule_data.library_ids,
        min_popularity=rule_data.min_popularity,
        max_popularity=rule_data.max_popularity,
        min_vote_average=rule_data.min_vote_average,
        max_vote_average=rule_data.max_vote_average,
        min_vote_count=rule_data.min_vote_count,
        max_vote_count=rule_data.max_vote_count,
        min_view_count=rule_data.min_view_count,
        max_view_count=rule_data.max_view_count,
        include_never_watched=rule_data.include_never_watched,
        min_days_since_added=rule_data.min_days_since_added,
        max_days_since_added=rule_data.max_days_since_added,
        min_days_since_last_watched=rule_data.min_days_since_last_watched,
        max_days_since_last_watched=rule_data.max_days_since_last_watched,
        min_size=rule_data.min_size,
        max_size=rule_data.max_size,
    )

    db.add(new_rule)
    await db.commit()
    await db.refresh(new_rule)

    LOG.info(f"Created cleanup rule: {new_rule.name} (ID: {new_rule.id})")
    return new_rule


@router.post("/rules/{rule_id}", response_model=CleanupRuleResponse)
async def update_rule(
    rule_id: int,
    rule_data: CleanupRuleUpdate,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Updates an existing cleanup rule."""
    result = await db.execute(select(ReclaimRule).where(ReclaimRule.id == rule_id))
    rule = result.scalar_one_or_none()

    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rule with ID {rule_id} not found",
        )

    # update only the fields that were provided
    update_data = rule_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(rule, field, value)

    await db.commit()
    await db.refresh(rule)

    LOG.info(f"Updated cleanup rule: {rule.name} (ID: {rule.id})")
    return rule


@router.delete("/rules/{rule_id}")
async def delete_rule(
    rule_id: int,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Remove a cleanup rule."""
    result = await db.execute(select(ReclaimRule).where(ReclaimRule.id == rule_id))
    rule = result.scalar_one_or_none()

    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rule with ID {rule_id} not found",
        )

    rule_name = rule.name
    await db.delete(rule)
    await db.commit()

    msg = f"Deleted cleanup rule: {rule_name} (ID: {rule_id})"
    LOG.info(msg)
    return {"message": msg}


@router.get("/rules/check-synced")
async def check_synced_rules(
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Return the count of synced libraries, movies, and series."""
    lib_count = await db.scalar(select(func.count(ServiceMediaLibrary.id)))
    movie_count = await db.scalar(select(func.count(Movie.id)))
    series_count = await db.scalar(select(func.count(Series.id)))
    return {
        "libraries": lib_count or 0,
        "movies": movie_count or 0,
        "series": series_count or 0,
    }


# @router.post("/rules/check-library-impact")
# async def check_library_impact(
#     library_ids: list[str],
#     _admin: Annotated[User, Depends(require_admin)],
#     db: AsyncSession = Depends(get_db),
# ):
#     """Check which rules would be affected by deselecting the given library IDs."""
#     result = await db.execute(select(ReclaimRule))
#     all_rules = result.scalars().all()

#     affected_rules = []
#     for rule in all_rules:
#         if rule.library_ids:
#             # Check if any of the rule's library_ids match the ones being deselected
#             if any(lib_id in library_ids for lib_id in rule.library_ids):
#                 affected_rules.append(
#                     {
#                         "id": rule.id,
#                         "name": rule.name,
#                         "media_type": rule.media_type,
#                         "affected_library_ids": [
#                             lib_id for lib_id in rule.library_ids if lib_id in library_ids
#                         ],
#                     }
#                 )

#     return {
#         "affected_count": len(affected_rules),
#         "rules": affected_rules,
#     }
