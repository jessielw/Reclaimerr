from __future__ import annotations

from backend.enums import Service
from backend.services.emby_base import _authentication_headers


def test_jellyfin_uses_mediabrowser_authorization_header() -> None:
    headers = _authentication_headers(Service.JELLYFIN, 'key with "quotes"')

    assert headers == {
        "Authorization": 'MediaBrowser Token="key%20with%20%22quotes%22"'
    }


def test_emby_keeps_its_token_header() -> None:
    assert _authentication_headers(Service.EMBY, "emby-key") == {
        "X-Emby-Token": "emby-key"
    }
