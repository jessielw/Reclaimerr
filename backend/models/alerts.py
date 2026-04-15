from pydantic import BaseModel, ConfigDict

from backend.enums import AlertLevel, Permission


class SystemAlert(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    alert_level: AlertLevel
    title: str
    message: str
    action_label: str | None = None
    action_href: str | None = None
    # internal - which permission gates this alert (not sent to client)
    required_permission: Permission | None = None
