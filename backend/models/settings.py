from pydantic import BaseModel, model_validator

from backend.enums import Service


class ServiceConfigUpdate(BaseModel):
    service_type: Service
    base_url: str
    api_key: str
    enabled: bool

    @model_validator(mode="after")
    def sanitize_fields(self) -> "ServiceConfigUpdate":
        """Sanitize fields after model initialization."""
        self.base_url = self.base_url.strip()
        self.api_key = self.api_key.strip()
        return self
