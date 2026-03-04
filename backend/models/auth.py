import re
from datetime import datetime
from typing import Any

from pydantic import (
    BaseModel,
    EmailStr,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from backend.enums import Permission, UserRole

PASSWORD_REGEX = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[\W_]).{8,64}$")
NAME_REGEX = re.compile(r"^[a-zA-Z0-9_-]+$")


class UsernameMixin:
    @classmethod
    def validate_username(cls, v: str) -> str:
        MIN_LEN = 5
        MAX_LEN = 32
        stripped = v.strip()
        user_len = len(stripped)
        if user_len < MIN_LEN or user_len > MAX_LEN:
            raise PydanticCustomError(
                "username_length",
                "Username must be between {min_len} and {max_len} characters long",
                {"min_len": MIN_LEN, "max_len": MAX_LEN},
            )
        if not re.match(NAME_REGEX, stripped):
            raise PydanticCustomError(
                "username_format",
                "Username can only contain letters, numbers, underscores, and hyphens",
            )
        return stripped


class DisplayNameMixin:
    @classmethod
    def validate_display_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        stripped = v.strip()
        if not stripped:
            return None
        MIN_LEN = 3
        MAX_LEN = 32
        name_len = len(stripped)
        if name_len < MIN_LEN or name_len > MAX_LEN:
            raise PydanticCustomError(
                "display_name_length",
                "Display name must be between {min_len} and {max_len} characters long",
                {"min_len": MIN_LEN, "max_len": MAX_LEN},
            )
        return stripped


class PasswordValidationMixin:
    @classmethod
    def validate_password(cls, v: str) -> str:
        pw_stripped = v.strip()
        MIN_LEN = 8
        MAX_LEN = 64
        pw_len = len(pw_stripped)
        if pw_len < MIN_LEN or pw_len > MAX_LEN:
            raise PydanticCustomError(
                "password_length",
                "Password must be between {min_len} and {max_len} characters long",
                {"min_len": MIN_LEN, "max_len": MAX_LEN},
            )
        if not re.match(PASSWORD_REGEX, pw_stripped):
            raise PydanticCustomError(
                "password_complexity",
                "Password must contain at least one lowercase letter, one uppercase letter, one digit, "
                "and one special character",
            )
        return pw_stripped


class LoginRequest(BaseModel, UsernameMixin, PasswordValidationMixin):
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def username_validation(cls, v: str) -> str:
        return cls.validate_username(v)

    @field_validator("password")
    @classmethod
    def password_validation(cls, v: str) -> str:
        return cls.validate_password(v)


class UserInfo(BaseModel, UsernameMixin, DisplayNameMixin):
    id: int
    username: str
    display_name: str | None
    email: EmailStr | None
    avatar_path: str | None
    role: UserRole
    permissions: list[Permission] = []
    created_at: datetime
    require_password_change: bool

    @field_validator("username")
    @classmethod
    def username_validation(cls, v: str) -> str:
        return cls.validate_username(v)

    @field_validator("display_name")
    @classmethod
    def display_name_validation(cls, v: str | None) -> str | None:
        return cls.validate_display_name(v)

    @field_validator("email", mode="before")
    @classmethod
    def empty_email_to_none(cls, v: str | None) -> str | None:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("permissions", mode="before")
    @classmethod
    def coerce_permissions(
        cls, value: list[Permission] | list[str] | tuple[str, ...] | None
    ) -> list[Permission]:
        """Coerce raw DB permission strings to Permission enums."""
        if not value:
            return []

        parsed: list[Permission] = []
        for perm in value:
            if isinstance(perm, Permission):
                parsed.append(perm)
                continue
            try:
                parsed.append(Permission(perm))
            except ValueError:
                continue
        return parsed

    @computed_field
    @property
    def avatar_url(self) -> str | None:
        """Generate full URL for avatar if avatar_path exists."""
        if self.avatar_path:
            return f"/avatars/{self.avatar_path}"
        return None

    @classmethod
    def from_user(cls, user: Any) -> "UserInfo":
        """Build UserInfo from DB user-like objects with string permissions."""
        return cls(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            email=user.email,
            avatar_path=user.avatar_path,
            role=user.role,
            permissions=cls.coerce_permissions(user.permissions),
            created_at=user.created_at,
            require_password_change=user.require_password_change or False,
        )


class AuthResponse(BaseModel):
    user: UserInfo


class CreateUserRequest(
    BaseModel, UsernameMixin, DisplayNameMixin, PasswordValidationMixin
):
    username: str
    password: str
    display_name: str | None = None
    email: EmailStr | None = None
    role: UserRole
    permissions: list[Permission] = []
    require_password_change: bool = True

    @field_validator("username")
    @classmethod
    def username_validation(cls, v: str) -> str:
        return cls.validate_username(v)

    @field_validator("display_name")
    @classmethod
    def display_name_validation(cls, v: str | None) -> str | None:
        return cls.validate_display_name(v)

    @field_validator("email", mode="before")
    @classmethod
    def empty_email_to_none(cls, v: str | None) -> str | None:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("password")
    @classmethod
    def password_validation(cls, v: str) -> str:
        return cls.validate_password(v)

    @model_validator(mode="after")
    def sanitize_fields(self) -> "CreateUserRequest":
        self.permissions = list(dict.fromkeys(self.permissions))
        return self


class UpdateUserRequest(BaseModel, DisplayNameMixin, PasswordValidationMixin):
    display_name: str | None = None
    email: EmailStr | None = None
    role: UserRole
    permissions: list[Permission] = []
    password: str | None = None

    @field_validator("display_name")
    @classmethod
    def display_name_validation(cls, v: str | None) -> str | None:
        return cls.validate_display_name(v)

    @field_validator("email", mode="before")
    @classmethod
    def empty_email_to_none(cls, v: str | None) -> str | None:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("password")
    @classmethod
    def password_validation(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return cls.validate_password(v)

    @model_validator(mode="after")
    def sanitize_fields(self) -> "UpdateUserRequest":
        self.permissions = list(dict.fromkeys(self.permissions))
        return self


class ChangeProfileInfoRequest(BaseModel, DisplayNameMixin):
    display_name: str | None = None
    email: EmailStr | None = None

    @field_validator("display_name")
    @classmethod
    def display_name_validation(cls, v: str | None) -> str | None:
        return cls.validate_display_name(v)

    @field_validator("email", mode="before")
    @classmethod
    def empty_email_to_none(cls, v: str | None) -> str | None:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class ChangePasswordRequest(BaseModel, PasswordValidationMixin):
    old_password: str | None = None
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_validation(cls, v: str) -> str:
        return cls.validate_password(v)

    @model_validator(mode="after")
    def sanitize_fields(self) -> "ChangePasswordRequest":
        if self.old_password is not None:
            self.old_password = self.old_password.strip() or None
        return self


# class LinkJellyfinRequest(BaseModel):
#     username: str
#     password: str
