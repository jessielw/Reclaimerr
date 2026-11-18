"""Owner-bound snapshots of already-completed checks; never executes a check."""

import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from backend.models import diagnostics as m


class CheckAttachmentError(ValueError):
    pass


class CheckCache:
    def __init__(self, *, max_entries=32, max_bytes=1024 * 1024, ttl=600):
        self.max_entries, self.max_bytes, self.ttl = max_entries, max_bytes, ttl
        self.entries: OrderedDict[str, tuple[int, Path, float, bytes]] = OrderedDict()
        self.lock = threading.Lock()

    def _expire(self):
        for key in list(self.entries):
            if self.entries[key][2] <= time.monotonic():
                del self.entries[key]

    def put(
        self,
        owner: int,
        path: Path,
        options: m.DiagnosticCheckRequest,
        result: m.DiagnosticCheckResult,
    ) -> m.DiagnosticCheckResult:
        # Metadata is tied to the server-side request, never to bundle input.
        snapshot = result.model_copy(
            update={
                "target": options.target if options.kind != "provider" else None,
                "service_config_id": options.service_config_id
                if options.kind == "provider"
                else None,
                "file_paths_included": options.include_file_paths,
                "attachment_expires_at": datetime.now(UTC)
                + timedelta(seconds=self.ttl),
            }
        )
        data = snapshot.model_dump_json().encode()
        if len(data) > min(65536, self.max_bytes):
            return snapshot.model_copy(update={"attachment_expires_at": None})
        with self.lock:
            self._expire()
            self.entries.pop(snapshot.check_id, None)
            while self.entries and (
                len(self.entries) >= self.max_entries
                or sum(len(e[3]) for e in self.entries.values()) + len(data)
                > self.max_bytes
            ):
                self.entries.popitem(last=False)
            self.entries[snapshot.check_id] = (
                owner,
                path.resolve(),
                time.monotonic() + self.ttl,
                data,
            )
        return snapshot

    def selected(
        self,
        owner: int,
        path: Path,
        ids: list[UUID],
        target: m.Target | None,
        include_paths: bool,
    ) -> list[m.DiagnosticCheckResult]:
        if len(ids) > 5:
            raise CheckAttachmentError("Select at most five completed checks.")
        result = []
        total = 0
        with self.lock:
            self._expire()
            for identifier in dict.fromkeys(map(str, ids)):
                cached = self.entries.get(identifier)
                if cached is None or cached[0] != owner or cached[1] != path.resolve():
                    raise CheckAttachmentError(
                        "Selected checks expired or are unavailable. Run the checks again and select their new results."
                    )
                check = m.DiagnosticCheckResult.model_validate_json(cached[3])
                if check.file_paths_included and not include_paths:
                    raise CheckAttachmentError(
                        "Selected checks include file paths. Enable path sharing or rerun those checks with paths masked."
                    )
                if target and check.kind != "provider" and check.target != target:
                    raise CheckAttachmentError(
                        "Selected checks belong to a different media target. Select checks for this target or prepare a system bundle."
                    )
                total += len(cached[3])
                if total > 256 * 1024:
                    raise CheckAttachmentError(
                        "Selected checks exceed the attachment budget. Select fewer checks."
                    )
                result.append(check)
        return result


check_cache = CheckCache()
