from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import niquests

from backend.core.utils.request import response_body_excerpt


class _SafeDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def _template_context(payload: dict[str, Any]) -> dict[str, str]:
    context: dict[str, str] = {}
    flattened = dict(payload)
    for namespace, nested in payload.items():
        if not isinstance(nested, dict):
            continue
        for key, value in nested.items():
            flattened[f"{namespace}_{key}"] = value
            if namespace in {"candidate", "protection"}:
                flattened.setdefault(str(key), value)
    for key, value in flattened.items():
        text = "" if value is None else str(value)
        context[key] = text
        context[f"urlencoded_{key}"] = quote(text, safe="")
    return context


def _render_template(template: str, context: dict[str, str]) -> str:
    return template.format_map(_SafeDict(context))


def _retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value.strip()))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0, int((retry_at - datetime.now(UTC)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


def _header_dict(headers: Any, context: dict[str, str]) -> dict[str, str]:
    rendered: dict[str, str] = {}
    for header in headers or []:
        if not isinstance(header, dict):
            continue
        name = str(header.get("name") or "").strip()
        value = str(header.get("value") or "").strip()
        if name:
            rendered[name] = _render_template(value, context)
    return rendered


async def send_webhook_payload(
    config: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Render and send a webhook payload without owning delivery policy."""
    context = _template_context(payload)
    url = _render_template(str(config.get("url_template") or ""), context)
    method = str(config.get("method") or "GET").upper()
    timeout = int(config.get("timeout_seconds") or 15)
    headers = _header_dict(config.get("headers"), context)

    auth = None
    username = config.get("auth_username")
    password = config.get("auth_password")
    if username and password:
        auth = (str(username), str(password))

    try:
        async with niquests.AsyncSession() as session:
            if method == "POST":
                body_template = config.get("body_template")
                if body_template:
                    response = await session.post(
                        url,
                        data=_render_template(str(body_template), context),
                        headers=headers,
                        auth=auth,
                        timeout=timeout,
                    )
                else:
                    response = await session.post(
                        url,
                        json=payload,
                        headers=headers,
                        auth=auth,
                        timeout=timeout,
                    )
            else:
                response = await session.get(
                    url, headers=headers, auth=auth, timeout=timeout
                )

        status_code = response.status_code or 0
        if 200 <= status_code < 300:
            return {"success": True, "status_code": status_code, "error": None}
        body = response_body_excerpt(response, max_chars=600)
        error = f"HTTP {status_code}"
        if body:
            error = f"{error}: {body}"
        result: dict[str, Any] = {
            "success": False,
            "status_code": status_code,
            "error": error,
        }
        response_headers = getattr(response, "headers", None)
        retry_after = _retry_after_seconds(
            response_headers.get("Retry-After") if response_headers else None
        )
        if retry_after is not None:
            result["retry_after_seconds"] = retry_after
        return result
    except Exception as exc:
        return {"success": False, "status_code": None, "error": str(exc)}
