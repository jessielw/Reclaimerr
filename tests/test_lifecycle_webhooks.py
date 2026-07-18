from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    LifecycleEvent,
    WebhookDelivery,
    WebhookEndpoint,
)
from backend.services.lifecycle_webhooks import run_webhook_delivery_job


def test_temporary_webhook_failure_is_persisted_and_retried(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db:
            endpoint = WebhookEndpoint(
                name="Automation",
                url_template="https://automation.example/webhook",
                event_types=["candidate.scheduled"],
                media_types=["movie"],
            )
            event = LifecycleEvent(
                event_id="event-1",
                event_type="candidate.scheduled",
                payload={
                    "schema_version": 1,
                    "id": "event-1",
                    "type": "candidate.scheduled",
                    "candidate": {"id": 1, "media_type": "movie"},
                },
            )
            db.add_all([endpoint, event])
            await db.flush()
            delivery = WebhookDelivery(
                event_id=event.id,
                endpoint_id=endpoint.id,
                next_attempt_at=datetime.now(UTC),
            )
            db.add(delivery)
            await db.commit()

        @asynccontextmanager
        async def test_db():
            async with session_maker() as session:
                yield session

        monkeypatch.setattr("backend.services.lifecycle_webhooks.async_db", test_db)
        send_mock = AsyncMock(
            return_value={
                "success": False,
                "status_code": 503,
                "error": "HTTP 503",
                "retry_after_seconds": 120,
            }
        )
        enqueue_mock = AsyncMock()
        monkeypatch.setattr(
            "backend.services.lifecycle_webhooks.send_webhook_payload", send_mock
        )
        monkeypatch.setattr(
            "backend.services.lifecycle_webhooks.enqueue_delivery", enqueue_mock
        )

        result = await run_webhook_delivery_job(delivery.id)
        assert result["status"] == "retrying"
        enqueue_mock.assert_awaited_once()

        async with session_maker() as db:
            persisted = await db.get(WebhookDelivery, delivery.id)
            assert persisted is not None
            assert persisted.status == "pending"
            assert persisted.attempts == 1
            assert persisted.last_status_code == 503
            assert persisted.next_attempt_at > datetime.now(UTC).replace(tzinfo=None)

        send_mock.return_value = {
            "success": True,
            "status_code": 204,
            "error": None,
        }
        result = await run_webhook_delivery_job(delivery.id)
        assert result == {"status": "delivered", "status_code": 204}
        async with session_maker() as db:
            persisted = await db.get(WebhookDelivery, delivery.id)
            assert persisted is not None
            assert persisted.status == "delivered"
            assert persisted.attempts == 2
            assert persisted.delivered_at is not None

        await engine.dispose()

    asyncio.run(run())
