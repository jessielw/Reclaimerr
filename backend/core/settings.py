from __future__ import annotations

import logging
import secrets
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.core.__version__ import __version__
from backend.enums import LogLevel


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

    # admin
    admin_password: str | None = Field(
        default=None, description="Initial admin password."
    )

    # API configuration
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = Field(
        default="*",
        description=(
            "Comma-separated list of allowed CORS origins (e.g., 'https://app.example.com'). "
            "Avoid '*' in production."
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
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level is valid."""
        try:
            return str(LogLevel(v.upper())).upper()
        except ValueError:
            return "INFO"

    @field_validator("jwt_secret", mode="before")
    @classmethod
    def validate_jwt_secret(cls, v: str | None) -> str:
        return cls._resolve_secret(v, "jwt_secret")

    @field_validator("encryption_key", mode="before")
    @classmethod
    def validate_encryption_key(cls, v: str | None) -> str:
        _log = logging.getLogger("reclaimerr")
        if isinstance(v, str):
            v = v.strip()
        if not v:
            # only safe moment to generate (nothing is set yet, so no existing data to corrupt)
            _log.warning(
                "ENCRYPTION_KEY is not set - generating a random value. "
                "Persist ENCRYPTION_KEY in your .env / secrets.env before storing any data. "
                "Changing the key after encrypted data exists will make that data unreadable."
            )
            return secrets.token_hex(32)
        # any explicitly set value is used as is
        return v

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
    def version(self) -> str:
        """Get application version."""
        return str(__version__)

    @staticmethod
    def _resolve_secret(v: str | None, field: str) -> str:
        """
        Return the secret value, auto-generating one only when genuinely absent.

        If a value is explicitly set (even a weak one) it is used as is so that
        existing encrypted data remains readable. A random secret is only
        generated when nothing was provided at all

        Note: desktop mode persists the generated value in secrets.env so it survives restarts.
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
