from fastapi import HTTPException, status
from pydantic import BaseModel

from backend.models.auth import PasswordValidationMixin


class SetupRequest(PasswordValidationMixin, BaseModel):
    password: str
    confirm_password: str

    def validate_fields(self) -> None:
        self.password = self.validate_password(self.password)
        if self.password != self.confirm_password:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Passwords do not match",
            )


class SetupStatusResponse(BaseModel):
    needs_setup: bool
