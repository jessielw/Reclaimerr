from __future__ import annotations

from dataclasses import asdict
from typing import Any
from urllib.parse import quote

import niquests
from sqlalchemy import select

from backend.core.logger import LOG
from backend.core.utils.request import response_body_excerpt
from backend.database import async_db
from backend.database.models import GeneralSettings
from backend.models.post_action_webhooks import PostActionWebhookEvent

__all__ = [
    "dispatch_configured_post_action_webhooks",
    "invalidate_webhook_config_cache",
    "send_post_action_webhook",
]


class _SafeDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _selected_path(config: dict[str, Any], event: PostActionWebhookEvent) -> str | None:
    path_mode = str(config.get("path_mode") or "original")
    if path_mode == "local":
        return event.local_path or event.path
    if path_mode == "destination":
        return event.destination_path or event.local_path or event.path
    return event.path or event.local_path


def _event_payload(
    config: dict[str, Any], event: PostActionWebhookEvent
) -> dict[str, Any]:
    payload = {key: _enum_value(value) for key, value in asdict(event).items()}
    payload["path"] = _selected_path(config, event)
    payload["original_path"] = event.path
    return payload


def _template_context(payload: dict[str, Any]) -> dict[str, str]:
    context: dict[str, str] = {}
    for key, value in payload.items():
        text = "" if value is None else str(value)
        context[key] = text
        context[f"urlencoded_{key}"] = quote(text, safe="")
    return context


def _render_template(template: str, context: dict[str, str]) -> str:
    return template.format_map(_SafeDict(context))


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


def _matches(config: dict[str, Any], event: PostActionWebhookEvent) -> bool:
    if not config.get("enabled", True):
        return False

    actions = {str(a) for a in config.get("actions") or []}
    if actions and event.action not in actions:
        return False

    media_types = {str(_enum_value(t)) for t in config.get("media_types") or []}
    if media_types and event.media_type.value not in media_types:
        return False

    return True


async def send_post_action_webhook(
    config: dict[str, Any], event: PostActionWebhookEvent
) -> dict[str, Any]:
    """Send one post action webhook.

    The caller decides whether failures are fatal. Cleanup code treats failures
    as best effort so completed reclaim actions are not rolled back.
    """
    payload = _event_payload(config, event)
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
        success = 200 <= status_code < 300
        if not success:
            body = response_body_excerpt(response, max_chars=600)
            error = f"HTTP {status_code}"
            if body:
                error = f"{error}: {body}"
            return {
                "success": False,
                "status_code": status_code,
                "error": error,
            }
        return {"success": True, "status_code": status_code, "error": None}
    except Exception as e:
        return {"success": False, "status_code": None, "error": str(e)}


class CachedWebhookConfigs:
    """Helper class to manage cached webhook configs without globals.

    Use the below instance `_cached_webhook_configs` to access the cache.
    """

    __slots__ = ("configs",)

    def __init__(self) -> None:
        self.configs: list[dict[str, Any]] | None = None


_cached_webhook_configs = CachedWebhookConfigs()


async def _load_webhook_configs() -> list[dict[str, Any]]:
    """Load webhook configs from DB, with a module level cache to avoid repeated
    DB round trips when dispatching many events in the same batch run."""
    if _cached_webhook_configs.configs is not None:
        return _cached_webhook_configs.configs
    async with async_db() as db:
        settings = (await db.execute(select(GeneralSettings))).scalars().first()
    _cached_webhook_configs.configs = settings.post_action_webhooks if settings else []
    return _cached_webhook_configs.configs


def invalidate_webhook_config_cache() -> None:
    """Invalidate the webhook config cache (call after saving settings)."""
    _cached_webhook_configs.configs = None


async def dispatch_configured_post_action_webhooks(
    event: PostActionWebhookEvent,
) -> list[dict[str, Any]]:
    """Dispatch post action webhooks configured in settings that match the event."""
    configs = await _load_webhook_configs()

    results: list[dict[str, Any]] = []
    for config in configs:
        if not isinstance(config, dict) or not _matches(config, event):
            continue
        result = await send_post_action_webhook(config, event)
        results.append(result)
        name = config.get("name") or "post action webhook"
        if result["success"]:
            LOG.info(
                f"Post action webhook '{name}' succeeded "
                f"(status={result['status_code']})"
            )
        else:
            LOG.warning(f"Post action webhook '{name}' failed: {result.get('error')}")
    return results
