"""Clear stored TMDB ratings for titles with no votes.

Revision ID: c3f7b1d09a24
Revises: d7f9a2c4e6b8
Create Date: 2026-08-04 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3f7b1d09a24"
down_revision: Union[str, None] = "d7f9a2c4e6b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # TMDB returns vote_average as 0 for a title nobody has voted on, and rows
    # written before the sync stopped storing that value still hold it, where a
    # rating rule reads it as a genuine score of zero. Requiring both a zero
    # rating and a zero vote count means only the sentinel is cleared, so no
    # real rating can be lost. The sync is stricter, clearing on any zero
    # vote count alone; this migration deliberately stays narrower so it
    # cannot destroy one.
    op.execute(
        sa.text(
            """
            UPDATE movies
            SET vote_average = NULL
            WHERE vote_count = 0 AND vote_average = 0
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE series
            SET vote_average = NULL
            WHERE vote_count = 0 AND vote_average = 0
            """
        )
    )


def downgrade() -> None:
    # Intentionally empty. The cleared values were TMDB's "no rating" marker
    # rather than data, so there is nothing meaningful to put back.
    pass
