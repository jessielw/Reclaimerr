"""CSV downloads that open safely in a spreadsheet."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from datetime import datetime

from fastapi.responses import Response

from backend.core.utils.datetime_utils import ensure_utc

# A cell starting with one of these is run as a formula by Excel, LibreOffice
# and Google Sheets, so a crafted title could execute on the admin's machine.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value: object) -> object:
    """Neutralize text a spreadsheet would treat as a formula."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def csv_datetime(value: str | datetime | None) -> str:
    """Render a timestamp as UTC ``YYYY-MM-DD HH:MM``, which spreadsheets parse."""
    if not value:
        return ""
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return f"{ensure_utc(parsed):%Y-%m-%d %H:%M}"


def csv_gigabytes(size_bytes: int | None) -> float | str:
    """Bytes as a GB number, so the column still sorts as numbers."""
    if size_bytes is None:
        return ""
    return round(size_bytes / 1024**3, 2)


def csv_response(
    filename: str, header: Sequence[str], rows: Iterable[Sequence[object]]
) -> Response:
    buffer = io.StringIO()
    # the BOM makes Excel read titles with accents as UTF-8
    buffer.write("﻿")
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([csv_cell(value) for value in row])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
