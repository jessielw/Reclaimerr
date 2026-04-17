import asyncio

from sqlalchemy import select

from backend.core.auth import get_password_hash, verify_password
from backend.core.logger import LOG
from backend.core.settings import settings
from backend.core.setup_state import setup_state
from backend.database import async_db
from backend.database.models import User
from backend.enums import UserRole


async def create_initial_admin() -> bool:
    """Create the first admin user if none exists, or reset password if ADMIN_PASSWORD is set.

    Also resolves the needs_setup flag: if an admin already exists the app is
    considered fully set up regardless of how it was created.

    Password reset:
        If ADMIN_PASSWORD is set and an admin already exists, the first admin's
        password is updated to the new value on startup. Remove the env var after
        the password has been reset to prevent it being applied on every restart.
        If you don't remove the var and the password matches the existing hash,
        it will be a no op and no warning will be logged.
    """
    async with async_db() as session:
        result = await session.execute(select(User).where(User.role == UserRole.ADMIN))
        existing_admin = result.scalars().first()

        if existing_admin:
            setup_state.needs_setup = False
            # if ADMIN_PASSWORD is set, treat it as a password reset request unless the
            # password matches the existing hash
            if settings.admin_password:
                if not verify_password(
                    settings.admin_password, existing_admin.password_hash
                ):
                    existing_admin.password_hash = get_password_hash(
                        settings.admin_password
                    )
                    existing_admin.bump_token_version()
                    await session.commit()
                    LOG.warning(
                        "Admin password has been reset via ADMIN_PASSWORD env var. "
                        "You should remove ADMIN_PASSWORD from your environment after logging in."
                    )
            else:
                LOG.debug("Admin user already exists (setup not required)")
            return False

        # If ADMIN_PASSWORD is set (via .env in Docker), auto create
        # the admin and mark setup complete so the wizard is never shown.
        if settings.admin_password:
            admin = User(
                username="admin",
                password_hash=get_password_hash(settings.admin_password),
                display_name="Admin",
                role=UserRole.ADMIN,
            )
            session.add(admin)
            await session.commit()
            setup_state.needs_setup = False
            LOG.info("Initial admin user created from ADMIN_PASSWORD env var")
            return True

        # no admin and no env var (setup wizard is required).
        LOG.info(
            "No admin account found. First run setup wizard will be shown on next browser open."
        )
        return False


if __name__ == "__main__":
    asyncio.run(create_initial_admin())
