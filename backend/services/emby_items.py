"""Item metadata lookups that work with server API keys."""

from typing import Any

from backend.enums import Service


async def get_item_details(
    client: Any, service: Service, item_id: str
) -> dict[str, Any]:
    # /Items/{id} is unavailable on Emby and requires a user on Jellyfin.
    fields = "Path,MediaSources"
    if service is Service.EMBY:
        fields += ",DisplayOrder"
    payload = await client._make_request(
        "Items",
        params={"Ids": item_id, "Fields": fields, "EnableUserData": False},
        timeout=10,
    )
    rows = payload.get("Items") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError(f"{service.value} item lookup returned no unique item")
    row = rows[0]
    if (
        not isinstance(row, dict)
        or str(row.get("Id", "")).replace("-", "").casefold()
        != item_id.replace("-", "").casefold()
    ):
        raise ValueError(f"{service.value} item lookup returned a different item")
    return row
