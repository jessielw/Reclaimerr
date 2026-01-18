from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class UserInfo(BaseModel):
    id: int
    username: str
    display_name: str | None
    email: str | None
    avatar_url: str | None
    role: str
    require_password_change: bool
    # jellyfin_linked: bool


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserInfo


class CreateUserRequest(BaseModel):
    username: str
    email: str | None = None
    password: str
    role: str = "user"
    require_password_change: bool = True


class ChangePasswordRequest(BaseModel):
    old_password: str | None = None
    new_password: str


# class LinkJellyfinRequest(BaseModel):
#     username: str
#     password: str
