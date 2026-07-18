from __future__ import annotations

from typing import Literal

SortOrder = Literal["asc", "desc"]


def total_pages(total: int, per_page: int) -> int:
    return (total + per_page - 1) // per_page if total else 0
