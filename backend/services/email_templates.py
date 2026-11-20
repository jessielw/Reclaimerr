"""Custom HTML templates for system emails.

A template is a plain HTML file named after the notification event, for example
``<data dir>/templates/email/delete_request_execution_succeeded.html``. When one
exists, an Email destination that chooses Template for the event receives it
instead of the default text body. With no file the email is unchanged, so this is
strictly opt-in.

Templates use ``{{token}}`` placeholders, filled with HTML-escaped values, and
``{{#if token}}...{{/if}}`` blocks that are kept only when the token has a value.
There is no scripting: a template cannot run code or read anything that is not
listed in ``TOKENS``. The text of the ``<title>`` element is the email subject.

An optional ``voices.json`` next to the templates holds a list of
``{"headline": ..., "subline": ..., "subject": ...}`` objects. One is picked per
request, so a template can vary its wording with ``{{headline}}``,
``{{subline}}`` and ``{{voice_subject}}``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from backend.core.logger import LOG
from backend.core.settings import settings

# every value a template can use
TOKENS = (
    "event",
    "request_id",
    "recipient_name",
    "actor",
    "media_title",
    "media_year",
    "media_type",
    "request_type",
    "is_deletion",
    "is_protection",
    "scope",
    "poster_url",
    "size",
    "size_bytes",
    "genres",
    "overview",
    "tmdb_id",
    "imdb_id",
    "reason",
    "admin_notes",
    "error",
    "requested_date",
    "scheduled_date",
    "deleted_date",
    "total_reclaimed",
    "app_url",
    "headline",
    "subline",
    "voice_subject",
)

_TOKEN = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_IF_BLOCK = re.compile(r"\{\{#if\s+(\w+)\s*\}\}(.*?)\{\{/if\s*\}\}", re.DOTALL)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SAFE_EVENT = re.compile(r"^[a-z0-9_]+$")


def template_dir() -> Path:
    return settings.data_dir_path / "templates" / "email"


def _format_bytes(value: int | None) -> str:
    if not value or value <= 0:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{size:.1f} {units[idx]}" if idx else f"{int(size)} B"


def _format_date(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return ""
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return f"{value.day} {value:%b %Y}"


def _poster_url(path: Any) -> str:
    """A poster URL a recipient's mail client can load.

    Reclaimerr stores TMDB poster paths, so the image comes from TMDB's CDN (or
    ``EMAIL_POSTER_BASE_URL``) and never from this server, which may not be
    reachable from the internet. A full URL pointing anywhere else, such as a
    media server on a private network, is dropped rather than sent as a broken
    image.
    """
    text = str(path or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text if text.startswith("https://image.tmdb.org/") else ""
    return f"{settings.email_poster_base_url.rstrip('/')}/{text.lstrip('/')}"


def parse_voices(text: str) -> list[dict[str, str]]:
    """Parse the text of a voices file. Raises ValueError when it is not usable."""
    raw = json.loads(text)
    if not isinstance(raw, list):
        raise ValueError("voices must be a JSON list")
    voices = [
        {k: str(v.get(k) or "") for k in ("headline", "subline", "subject")}
        for v in raw
        if isinstance(v, dict)
    ]
    if not voices:
        raise ValueError("voices needs at least one entry with a headline")
    return voices


def _default_voices() -> list[dict[str, str]]:
    """The wording used when none is saved: the same list the editor offers."""
    return parse_voices(example_voices())


def _load_voices(directory: Path) -> list[dict[str, str]]:
    path = directory / "voices.json"
    if not path.is_file():
        return _default_voices()
    try:
        return parse_voices(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOG.warning(f"Ignoring unusable email voices file {path}: {exc}")
        return _default_voices()


def build_values(
    *,
    event: str,
    context: Mapping[str, Any] | None,
    recipient: Any = None,
    application_url: str | None = None,
    total_reclaimed_bytes: int | None = None,
    directory: Path | None = None,
    voices: list[dict[str, str]] | None = None,
) -> dict[str, str]:
    """The raw (unescaped) text for every token."""
    ctx = dict(context or {})
    request_id = ctx.get("request_id")
    voices = voices or _load_voices(directory or template_dir())
    voice = (
        voices[int(request_id) % len(voices)]
        if isinstance(request_id, int)
        else voices[0]
    )
    genres = ctx.get("genres")
    size_bytes = ctx.get("media_size_bytes")
    name = ""
    if recipient is not None:
        name = (
            getattr(recipient, "display_name", None)
            or getattr(recipient, "username", None)
            or ""
        )
    media_type = str(ctx.get("media_type") or "")
    values = {
        "event": event,
        "request_id": str(request_id or ""),
        "recipient_name": str(name),
        "actor": str(ctx.get("actor") or ""),
        "media_title": str(ctx.get("media_title") or ""),
        "media_year": str(ctx.get("media_year") or ""),
        "media_type": "TV show" if media_type == "series" else media_type.title(),
        "request_type": str(ctx.get("request_type") or "Deletion"),
        # for {{#if}}: Request approved/declined emails cover protection requests too
        "is_deletion": "" if ctx.get("request_type") == "Protection" else "1",
        "is_protection": "1" if ctx.get("request_type") == "Protection" else "",
        "scope": str(ctx.get("scope") or ""),
        "poster_url": _poster_url(ctx.get("poster_path")),
        "size": _format_bytes(size_bytes if isinstance(size_bytes, int) else None),
        "size_bytes": str(size_bytes or ""),
        "genres": ", ".join(str(g) for g in genres) if isinstance(genres, list) else "",
        "overview": str(ctx.get("overview") or ""),
        "tmdb_id": str(ctx.get("tmdb_id") or ""),
        "imdb_id": str(ctx.get("imdb_id") or ""),
        "reason": str(ctx.get("reason") or ""),
        "admin_notes": str(ctx.get("admin_notes") or ""),
        "error": str(ctx.get("error") or ""),
        "requested_date": _format_date(ctx.get("requested_at")),
        "scheduled_date": _format_date(ctx.get("scheduled_at")),
        "deleted_date": _format_date(ctx.get("executed_at")),
        "total_reclaimed": _format_bytes(total_reclaimed_bytes),
        "app_url": (application_url or "").rstrip("/"),
        "headline": voice["headline"],
        "subline": voice["subline"],
        "voice_subject": voice["subject"],
    }
    # the voice lines can use the other tokens, like "{{media_title}} is gone"
    for key in ("headline", "subline", "voice_subject"):
        values[key] = render_string(values[key], values, html=False)
    return values


def render_string(template: str, values: Mapping[str, str], *, html: bool) -> str:
    """Fill ``template``. Unknown tokens render as nothing."""

    def keep(match: re.Match[str]) -> str:
        return match.group(2) if values.get(match.group(1)) else ""

    def fill(match: re.Match[str]) -> str:
        value = values.get(match.group(1), "")
        return escape(value, quote=True) if html else value

    return _TOKEN.sub(fill, _IF_BLOCK.sub(keep, template))


def find_template(event: str, directory: Path | None = None) -> Path | None:
    if not _SAFE_EVENT.match(event):
        return None
    path = (directory or template_dir()) / f"{event}.html"
    return path if path.is_file() else None


def template_uses(source: str, token: str) -> bool:
    return bool(
        re.search(r"\{\{\s*(?:#if\s+)?" + re.escape(token) + r"\s*\}\}", source)
    )


def render_template(
    source: str, values: Mapping[str, str], *, default_subject: str
) -> tuple[str, str]:
    """Return ``(subject, html)`` for a template's source."""
    subject = default_subject
    title = _TITLE.search(source)
    if title:
        rendered = render_string(title.group(1), values, html=False).strip()
        subject = re.sub(r"\s+", " ", rendered) or default_subject
    return subject, render_string(source, values, html=True)


# events an admin can write a template for, and the label the UI shows
TEMPLATE_EVENTS: dict[str, str] = {
    "delete_request_execution_succeeded": "Deletion completed",
    "delete_request_execution_failed": "Deletion failed",
    "request_approved": "Request approved",
    "request_declined": "Request declined",
    "admin_new_delete_request": "New delete request (admins)",
    "admin_new_protection_request": "New protection request (admins)",
    "admin_request_cancelled": "Request cancelled (admins)",
    "admin_delete_execution_failed": "Deletion failed (admins)",
}
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "email_examples"


def example_voices() -> str:
    return (EXAMPLES_DIR / "voices.json").read_text(encoding="utf-8")
