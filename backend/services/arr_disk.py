from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.enums import Service

__all__ = ["ArrDiskError", "ArrDiskSpace", "load_arr_disk_space"]


@dataclass(slots=True, frozen=True)
class ArrDiskError:
    """One Arr instance that could not be reached for disk stats."""

    service_type: str
    service_config_id: int
    message: str


@dataclass(slots=True)
class ArrDiskSpace:
    """Mount entries from every reachable Arr, plus whoever did not answer."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    errors: list[ArrDiskError] = field(default_factory=list)


async def load_arr_disk_space() -> ArrDiskSpace:
    """Fetch disk space from all configured Radarr/Sonarr instances.

    Each entry retains its service type and config ID so scoped path mappings
    and provider-specific rule targets can be resolved without conflating two
    Arr instances that happen to expose the same container path.

    An instance that cannot be reached is recorded rather than raised: rule
    evaluation carries on with whatever it did get, and the storage page shows
    the gap instead of failing outright.
    """
    radarr_clients = service_manager.radarr_clients()
    if not radarr_clients and service_manager.radarr:
        radarr_clients = {0: service_manager.radarr}
    sonarr_clients = service_manager.sonarr_clients()
    if not sonarr_clients and service_manager.sonarr:
        sonarr_clients = {0: service_manager.sonarr}

    result = ArrDiskSpace()

    client_groups = (
        (Service.RADARR, radarr_clients),
        (Service.SONARR, sonarr_clients),
    )
    for service_type, clients in client_groups:
        for config_id, client in clients.items():
            try:
                for entry in await client.get_disk_space():
                    path = str(entry.get("path", "") or "")
                    if not path:
                        continue
                    result.entries.append(
                        {
                            **entry,
                            "path": path,
                            "service_type": service_type.value,
                            "service_config_id": config_id,
                        }
                    )
            except Exception as exc:
                LOG.debug(
                    f"Could not load {service_type.value} disk-space data "
                    f"for config {config_id}: {exc}"
                )
                result.errors.append(
                    ArrDiskError(
                        service_type=service_type.value,
                        service_config_id=config_id,
                        message=str(exc),
                    )
                )

    result.entries.sort(key=lambda e: -len(str(e.get("path") or "")))
    return result
