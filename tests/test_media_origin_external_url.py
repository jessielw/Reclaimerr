from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.database.models import ServiceConfig
from backend.enums import MediaType, Service
from backend.models.settings import ServiceConfigUpdate
from backend.services.media_origins import MediaOriginLookup, _link_base_url


def _config(extra_settings: dict | None) -> ServiceConfig:
    return ServiceConfig(
        service_type=Service.SONARR,
        base_url="http://sonarr:8989",
        api_key="key",
        name="Sonarr",
        enabled=True,
        extra_settings=extra_settings,
    )


def test_link_base_url_prefers_external_url() -> None:
    config = _config({"timeout": 300, "external_url": "http://192.168.1.2:8989"})
    assert _link_base_url(config) == "http://192.168.1.2:8989"


@pytest.mark.parametrize(
    "extra_settings",
    [None, {}, {"timeout": 300}, {"external_url": ""}, {"external_url": "   "}],
)
def test_link_base_url_falls_back_to_base_url(extra_settings: dict | None) -> None:
    assert _link_base_url(_config(extra_settings)) == "http://sonarr:8989"


def test_seerr_links_use_link_base_url() -> None:
    seerr = ServiceConfig(
        service_type=Service.SEERR,
        base_url="http://seerr:5055",
        api_key="key",
        name="Seerr",
        enabled=True,
        extra_settings={"external_url": "https://requests.example.com"},
    )
    lookup = MediaOriginLookup(seerr_configs={1: ("Seerr", _link_base_url(seerr))})
    links = lookup.seerr_links(MediaType.SERIES, 42)
    assert [link.item_url for link in links] == ["https://requests.example.com/tv/42"]


def test_service_config_update_normalizes_external_url() -> None:
    update = ServiceConfigUpdate(
        service_type=Service.RADARR,
        base_url="http://radarr:7878",
        enabled=True,
        extra_settings={"timeout": 300, "external_url": "  http://10.0.0.5:7878/  "},
    )
    assert update.extra_settings == {
        "timeout": 300,
        "external_url": "http://10.0.0.5:7878",
    }


def test_service_config_update_rejects_external_url_without_scheme() -> None:
    with pytest.raises(ValidationError, match="must include a scheme"):
        ServiceConfigUpdate(
            service_type=Service.RADARR,
            base_url="http://radarr:7878",
            enabled=True,
            extra_settings={"external_url": "192.168.1.2:7878"},
        )
