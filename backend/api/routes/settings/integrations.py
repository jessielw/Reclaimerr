from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.api_tokens import API_TOKEN_SCOPES, generate_api_token
from backend.core.auth import require_admin
from backend.core.encryption import fer_encrypt
from backend.database import get_db
from backend.database.models import (
    ApiToken,
    LifecycleEvent,
    User,
    WebhookDelivery,
    WebhookEndpoint,
)
from backend.models.integrations import (
    ApiTokenCreate,
    ApiTokenCreatedResponse,
    ApiTokenResponse,
    WebhookDeliveryResponse,
    WebhookEndpointInput,
    WebhookEndpointResponse,
    WebhookHeaderInput,
    WebhookTestResponse,
)
from backend.services.lifecycle_webhooks import (
    endpoint_config,
    enqueue_delivery,
)
from backend.services.webhook_transport import send_webhook_payload

router = APIRouter(prefix="/integrations", tags=["settings", "integrations"])
SECRET_MASK = "********"


def _token_response(row: ApiToken) -> ApiTokenResponse:
    return ApiTokenResponse(
        id=row.id,
        name=row.name,
        token_prefix=row.token_prefix,
        scopes=list(row.scopes),
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
    )


@router.get("/api-tokens", response_model=list[ApiTokenResponse])
async def list_api_tokens(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ApiTokenResponse]:
    rows = (
        (await db.execute(select(ApiToken).order_by(ApiToken.created_at.desc())))
        .scalars()
        .all()
    )
    return [_token_response(row) for row in rows]


@router.post(
    "/api-tokens",
    response_model=ApiTokenCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_token(
    request: ApiTokenCreate,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApiTokenCreatedResponse:
    if request.expires_at is not None:
        expires_at = request.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        else:
            expires_at = expires_at.astimezone(UTC)
        if expires_at <= datetime.now(UTC):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Token expiration must be in the future",
            )
    else:
        expires_at = None
    invalid_scopes = set(request.scopes) - API_TOKEN_SCOPES
    if invalid_scopes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported API token scopes: {sorted(invalid_scopes)}",
        )
    plaintext, prefix, digest = generate_api_token()
    row = ApiToken(
        name=request.name,
        token_prefix=prefix,
        token_hash=digest,
        scopes=list(request.scopes),
        created_by_user_id=admin.id,
        expires_at=expires_at,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return ApiTokenCreatedResponse(**_token_response(row).model_dump(), token=plaintext)


@router.delete("/api-tokens/{token_id}", response_model=ApiTokenResponse)
async def revoke_api_token(
    token_id: int,
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApiTokenResponse:
    row = await db.get(ApiToken, token_id)
    if row is None:
        raise HTTPException(status_code=404, detail="API token not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(row)
    return _token_response(row)


def _endpoint_response(row: WebhookEndpoint) -> WebhookEndpointResponse:
    config = endpoint_config(row)
    masked_headers = [
        WebhookHeaderInput(name=str(header.get("name") or ""), value=SECRET_MASK)
        for header in config.get("headers") or []
        if str(header.get("name") or "").strip()
    ]
    return WebhookEndpointResponse(
        id=row.id,
        name=row.name,
        enabled=row.enabled and row.deleted_at is None,
        method=row.method,
        url_template=row.url_template,
        event_types=list(row.event_types),
        media_types=list(row.media_types),
        path_mode=row.path_mode,
        body_template=row.body_template,
        timeout_seconds=row.timeout_seconds,
        auth_username=row.auth_username,
        auth_password_is_set=bool(row.auth_password_encrypted),
        headers=masked_headers,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _apply_endpoint_input(
    row: WebhookEndpoint, request: WebhookEndpointInput, *, creating: bool
) -> None:
    row.name = request.name
    row.enabled = request.enabled
    row.method = request.method
    row.url_template = request.url_template
    row.event_types = list(request.event_types)
    row.media_types = [media_type.value for media_type in request.media_types]
    row.path_mode = request.path_mode
    row.body_template = request.body_template
    row.timeout_seconds = request.timeout_seconds
    row.auth_username = request.auth_username
    if creating or request.auth_password is not None:
        row.auth_password_encrypted = (
            fer_encrypt(request.auth_password) if request.auth_password else None
        )
    existing_headers = {
        str(header.get("name") or "").lower(): str(header.get("value") or "")
        for header in endpoint_config(row).get("headers") or []
    }
    headers = []
    for header in request.headers:
        value = header.value
        if not creating and value == SECRET_MASK:
            value = existing_headers.get(header.name.lower(), "")
        headers.append({"name": header.name, "value": value})
    row.headers_encrypted = fer_encrypt(json.dumps(headers)) if headers else None


@router.get("/webhooks", response_model=list[WebhookEndpointResponse])
async def list_webhook_endpoints(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[WebhookEndpointResponse]:
    rows = (
        (
            await db.execute(
                select(WebhookEndpoint)
                .where(WebhookEndpoint.deleted_at.is_(None))
                .order_by(WebhookEndpoint.name, WebhookEndpoint.id)
            )
        )
        .scalars()
        .all()
    )
    return [_endpoint_response(row) for row in rows]


@router.post(
    "/webhooks",
    response_model=WebhookEndpointResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_webhook_endpoint(
    request: WebhookEndpointInput,
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookEndpointResponse:
    row = WebhookEndpoint(name=request.name, url_template=request.url_template)
    _apply_endpoint_input(row, request, creating=True)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _endpoint_response(row)


@router.put("/webhooks/{endpoint_id}", response_model=WebhookEndpointResponse)
async def update_webhook_endpoint(
    endpoint_id: int,
    request: WebhookEndpointInput,
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookEndpointResponse:
    row = await db.get(WebhookEndpoint, endpoint_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    _apply_endpoint_input(row, request, creating=False)
    await db.commit()
    await db.refresh(row)
    return _endpoint_response(row)


@router.delete("/webhooks/{endpoint_id}")
async def delete_webhook_endpoint(
    endpoint_id: int,
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    row = await db.get(WebhookEndpoint, endpoint_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    row.enabled = False
    row.deleted_at = datetime.now(UTC)
    deliveries = (
        (
            await db.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.endpoint_id == endpoint_id,
                    WebhookDelivery.status == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    for delivery in deliveries:
        delivery.status = "canceled"
        delivery.last_error = "Webhook endpoint was deleted"
    await db.commit()
    return {"message": "Webhook endpoint deleted"}


def _sample_webhook_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": "test-event",
        "type": "candidate.scheduled",
        "occurred_at": datetime.now(UTC).isoformat(),
        "candidate": {
            "id": 0,
            "media_type": "movie",
            "title": "Reclaimerr Test Movie",
            "tmdb_id": 550,
            "auto_delete_state": "scheduled",
        },
        "links": {"self": "/api/v1/candidates/0"},
    }


@router.post("/webhooks/test", response_model=WebhookTestResponse)
async def test_webhook_endpoint(
    request: WebhookEndpointInput,
    _admin: Annotated[User, Depends(require_admin)],
) -> WebhookTestResponse:
    config = {
        **request.model_dump(mode="json"),
        "headers": [header.model_dump() for header in request.headers],
    }
    result = await send_webhook_payload(config, _sample_webhook_payload())
    return WebhookTestResponse.model_validate(result)


@router.post("/webhooks/{endpoint_id}/test", response_model=WebhookTestResponse)
async def test_saved_webhook_endpoint(
    endpoint_id: int,
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookTestResponse:
    endpoint = await db.get(WebhookEndpoint, endpoint_id)
    if endpoint is None or endpoint.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    result = await send_webhook_payload(
        endpoint_config(endpoint), _sample_webhook_payload()
    )
    return WebhookTestResponse.model_validate(result)


@router.get("/webhook-deliveries", response_model=list[WebhookDeliveryResponse])
async def list_webhook_deliveries(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[WebhookDeliveryResponse]:
    rows = (
        await db.execute(
            select(WebhookDelivery, LifecycleEvent, WebhookEndpoint)
            .join(LifecycleEvent, LifecycleEvent.id == WebhookDelivery.event_id)
            .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        WebhookDeliveryResponse(
            id=delivery.id,
            event_id=event.event_id,
            event_type=event.event_type,
            endpoint_id=endpoint.id,
            endpoint_name=endpoint.name,
            status=delivery.status,
            attempts=delivery.attempts,
            next_attempt_at=delivery.next_attempt_at,
            last_status_code=delivery.last_status_code,
            last_error=delivery.last_error,
            delivered_at=delivery.delivered_at,
            created_at=delivery.created_at,
        )
        for delivery, event, endpoint in rows
    ]


@router.post(
    "/webhook-deliveries/{delivery_id}/retry",
    response_model=WebhookDeliveryResponse,
)
async def retry_webhook_delivery(
    delivery_id: int,
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WebhookDeliveryResponse:
    row = (
        await db.execute(
            select(WebhookDelivery, LifecycleEvent, WebhookEndpoint)
            .join(LifecycleEvent, LifecycleEvent.id == WebhookDelivery.event_id)
            .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
            .where(WebhookDelivery.id == delivery_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Webhook delivery not found")
    delivery, event, endpoint = row
    if endpoint.deleted_at is not None or not endpoint.enabled:
        raise HTTPException(status_code=409, detail="Webhook endpoint is disabled")
    delivery.status = "pending"
    delivery.attempts = 0
    delivery.next_attempt_at = datetime.now(UTC)
    delivery.last_error = None
    delivery.last_status_code = None
    delivery.delivered_at = None
    await db.commit()
    await enqueue_delivery(
        delivery.id,
        scheduled_at=delivery.next_attempt_at,
        endpoint_id=endpoint.id,
    )
    return WebhookDeliveryResponse(
        id=delivery.id,
        event_id=event.event_id,
        event_type=event.event_type,
        endpoint_id=endpoint.id,
        endpoint_name=endpoint.name,
        status=delivery.status,
        attempts=delivery.attempts,
        next_attempt_at=delivery.next_attempt_at,
        last_status_code=delivery.last_status_code,
        last_error=delivery.last_error,
        delivered_at=delivery.delivered_at,
        created_at=delivery.created_at,
    )
