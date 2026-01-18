import asyncio

from sqlalchemy import select

from backend.core.auth import get_password_hash
from backend.core.logger import LOG
from backend.core.settings import settings
from backend.database import async_db
from backend.database.models import User
from backend.enums import UserRole


async def create_initial_admin() -> bool:
    """Create the first admin user if no users exist."""
    async with async_db() as session:
        # check if any users exist
        user = await session.execute(select(User).where(User.role == UserRole.ADMIN))
        if user.first():
            LOG.debug("Admin user already exists, skipping admin creation")
            return False

        # check if initial admin password is set
        if not settings.admin_password:
            LOG.warning(
                "No initial admin password set in settings. Skipping admin creation. "
                "Please set 'ADMIN_PASSWORD' in your environment variables or .env file "
                "to create the initial admin user"
            )
            return False

        # create admin user
        admin = User(
            username="admin",
            password_hash=get_password_hash(settings.admin_password),
            display_name="Admin",
            role=UserRole.ADMIN,
        )
        session.add(admin)
        await session.commit()

        LOG.info("Initial admin user created successfully")
        return True


if __name__ == "__main__":
    asyncio.run(create_initial_admin())
