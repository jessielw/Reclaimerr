from __future__ import annotations

from backend.core.api_tokens import API_TOKEN_SCOPES
from backend.models.integrations import ApiTokenCreate


def test_every_known_scope_can_be_requested_when_creating_a_token():
    token = ApiTokenCreate(name="all", scopes=sorted(API_TOKEN_SCOPES))

    assert set(token.scopes) == API_TOKEN_SCOPES
