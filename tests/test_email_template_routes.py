from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.routes.settings.notifications import limiter, router
from backend.core.auth import require_admin
from backend.database import Base, get_db
from backend.services import email_templates as et

pytestmark = pytest.mark.anyio
EVENT = "delete_request_execution_succeeded"
BASE = "/notifications/email-templates"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(et, "template_dir", lambda: tmp_path)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def db():
        async with factory() as session:
            yield session

    app = FastAPI()
    app.include_router(router)
    app.state.limiter = limiter
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
        display_name="Alex", username="alex", email="alex@example.com"
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http
    await engine.dispose()


async def test_lists_events_and_starts_from_the_example(client):
    listed = (await client.get(BASE)).json()
    assert {e["event"] for e in listed["events"]} >= {EVENT}
    assert not any(e["custom"] for e in listed["events"])
    assert listed["voices"] is None and listed["example_voices"]

    detail = (await client.get(f"{BASE}/{EVENT}")).json()
    assert detail["custom"] is False
    assert detail["html"] == detail["example_html"]
    assert "{{media_title}}" in detail["html"]


async def test_save_get_and_delete_a_template(client, tmp_path):
    saved = await client.put(f"{BASE}/{EVENT}", json={"html": "<title>t</title>hi"})
    assert saved.status_code == 200
    assert (tmp_path / f"{EVENT}.html").read_text() == "<title>t</title>hi"

    detail = (await client.get(f"{BASE}/{EVENT}")).json()
    assert detail["custom"] is True and detail["html"] == "<title>t</title>hi"

    assert (await client.delete(f"{BASE}/{EVENT}")).status_code == 200
    assert not (tmp_path / f"{EVENT}.html").exists()


async def test_unknown_events_are_rejected(client, tmp_path):
    assert (await client.get(f"{BASE}/..%2Fsecrets")).status_code == 404
    assert (await client.put(f"{BASE}/other", json={"html": "x"})).status_code == 404
    assert not list(tmp_path.iterdir())


async def test_preview_renders_unsaved_text_and_checks_voices(client, tmp_path):
    body = {"html": "<title>S {{voice_subject}}</title>{{headline}} {{media_title}}"}
    voices = '[{"headline": "Bye {{media_title}}", "subline": "", "subject": "Gone"}]'

    ok = await client.post(f"{BASE}/{EVENT}/preview", json={**body, "voices": voices})
    bad = await client.post(
        f"{BASE}/{EVENT}/preview", json={**body, "voices": "not json"}
    )

    assert ok.json()["subject"] == "S Gone"
    assert "Bye Example Movie" in ok.json()["html"]
    assert bad.status_code == 400
    assert not list(tmp_path.iterdir())  # previewing never writes


async def test_preview_rejects_voices_over_the_template_size_limit(client):
    voices = json.dumps([{"headline": "x" * et.MAX_TEMPLATE_BYTES}])

    response = await client.post(
        f"{BASE}/{EVENT}/preview", json={"html": "{{headline}}", "voices": voices}
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "voices"]


async def test_voices_are_validated_before_they_are_saved(client, tmp_path):
    bad = await client.put("/notifications/email-voices", json={"text": "{}"})
    good = await client.put(
        "/notifications/email-voices",
        json={"text": '[{"headline": "h", "subline": "s", "subject": "x"}]'},
    )

    assert bad.status_code == 400
    assert good.status_code == 200 and (tmp_path / "voices.json").exists()
    assert (await client.delete("/notifications/email-voices")).status_code == 200
    assert not (tmp_path / "voices.json").exists()


async def test_test_email_needs_smtp(client):
    response = await client.post(f"{BASE}/{EVENT}/test", json={"html": "x"})
    assert response.status_code == 400
    assert "SMTP" in response.json()["detail"]


async def test_admin_can_send_a_template_to_everyone(client, tmp_path):
    url = f"{BASE}/{EVENT}/everyone"

    refused = await client.put(url, json={"enabled": True})
    await client.put(f"{BASE}/{EVENT}", json={"html": "<title>t</title>hi"})
    on = await client.put(url, json={"enabled": True})
    listed = {e["event"]: e for e in (await client.get(BASE)).json()["events"]}

    assert refused.status_code == 400  # nothing saved yet
    assert on.status_code == 200 and listed[EVENT]["everyone"] is True

    await client.delete(f"{BASE}/{EVENT}")  # removing the template turns it off
    listed = {e["event"]: e for e in (await client.get(BASE)).json()["events"]}
    assert listed[EVENT]["everyone"] is False
    assert (
        await client.put(f"{BASE}/other/everyone", json={"enabled": True})
    ).status_code == 404
