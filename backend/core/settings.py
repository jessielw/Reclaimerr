from __future__ import annotations

import ipaddress
import logging
import os
import re
import secrets
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.core.__version__ import __version__
from backend.enums import LogLevel

# Resolve the secrets file path at import time using the same DATA_DIR the app
# will use, so a custom DATA_DIR (e.g. /var/reclaimerr) is honoured on restarts.
_SECRETS_ENV = Path(os.environ.get("DATA_DIR", "./data")) / "secrets.env"
_HTTP_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class Settings(BaseSettings):
    """Bootstrap settings loaded from environment variables."""

    # application data directory
    data_dir: Path = Field(
        default=Path("./data"), description="Directory for database, logs, cache."
    )

    # static directory
    static_dir: Path = Field(
        default=Path("./data/static"), description="Directory for static files."
    )

    # optional: path to built frontend dist folder (enables SPA serving)
    frontend_dist: Path | None = Field(
        default=None,
        description="Path to built frontend dist/ folder. When set, serves the SPA.",
    )

    # avatars directory
    avatars_dir: Path = Field(
        default=Path("./data/static/avatars"), description="Directory for user avatars."
    )

    # logging
    log_level: str = Field(
        default="INFO", description="Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL."
    )
    log_retention_days: int = Field(
        default=30,
        description="Number of days of rotated logs to retain (minimum 1).",
    )

    # admin
    admin_password: str | None = Field(
        default=None, description="Initial admin password."
    )

    # API configuration
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    command_workers: int = Field(
        default=2,
        validation_alias="RECLAIMERR_COMMAND_WORKERS",
        description="Number of internal durable command executors (1-8).",
    )
    cors_origins: str = Field(
        default="*",
        description=(
            "Comma-separated list of allowed CORS origins (e.g., 'https://app.example.com'). "
            "Avoid '*' in production."
        ),
    )
    proxy_trusted_hosts: str = Field(
        default="127.0.0.1,::1",
        description=(
            "Comma-separated trusted proxy IPs/CIDRs (or '*') used for "
            "X-Forwarded-* headers."
        ),
    )

    # trusted reverse-proxy authentication
    forward_auth_enabled: bool = Field(
        default=False,
        description=(
            "Trust a reverse proxy supplied user header for UI authentication. "
            "Requires FORWARD_AUTH_TRUSTED_PROXIES."
        ),
    )
    forward_auth_user_header: str = Field(
        default="Remote-User",
        description="Header containing the authenticated proxy username.",
    )
    forward_auth_trusted_proxies: str = Field(
        default="",
        description=(
            "Comma-separated direct proxy IPs or CIDRs allowed to supply the "
            "forward-auth user header. Wildcards and all-address ranges such as "
            "0.0.0.0/0 are not accepted."
        ),
    )

    # JWT authentication
    jwt_secret: str = Field(
        default="",
        description="Secret key for JWT tokens (min 32 characters). Auto generated if not set.",
    )
    jwt_algorithm: str = "HS256"

    # cookie security (set to True in production behind HTTPS)
    cookie_secure: bool = Field(
        default=False,
        description="Set to True when serving behind HTTPS to mark auth cookies as Secure.",
    )

    # TMDB API configuration
    tmdb_api_key: str | None = Field(
        default=None,
        description="TMDB API bearer token (read access token from https://www.themoviedb.org/settings/api).",
    )

    # encryption key for sensitive DB fields
    encryption_key: str = Field(
        default="",
        description="Key for encrypting sensitive DB fields (min 32 characters). Auto generated if not set.",
    )

    model_config = SettingsConfigDict(
        # _SECRETS_ENV is loaded first so .env user values always win.
        # The model_validator below writes this file on first run so secrets
        # survive restarts without requiring the user to set anything manually.
        # _SECRETS_ENV is resolved at import time from DATA_DIR so custom paths work.
        env_file=[str(_SECRETS_ENV), ".env"],
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def _validate_forward_auth_config(self) -> Settings:
        """Fail closed when trusted-header auth has no explicit trust boundary."""
        if self.forward_auth_enabled and not self.forward_auth_trusted_proxies_list:
            raise PydanticCustomError(
                "forward_auth_missing_trusted_proxies",
                (
                    "FORWARD_AUTH_TRUSTED_PROXIES must contain at least one proxy "
                    "IP or CIDR when FORWARD_AUTH_ENABLED is true"
                ),
            )
        return self

    @model_validator(mode="after")
    def _persist_generated_secrets(self) -> Settings:
        """Write <data_dir>/secrets.env on first run so generated keys survive restarts."""
        secrets_path = self.data_dir / "secrets.env"
        if not secrets_path.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            secrets_path.write_text(
                "# Auto-generated by Reclaimerr on first launch. Do not share.\n"
                f"JWT_SECRET={self.jwt_secret}\n"
                f"ENCRYPTION_KEY={self.encryption_key}\n"
            )
        return self

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level is valid."""
        normalized = v.strip().upper()
        if normalized in LogLevel.__members__:
            return normalized

        accepted_values = ", ".join(LogLevel.__members__.keys())
        logging.getLogger("reclaimerr").warning(
            "Invalid LOG_LEVEL value %r. Falling back to INFO. Accepted values: %s",
            v,
            accepted_values,
        )
        return "INFO"

    @field_validator("log_retention_days")
    @classmethod
    def validate_log_retention_days(cls, v: int) -> int:
        """Clamp log retention to a safe minimum."""
        return max(1, v)

    @field_validator("command_workers", mode="before")
    @classmethod
    def validate_command_workers(cls, v: object) -> int:
        """Parse and clamp the advanced command executor count."""
        try:
            parsed = int(str(v).strip())
        except (TypeError, ValueError):
            logging.getLogger("reclaimerr").warning(
                "Invalid RECLAIMERR_COMMAND_WORKERS value %r. Falling back to 2.",
                v,
            )
            return 2
        clamped = min(8, max(1, parsed))
        if clamped != parsed:
            logging.getLogger("reclaimerr").warning(
                "RECLAIMERR_COMMAND_WORKERS=%s is outside the supported range "
                "1-8; using %s.",
                parsed,
                clamped,
            )
        return clamped

    @field_validator("forward_auth_user_header", mode="before")
    @classmethod
    def validate_forward_auth_user_header(cls, v: object) -> str:
        """Require one syntactically valid HTTP header name."""
        value = str(v).strip()
        if not value or len(value) > 128 or not _HTTP_HEADER_NAME_RE.fullmatch(value):
            raise PydanticCustomError(
                "invalid_forward_auth_header",
                "FORWARD_AUTH_USER_HEADER must be a valid HTTP header name",
            )
        return value

    @field_validator("forward_auth_trusted_proxies", mode="before")
    @classmethod
    def validate_forward_auth_trusted_proxies(cls, v: object) -> str:
        """Validate explicit IP and CIDR entries used for identity-header trust."""
        raw = "" if v is None else str(v).strip()
        if not raw:
            return ""

        entries = [entry.strip() for entry in raw.split(",") if entry.strip()]
        for entry in entries:
            if entry == "*":
                raise PydanticCustomError(
                    "unsafe_forward_auth_proxy_wildcard",
                    "FORWARD_AUTH_TRUSTED_PROXIES does not accept '*' or "
                    "all-address ranges such as 0.0.0.0/0",
                )
            try:
                network = ipaddress.ip_network(entry, strict=False)
            except ValueError as exc:
                raise PydanticCustomError(
                    "invalid_forward_auth_proxy",
                    "Invalid proxy IP or CIDR in FORWARD_AUTH_TRUSTED_PROXIES: {entry}",
                    {"entry": entry},
                ) from exc
            if network.prefixlen == 0:
                raise PydanticCustomError(
                    "unsafe_forward_auth_proxy_wildcard",
                    "FORWARD_AUTH_TRUSTED_PROXIES does not accept '*' or "
                    "all-address ranges such as {entry}",
                    {"entry": entry},
                )
        return ",".join(entries)

    @field_validator("jwt_secret", mode="before")
    @classmethod
    def validate_jwt_secret(cls, v: str | None) -> str:
        return cls._resolve_secret(v, "jwt_secret")

    @field_validator("encryption_key", mode="before")
    @classmethod
    def validate_encryption_key(cls, v: str | None) -> str:
        return cls._resolve_secret(v, "encryption_key")

    @property
    def data_dir_path(self) -> Path:
        """Get data directory as Path object (ensures directory exists)."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir

    @property
    def static_dir_path(self) -> Path:
        """Get static directory as Path object (ensures directory/sub directories exists)."""
        self.static_dir.mkdir(parents=True, exist_ok=True)
        return self.static_dir

    @property
    def avatars_dir_path(self) -> Path:
        """Get avatars directory as Path object (ensures directory exists)."""
        self.avatars_dir.mkdir(parents=True, exist_ok=True)
        return self.avatars_dir

    @property
    def db_path(self) -> Path:
        """Get database file path."""
        db_path = self.data_dir_path / "database" / "reclaimerr.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return db_path

    @property
    def log_level_enum(self) -> LogLevel:
        """Get log level as enum."""
        try:
            return LogLevel[self.log_level]
        except KeyError:
            return LogLevel.INFO

    @property
    def log_dir(self) -> Path:
        """Get log directory path."""
        log_path = self.data_dir_path / "logs"
        log_path.mkdir(parents=True, exist_ok=True)
        return log_path

    @property
    def cors_origins_list(self) -> list[str]:
        """Convert comma-separated CORS origins to list."""
        if not self.cors_origins or self.cors_origins == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",")]

    @property
    def proxy_trusted_hosts_list(self) -> list[str]:
        """Convert comma-separated trusted proxy hosts to list."""
        raw = (self.proxy_trusted_hosts or "").strip()
        if not raw:
            return ["127.0.0.1", "::1"]
        hosts = [item.strip() for item in raw.split(",") if item.strip()]
        return hosts or ["127.0.0.1", "::1"]

    @property
    def forward_auth_trusted_proxies_list(self) -> list[str]:
        """Return the explicit reverse proxies trusted to assert user identity."""
        return [
            item.strip()
            for item in self.forward_auth_trusted_proxies.split(",")
            if item.strip()
        ]

    @property
    def version(self) -> str:
        """Get application version."""
        return str(__version__)

    @staticmethod
    def _resolve_secret(v: str | None, field: str) -> str:
        """
        Return the secret value, auto-generating one only when genuinely absent.

        If a value is explicitly set (even a weak one) it is used as is so that
        existing encrypted data remains readable. A random secret is only
        generated when nothing was provided at all.
        """
        # we're avoiding importing LOG here to prevent circular imports, but we still want
        # to log through the same logger ASAP that's in backend.core.settings.logger (LOG)
        _log = logging.getLogger("reclaimerr")

        if isinstance(v, str):
            v = v.strip()

        if not v:
            # truly absent (safe to generate a new key)
            _log.warning(
                "%s is not set - generating a random value. "
                "Set %s in your .env / secrets.env for a stable secret across restarts.",
                field,
                field,
            )
            return secrets.token_hex(32)

        # value was explicitly provided (reject if too short)
        if len(v) < 32:
            raise PydanticCustomError(
                "field_too_short",
                (
                    "{field} is set but shorter than 32 characters. Provide a strong random value (e.g. "
                    "`openssl rand -hex 32`) or remove it to auto-generate one."
                ),
                {"field": field},
            )

        return v


# global settings instance
settings = Settings()
