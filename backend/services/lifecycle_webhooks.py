from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.encryption import fer_decrypt
from backend.core.logger import LOG
from backend.database import async_db
from backend.database.models import (
    LifecycleEvent,
    WebhookDelivery,
    WebhookEndpoint,
)
from backend.enums import BackgroundJobPriority, BackgroundJobType
from backend.jobs.queue import enqueue_background_job
from backend.models.jobs import WebhookDeliveryJobPayload
from backend.models.lifecycle_events import CandidateFileEvent
from backend.services.webhook_transport import send_webhook_payload

MAX_DELIVERY_ATTEMPTS = 6
RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=12),
    timedelta(hours=24),
)
RETRYABLE_STATUS_CODES = {408, 425, 429}


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _file_event_payload(event: CandidateFileEvent) -> dict[str, Any]:
    return {key: _enum_value(value) for key, value in asdict(event).items()}


async def add_lifecycle_event(
    db: AsyncSession,
    *,
    event_type: str,
    resource_name: str,
    resource_payload: dict[str, Any],
    media_type: str | None = None,
    candidate_id: int | None = None,
    actor_api_token_id: int | None = None,
    actor_user_id: int | None = None,
    links: dict[str, str] | None = None,
    top_level_payload: dict[str, Any] | None = None,
) -> LifecycleEvent:
    """Add one immutable event and its matching deliveries to a caller transaction."""
    public_event_id = str(uuid4())
    occurred_at = datetime.now(UTC)
    payload = {
        "schema_version": 1,
        "id": public_event_id,
        "type": event_type,
        "occurred_at": occurred_at.isoformat(),
        resource_name: resource_payload,
        **(top_level_payload or {}),
        "links": links or {},
    }
    lifecycle_event = LifecycleEvent(
        event_id=public_event_id,
        event_type=event_type,
        payload=payload,
        candidate_id=candidate_id,
        actor_api_token_id=actor_api_token_id,
        actor_user_id=actor_user_id,
    )
    db.add(lifecycle_event)
    await db.flush()
    endpoints = (
        (
            await db.execute(
                select(WebhookEndpoint).where(
                    WebhookEndpoint.enabled.is_(True),
                    WebhookEndpoint.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for endpoint in endpoints:
        if event_type not in endpoint.event_types:
            continue
        if (
            media_type
            and endpoint.media_types
            and media_type not in endpoint.media_types
        ):
            continue
        db.add(
            WebhookDelivery(
                event_id=lifecycle_event.id,
                endpoint_id=endpoint.id,
                next_attempt_at=occurred_at,
            )
        )
    await db.flush()
    return lifecycle_event


async def dispatch_candidate_file_event(
    event: CandidateFileEvent,
) -> list[dict[str, Any]]:
    """Persist a completed candidate file action for durable delivery."""
    event_type = f"candidate.{event.action}"
    candidate_payload = _file_event_payload(event)
    async with async_db() as db:
        lifecycle_event = await add_lifecycle_event(
            db,
            event_type=event_type,
            resource_name="candidate",
            resource_payload=candidate_payload,
            media_type=event.media_type.value,
            candidate_id=None,
            links=(
                {"self": f"/api/v1/candidates/{event.candidate_id}"}
                if event.candidate_id is not None
                else {}
            ),
            top_level_payload=candidate_payload,
        )
        delivery_ids = list(
            (
                await db.execute(
                    select(WebhookDelivery.id).where(
                        WebhookDelivery.event_id == lifecycle_event.id
                    )
                )
            ).scalars()
        )
        await db.commit()

    await enqueue_event_deliveries(lifecycle_event.id)
    LOG.info(f"Queued {len(delivery_ids)} durable webhook delivery(s) for {event_type}")
    return [
        {"queued": True, "delivery_id": delivery_id} for delivery_id in delivery_ids
    ]


def endpoint_config(endpoint: WebhookEndpoint) -> dict[str, Any]:
    headers: list[dict[str, str]] = []
    if endpoint.headers_encrypted:
        try:
            decoded = json.loads(fer_decrypt(endpoint.headers_encrypted))
            if isinstance(decoded, list):
                headers = [item for item in decoded if isinstance(item, dict)]
        except (InvalidToken, ValueError, TypeError):
            LOG.warning(f"Could not decrypt headers for webhook endpoint {endpoint.id}")
    password: str | None = None
    if endpoint.auth_password_encrypted:
        try:
            password = fer_decrypt(endpoint.auth_password_encrypted)
        except InvalidToken:
            LOG.warning(
                f"Could not decrypt password for webhook endpoint {endpoint.id}"
            )
    return {
        "enabled": endpoint.enabled and endpoint.deleted_at is None,
        "name": endpoint.name,
        "method": endpoint.method,
        "url_template": endpoint.url_template,
        "headers": headers,
        "auth_username": endpoint.auth_username,
        "auth_password": password,
        "body_template": endpoint.body_template,
        "timeout_seconds": endpoint.timeout_seconds,
    }


async def enqueue_delivery(
    delivery_id: int,
    *,
    scheduled_at: datetime,
    endpoint_id: int | None = None,
) -> None:
    if endpoint_id is None:
        async with async_db() as db:
            endpoint_id = (
                await db.execute(
                    select(WebhookDelivery.endpoint_id).where(
                        WebhookDelivery.id == delivery_id
                    )
                )
            ).scalar_one_or_none()
    await enqueue_background_job(
        BackgroundJobType.WEBHOOK_DELIVERY,
        WebhookDeliveryJobPayload(
            delivery_id=delivery_id,
            endpoint_id=endpoint_id,
        ).model_dump(mode="json"),
        scheduled_at=scheduled_at,
        dedupe_key=f"webhook-delivery:{delivery_id}",
        replace_pending=True,
        max_attempts=1,
        priority=BackgroundJobPriority.HIGH,
    )


async def enqueue_event_deliveries(event_id: int) -> None:
    async with async_db() as db:
        deliveries = (
            (
                await db.execute(
                    select(WebhookDelivery).where(
                        WebhookDelivery.event_id == event_id,
                        WebhookDelivery.status == "pending",
                    )
                )
            )
            .scalars()
            .all()
        )
    for delivery in deliveries:
        try:
            await enqueue_delivery(
                delivery.id,
                scheduled_at=delivery.next_attempt_at,
                endpoint_id=delivery.endpoint_id,
            )
        except Exception as exc:
            # The delivery row is the durable source of truth and startup
            # recovery can enqueue it later.
            LOG.warning(
                f"Could not enqueue webhook delivery {delivery.id}; it remains "
                f"pending for recovery: {exc}"
            )


async def recover_pending_webhook_deliveries() -> int:
    async with async_db() as db:
        deliveries = (
            (
                await db.execute(
                    select(WebhookDelivery).where(WebhookDelivery.status == "pending")
                )
            )
            .scalars()
            .all()
        )
    for delivery in deliveries:
        try:
            await enqueue_delivery(
                delivery.id,
                scheduled_at=delivery.next_attempt_at,
                endpoint_id=delivery.endpoint_id,
            )
        except Exception as exc:
            LOG.warning(f"Could not recover webhook delivery {delivery.id}: {exc}")
    if deliveries:
        LOG.info(f"Recovered {len(deliveries)} pending webhook delivery job(s)")
    return len(deliveries)


def _retryable(status_code: int | None) -> bool:
    return (
        status_code is None
        or status_code in RETRYABLE_STATUS_CODES
        or (status_code is not None and status_code >= 500)
    )


async def run_webhook_delivery_job(delivery_id: int) -> dict[str, Any]:
    now = datetime.now(UTC)
    async with async_db() as db:
        delivery = await db.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return {"status": "missing"}
        if delivery.status != "pending":
            return {"status": delivery.status}
        event = await db.get(LifecycleEvent, delivery.event_id)
        endpoint = await db.get(WebhookEndpoint, delivery.endpoint_id)
        if (
            event is None
            or endpoint is None
            or not endpoint.enabled
            or endpoint.deleted_at
        ):
            delivery.status = "canceled"
            delivery.last_error = "Webhook endpoint is no longer active"
            await db.commit()
            return {"status": "canceled"}
        config = endpoint_config(endpoint)
        payload = dict(event.payload)
        if endpoint.path_mode in {"local", "destination"}:
            candidate = payload.get("candidate")
            if isinstance(candidate, dict):
                if endpoint.path_mode == "destination":
                    selected_path = (
                        candidate.get("destination_path")
                        or candidate.get("local_path")
                        or candidate.get("path")
                    )
                else:
                    selected_path = candidate.get("local_path") or candidate.get("path")
                candidate = {**candidate, "path": selected_path}
                payload["candidate"] = candidate
                payload["path"] = selected_path

    result = await send_webhook_payload(config, payload)
    status_code = result.get("status_code")
    error = str(result.get("error") or "Webhook delivery failed")

    async with async_db() as db:
        delivery = await db.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return {"status": "missing"}
        delivery.attempts += 1
        delivery.last_status_code = (
            status_code if isinstance(status_code, int) else None
        )
        if result.get("success") is True:
            delivery.status = "delivered"
            delivery.delivered_at = now
            delivery.last_error = None
            await db.commit()
            LOG.info(
                f"Lifecycle webhook delivery {delivery.id} succeeded "
                f"(status={status_code})"
            )
            return {"status": "delivered", "status_code": status_code}

        delivery.last_error = error[:1000]
        if (
            not _retryable(delivery.last_status_code)
            or delivery.attempts >= MAX_DELIVERY_ATTEMPTS
        ):
            delivery.status = "failed"
            await db.commit()
            LOG.warning(
                f"Lifecycle webhook delivery {delivery.id} failed permanently: {error}"
            )
            return {"status": "failed", "status_code": status_code, "error": error}

        delay = RETRY_DELAYS[min(delivery.attempts - 1, len(RETRY_DELAYS) - 1)]
        retry_after = result.get("retry_after_seconds")
        if isinstance(retry_after, int) and retry_after >= 0:
            delay = timedelta(seconds=min(retry_after, 24 * 60 * 60))
        delivery.next_attempt_at = now + delay
        next_attempt_at = delivery.next_attempt_at
        await db.commit()

    await enqueue_delivery(
        delivery_id,
        scheduled_at=next_attempt_at,
        endpoint_id=delivery.endpoint_id,
    )
    LOG.warning(
        f"Lifecycle webhook delivery {delivery_id} failed; retrying at "
        f"{next_attempt_at.isoformat()}: {error}"
    )
    return {"status": "retrying", "status_code": status_code, "error": error}
