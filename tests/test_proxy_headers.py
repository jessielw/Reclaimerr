from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import backend.api.main as api_main


async def _oidc_start(request: Request) -> PlainTextResponse:
    return PlainTextResponse(str(request.url_for("oidc_callback")))


async def _scheme(request: Request) -> PlainTextResponse:
    return PlainTextResponse(request.url.scheme)


async def _oidc_callback(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def _build_client(*, trusted_hosts: list[str] | str) -> TestClient:
    base_app = Starlette(
        routes=[
            Route("/api/auth/oidc/start", _oidc_start, name="oidc_start"),
            Route("/scheme", _scheme, name="scheme"),
            Route("/api/auth/oidc/callback", _oidc_callback, name="oidc_callback"),
        ]
    )
    wrapped_app = api_main._wrap_proxy_headers(  # pyright: ignore[reportPrivateUsage]
        base_app.__call__,
        trusted_hosts=trusted_hosts,
    )
    return TestClient(
        wrapped_app,
        base_url="http://testserver",
    )


def test_trusted_proxy_uses_forwarded_proto_for_callback_uri() -> None:
    client = _build_client(trusted_hosts="*")

    scheme_response = client.get(
        "/scheme",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "203.0.113.10",
        },
    )
    response = client.get(
        "/api/auth/oidc/start",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "203.0.113.10",
        },
    )

    assert scheme_response.status_code == 200
    assert scheme_response.text == "https"
    assert response.status_code == 200
    assert response.text == "https://testserver/api/auth/oidc/callback"


def test_untrusted_proxy_ignores_forwarded_proto_for_callback_uri() -> None:
    client = _build_client(trusted_hosts=["10.10.10.3"])

    response = client.get(
        "/api/auth/oidc/start",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "203.0.113.10",
        },
    )

    assert response.status_code == 200
    assert response.text == "http://testserver/api/auth/oidc/callback"
