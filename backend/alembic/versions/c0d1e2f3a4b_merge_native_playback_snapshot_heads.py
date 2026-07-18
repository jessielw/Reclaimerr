"""Merge native playback snapshot and IMDb cache migration heads.

Revision ID: c0d1e2f3a4b
Revises: b8c9d0e1f2a3, b9c2d4e6f8a0
Create Date: 2026-07-16 00:01:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence


revision: str = "c0d1e2f3a4b"
down_revision: str | Sequence[str] | None = ("b8c9d0e1f2a3", "b9c2d4e6f8a0")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
