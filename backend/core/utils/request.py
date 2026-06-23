from __future__ import annotations

import re
from typing import Any

import niquests

RETRY_CODES = (429, 503, 502, 504)

DEFAULT_ERROR_BODY_MAX_CHARS = 1200
DEFAULT_ERROR_SUMMARY_MAX_CHARS = 500
_TRUNCATE_SUFFIX = "..."
_REDACTED = "<redacted>"
_SECRET_KEY_PATTERN = (
    r"(?:api[_-]?key|token|authorization|password|passwd|secret|client[_-]?secret|"
    r"x-api-key|x-emby-token|x-plex-token)"
)
_QUERY_SECRET_RE = re.compile(rf"(?i)\b({_SECRET_KEY_PATTERN}=)([^&\s]+)")
_KV_SECRET_RE = re.compile(
    rf"(?i)([\"']?{_SECRET_KEY_PATTERN}[\"']?\s*[:=]\s*)([\"']?)([^\"',}}\s&]+)([\"']?)"
)


def should_retry_on_status(exception: BaseException) -> bool:
    """Check if exception is a retryable HTTP status code."""
    if isinstance(exception, niquests.HTTPError) and exception.response is not None:
        return exception.response.status_code in RETRY_CODES
    return False


def _truncate(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= len(_TRUNCATE_SUFFIX):
        return _TRUNCATE_SUFFIX[:max_chars]
    return text[: max_chars - len(_TRUNCATE_SUFFIX)] + _TRUNCATE_SUFFIX


def _redact_sensitive(text: str) -> str:
    text = _QUERY_SECRET_RE.sub(rf"\1{_REDACTED}", text)
    return _KV_SECRET_RE.sub(rf"\1\2{_REDACTED}\4", text)


def _response_body_text(response: Any) -> str | None:
    if response is None:
        return None

    text: str | None = None
    try:
        raw_text = getattr(response, "text", None)
    except Exception:
        raw_text = None

    if isinstance(raw_text, bytes):
        text = raw_text.decode("utf-8", errors="replace")
    elif isinstance(raw_text, str):
        text = raw_text

    if text and text.strip():
        return text.strip()

    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)) and content:
        if b"\x00" in content:
            return f"<{len(content)} bytes binary body>"
        return bytes(content).decode("utf-8", errors="replace").strip() or None
    if content:
        return str(content).strip() or None
    return None


def response_body_excerpt(
    response: Any,
    *,
    max_chars: int = DEFAULT_ERROR_BODY_MAX_CHARS,
) -> str | None:
    body = _response_body_text(response)
    if not body:
        return None
    return _truncate(_redact_sensitive(body), max_chars=max_chars)


def format_http_failure(
    *,
    action: str,
    exception: BaseException | None = None,
    response: Any | None = None,
    method: str | None = None,
    endpoint: str | None = None,
    max_body_chars: int = DEFAULT_ERROR_BODY_MAX_CHARS,
) -> str:
    resp = response if response is not None else getattr(exception, "response", None)
    status_code = getattr(resp, "status_code", None)
    req = getattr(resp, "request", None)
    method_name = method or getattr(req, "method", None)
    request_url_raw = endpoint or getattr(req, "url", None)
    request_url = (
        _truncate(_redact_sensitive(str(request_url_raw)), max_chars=max_body_chars)
        if request_url_raw
        else None
    )
    body = response_body_excerpt(resp, max_chars=max_body_chars)

    details: list[str] = []
    if status_code is not None:
        details.append(f"status={status_code}")
    if method_name:
        details.append(f"method={method_name}")
    if request_url:
        details.append(f"url={request_url}")
    if body:
        details.append(f"body={body}")

    message = action
    if details:
        message += f" ({', '.join(details)})"

    exception_text = (
        _truncate(_redact_sensitive(str(exception).strip()), max_chars=max_body_chars)
        if exception
        else ""
    )
    if exception_text and exception_text not in message:
        message = f"{message}: {exception_text}"
    return message


def summarize_error_message(
    message: str | None,
    *,
    max_chars: int = DEFAULT_ERROR_SUMMARY_MAX_CHARS,
) -> str | None:
    if not message:
        return None
    normalized = " ".join(str(message).split())
    if not normalized:
        return None
    return _truncate(_redact_sensitive(normalized), max_chars=max_chars)
