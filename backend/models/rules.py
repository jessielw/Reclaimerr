from pydantic import BaseModel


class ValidateRegexRequest(BaseModel):
    base_path: str = ""
    suffix: str = ""


class ValidateRegexResponse(BaseModel):
    valid: bool
    error: str | None = None
    pattern: str | None = None
