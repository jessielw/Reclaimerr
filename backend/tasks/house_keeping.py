from sqlalchemy import delete, select

from backend.core.logger import LOG
from backend.database import async_db
from backend.database.models import TaskRun
from backend.enums import Task
from backend.tasks.task_tracker import track_task_execution

__all__ = ("weekly_house_keeping",)


async def weekly_house_keeping() -> None:
    async with track_task_execution(Task.WEEKLY_HOUSE_KEEPING):
        LOG.info("Starting weekly house keeping task")

        # delete old task runs, keeping only the most recent 200 runs
        await _trim_task_runs(keep_recent=200)

        LOG.info("Finished weekly house keeping task")


async def _trim_task_runs(keep_recent: int) -> None:
    """Trim old task runs from the database, keeping only the most recent N runs total."""
    async with async_db() as session:
        try:
            rows_to_del = (
                select(TaskRun.id)
                .order_by(TaskRun.started_at.desc())
                .offset(keep_recent)
            )
            del_stmt = delete(TaskRun).where(TaskRun.id.in_(rows_to_del))
            result = await session.execute(del_stmt)
            count = result.rowcount or 0  # pyright: ignore[reportAttributeAccessIssue]
            await session.commit()
            if count > 0:
                LOG.debug(f"Trimmed {count} old task runs")
        except Exception as e:
            LOG.error(f"Error trimming task runs: {e}")
            await session.rollback()
