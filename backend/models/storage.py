from __future__ import annotations

from pydantic import BaseModel


class StorageMountSource(BaseModel):
    """An Arr instance that reports a given mount."""

    service_type: str
    service_config_id: int
    name: str


class StorageMount(BaseModel):
    """One volume as an Arr reports it.

    ``used_bytes`` is null when the Arr reported free space without a total,
    which some remote and network mounts do.
    """

    path: str
    label: str | None = None
    total_bytes: int | None = None
    free_bytes: int | None = None
    used_bytes: int | None = None
    sources: list[StorageMountSource]


class StorageInstanceUsage(BaseModel):
    service_type: str
    service_config_id: int
    name: str
    mount_count: int
    total_bytes: int
    free_bytes: int
    used_bytes: int


class StorageLibraryTotals(BaseModel):
    media_type: str
    item_count: int
    total_bytes: int


class StorageReclaimable(BaseModel):
    media_type: str
    candidate_count: int
    total_bytes: int


class StorageReclaimed(BaseModel):
    media_type: str
    action: str
    item_count: int
    total_bytes: int


class StorageResponse(BaseModel):
    mounts: list[StorageMount]
    capacity_total_bytes: int
    capacity_free_bytes: int
    capacity_used_bytes: int
    # true when at least one mount reported free space but no total, so the
    # capacity figures above are a floor rather than the whole picture
    capacity_incomplete: bool
    instances: list[StorageInstanceUsage]
    libraries: list[StorageLibraryTotals]
    library_total_bytes: int
    reclaimable: list[StorageReclaimable]
    reclaimable_total_bytes: int
    reclaimed: list[StorageReclaimed]
    reclaimed_total_bytes: int
    # one line per Arr that could not be reached; the rest of the page is still
    # accurate for whatever did answer
    errors: list[str]
