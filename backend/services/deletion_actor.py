from dataclasses import dataclass

from backend.core.api_tokens import ApiPrincipal
from backend.database.models import User


@dataclass(frozen=True, slots=True)
class DeletionActor:
    name: str
    user_id: int | None = None
    token_id: int | None = None
    # the API token's own name, kept when a user is acting through it
    token_name: str | None = None

    @classmethod
    def from_user(cls, user: User) -> "DeletionActor":
        return cls(name=user.username, user_id=user.id)

    @classmethod
    def from_token(cls, principal: ApiPrincipal) -> "DeletionActor":
        return cls(
            name=principal.name, token_id=principal.token_id, token_name=principal.name
        )

    @classmethod
    def from_token_for_user(
        cls, principal: ApiPrincipal, user: User
    ) -> "DeletionActor":
        """A user acting through an integration's token.

        The user is recorded as the actor, while the token stays on record and
        still decides what may be done and whether a schedule can run.
        """
        return cls(
            name=user.username,
            user_id=user.id,
            token_id=principal.token_id,
            token_name=principal.name,
        )

    @property
    def label(self) -> str:
        if self.token_id is None or self.user_id is not None:
            return self.name
        return f"API token: {self.name}"
