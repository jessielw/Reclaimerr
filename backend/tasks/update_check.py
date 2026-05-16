from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlparse

import niquests
from semver import VersionInfo
from sqlalchemy import select

from backend.core.__version__ import __version__, program_name, program_url
from backend.core.logger import LOG
from backend.core.task_tracking import track_task_execution
from backend.database import async_db
from backend.database.models import AppUpdateState
from backend.enums import Task


def _parse_repo_slug(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Invalid GitHub repo URL: {url}")
    return parts[0], parts[1]


def _normalize_version(raw: str) -> str:
    return raw.strip().lstrip("vV")


async def _persist_result(
    *,
    latest_version: str | None,
    latest_release_url: str | None,
    latest_release_published_at: datetime | None,
    update_available: bool,
    error: str | None,
) -> None:
    now = datetime.now(UTC)
    async with async_db() as db:
        state = (await db.execute(select(AppUpdateState))).scalars().first()
        if state is None:
            state = AppUpdateState()
            db.add(state)

        state.current_version = str(__version__)
        if latest_version is not None:
            state.latest_version = latest_version
        if latest_release_url is not None:
            state.latest_release_url = latest_release_url
        if latest_release_published_at is not None:
            state.latest_release_published_at = latest_release_published_at
        state.update_available = update_available
        state.last_checked_at = now
        state.last_check_error = error
        await db.commit()


async def check_app_updates() -> None:
    """Scheduled task: check latest GitHub release and persist app update state."""
    async with track_task_execution(Task.CHECK_APP_UPDATES):
        owner, repo = _parse_repo_slug(program_url)
        latest_version: str | None = None
        latest_url: str | None = None
        latest_published_at: datetime | None = None

        try:
            endpoint = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
            async with niquests.AsyncSession() as session:
                response = await session.get(
                    endpoint,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": f"{program_name}/{__version__}",
                    },
                    timeout=20,
                )
                response.raise_for_status()
                payload = response.json()

            tag_name = str(payload.get("tag_name") or "").strip()
            if not tag_name:
                raise ValueError("GitHub release response missing tag_name")

            latest_version = _normalize_version(tag_name)
            latest_url = str(payload.get("html_url") or "").strip() or None

            published_raw = str(payload.get("published_at") or "").strip()
            if published_raw:
                latest_published_at = datetime.fromisoformat(
                    published_raw.replace("Z", "+00:00")
                )

            current_semver = VersionInfo.parse(str(__version__))
            latest_semver = VersionInfo.parse(latest_version)
            update_available = latest_semver > current_semver

            await _persist_result(
                latest_version=latest_version,
                latest_release_url=latest_url,
                latest_release_published_at=latest_published_at,
                update_available=update_available,
                error=None,
            )
            LOG.debug(
                f"Update check complete: current={__version__}, latest={latest_version}, "
                f"update_available={update_available}"
            )
        except Exception as e:
            LOG.warning(f"Update check failed: {e}")
            await _persist_result(
                latest_version=latest_version,
                latest_release_url=latest_url,
                latest_release_published_at=latest_published_at,
                update_available=False,
                error=str(e)[:2000],
            )
