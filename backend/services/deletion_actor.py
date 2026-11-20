from dataclasses import dataclass

from backend.core.api_tokens import ApiPrincipal
from backend.database.models import User


@dataclass(frozen=True, slots=True)
class DeletionActor:
    name: str
    user_id: int | None = None
    token_id: int | None = None

    @classmethod
    def from_user(cls, user: User) -> "DeletionActor":
        return cls(name=user.username, user_id=user.id)

    @classmethod
    def from_token(cls, principal: ApiPrincipal) -> "DeletionActor":
        return cls(name=principal.name, token_id=principal.token_id)

    @property
    def label(self) -> str:
        return self.name if self.token_id is None else f"API token: {self.name}"
