from __future__ import annotations

import logging
from pathlib import Path

import pytest

from backend.core.settings import Settings
from backend.enums import LogLevel

_JWT_SECRET = "a" * 32
_ENCRYPTION_KEY = "b" * 32
_ACCEPTED_LOG_LEVELS = "DEBUG, INFO, WARNING, ERROR, CRITICAL"


def _build_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        data_dir=tmp_path / "data",
        jwt_secret=_JWT_SECRET,
        encryption_key=_ENCRYPTION_KEY,
    )


def test_log_level_debug_resolves_to_enum(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = _build_settings(tmp_path)

    assert settings.log_level == "DEBUG"
    assert settings.log_level_enum is LogLevel.DEBUG


@pytest.mark.parametrize("value", ["debug", "DeBuG"])
def test_log_level_case_insensitive(monkeypatch, tmp_path: Path, value: str) -> None:
    monkeypatch.setenv("LOG_LEVEL", value)

    settings = _build_settings(tmp_path)

    assert settings.log_level == "DEBUG"
    assert settings.log_level_enum is LogLevel.DEBUG


def test_log_level_whitespace_is_trimmed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOG_LEVEL", "  DEBUG  ")

    settings = _build_settings(tmp_path)

    assert settings.log_level == "DEBUG"
    assert settings.log_level_enum is LogLevel.DEBUG


def test_log_level_invalid_falls_back_and_warns(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")
    caplog.set_level(logging.WARNING, logger="reclaimerr")

    settings = _build_settings(tmp_path)

    assert settings.log_level == "INFO"
    assert settings.log_level_enum is LogLevel.INFO
    assert any(
        "Invalid LOG_LEVEL value 'VERBOSE'" in record.getMessage()
        and f"Accepted values: {_ACCEPTED_LOG_LEVELS}" in record.getMessage()
        for record in caplog.records
    )


def test_log_level_empty_string_falls_back_and_warns(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "")
    caplog.set_level(logging.WARNING, logger="reclaimerr")

    settings = _build_settings(tmp_path)

    assert settings.log_level == "INFO"
    assert settings.log_level_enum is LogLevel.INFO
    assert any(
        "Invalid LOG_LEVEL value ''" in record.getMessage()
        and f"Accepted values: {_ACCEPTED_LOG_LEVELS}" in record.getMessage()
        for record in caplog.records
    )


def test_proxy_trusted_hosts_list_defaults_to_loopback(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)

    assert settings.proxy_trusted_hosts_list == ["127.0.0.1", "::1"]


def test_proxy_trusted_hosts_list_parses_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROXY_TRUSTED_HOSTS", "10.10.10.2, 10.10.10.0/24, *")

    settings = _build_settings(tmp_path)

    assert settings.proxy_trusted_hosts_list == [
        "10.10.10.2",
        "10.10.10.0/24",
        "*",
    ]


def test_forward_auth_is_safely_disabled_by_default(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)

    assert settings.forward_auth_enabled is False
    assert settings.forward_auth_user_header == "Remote-User"
    assert settings.forward_auth_trusted_proxies_list == []


def test_forward_auth_requires_explicit_trusted_proxies(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FORWARD_AUTH_ENABLED", "true")
    monkeypatch.delenv("FORWARD_AUTH_TRUSTED_PROXIES", raising=False)

    with pytest.raises(ValueError, match="FORWARD_AUTH_TRUSTED_PROXIES"):
        _build_settings(tmp_path)


def test_forward_auth_rejects_wildcard_proxy(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FORWARD_AUTH_TRUSTED_PROXIES", "*")

    with pytest.raises(ValueError, match="does not accept"):
        _build_settings(tmp_path)


@pytest.mark.parametrize("value", ["0.0.0.0/0", "::/0", "172.18.0.4,0.0.0.0/0"])
def test_forward_auth_rejects_all_address_ranges(
    monkeypatch, tmp_path: Path, value: str
) -> None:
    monkeypatch.setenv("FORWARD_AUTH_TRUSTED_PROXIES", value)

    with pytest.raises(ValueError, match="does not accept"):
        _build_settings(tmp_path)


def test_forward_auth_parses_ip_and_cidr_allowlist(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FORWARD_AUTH_TRUSTED_PROXIES", "172.18.0.4, 10.20.0.0/16, ::1")
    monkeypatch.setenv("FORWARD_AUTH_USER_HEADER", "X-Authenticated-User")

    settings = _build_settings(tmp_path)

    assert settings.forward_auth_user_header == "X-Authenticated-User"
    assert settings.forward_auth_trusted_proxies_list == [
        "172.18.0.4",
        "10.20.0.0/16",
        "::1",
    ]


def test_forward_auth_rejects_invalid_header_name(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FORWARD_AUTH_USER_HEADER", "Remote User")

    with pytest.raises(ValueError, match="valid HTTP header"):
        _build_settings(tmp_path)


def test_command_workers_defaults_to_two(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("RECLAIMERR_COMMAND_WORKERS", raising=False)

    settings = _build_settings(tmp_path)

    assert settings.command_workers == 2


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("1", 1), ("5", 5), ("8", 8), ("0", 1), ("12", 8)],
)
def test_command_workers_parses_and_clamps_environment(
    monkeypatch,
    tmp_path: Path,
    configured: str,
    expected: int,
) -> None:
    monkeypatch.setenv("RECLAIMERR_COMMAND_WORKERS", configured)

    settings = _build_settings(tmp_path)

    assert settings.command_workers == expected


def test_invalid_command_workers_falls_back_to_two(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RECLAIMERR_COMMAND_WORKERS", "many")

    settings = _build_settings(tmp_path)

    assert settings.command_workers == 2


def test_forward_auth_local_fallback_defaults_off(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)

    assert settings.forward_auth_allow_local_fallback is False


def test_forward_auth_logout_url_defaults_empty(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)

    assert settings.forward_auth_logout_url == ""


def test_forward_auth_logout_url_accepts_absolute_https(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FORWARD_AUTH_LOGOUT_URL", " https://auth.example.com/logout ")

    settings = _build_settings(tmp_path)

    assert settings.forward_auth_logout_url == "https://auth.example.com/logout"


@pytest.mark.parametrize(
    "value", ["javascript:alert(1)", "/logout", "auth.example.com/logout", "ftp://x/y"]
)
def test_forward_auth_logout_url_rejects_non_http(
    monkeypatch, tmp_path: Path, value: str
) -> None:
    monkeypatch.setenv("FORWARD_AUTH_LOGOUT_URL", value)

    with pytest.raises(ValueError, match="absolute http or https URL"):
        _build_settings(tmp_path)
