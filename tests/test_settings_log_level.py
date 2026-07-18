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


def test_command_workers_defaults_to_three(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("RECLAIMERR_COMMAND_WORKERS", raising=False)

    settings = _build_settings(tmp_path)

    assert settings.command_workers == 3


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


def test_invalid_command_workers_falls_back_to_three(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RECLAIMERR_COMMAND_WORKERS", "many")

    settings = _build_settings(tmp_path)

    assert settings.command_workers == 3
