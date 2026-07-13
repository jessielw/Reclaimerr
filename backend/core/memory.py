from __future__ import annotations

import ctypes
import gc
import os
from dataclasses import dataclass

from backend.core.logger import LOG

_KIB_PER_MIB = 1024


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    rss_mib: float | None

    def label(self) -> str:
        if self.rss_mib is None:
            return "rss=unknown"
        return f"rss={self.rss_mib:.1f} MiB"


def current_memory_snapshot() -> MemorySnapshot:
    """Return a lightweight process memory snapshot."""
    return MemorySnapshot(rss_mib=_linux_rss_mib())


def log_memory_snapshot(label: str) -> MemorySnapshot:
    snapshot = current_memory_snapshot()
    LOG.info(f"Memory {label}: {snapshot.label()}")
    return snapshot


def cleanup_process_memory(*, context: str) -> MemorySnapshot:
    """Collect Python garbage and ask glibc to return free arenas where possible."""
    gc.collect()
    _malloc_trim()
    return log_memory_snapshot(f"after cleanup ({context})")


def _linux_rss_mib() -> float | None:
    if os.name != "posix":
        return None
    try:
        with open("/proc/self/status", encoding="utf-8") as status_file:
            for line in status_file:
                if not line.startswith("VmRSS:"):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    return None
                return int(parts[1]) / _KIB_PER_MIB
    except OSError:
        return None
    return None


def _malloc_trim() -> None:
    if os.name != "posix":
        return
    try:
        libc = ctypes.CDLL("libc.so.6")
        trim = getattr(libc, "malloc_trim", None)
        if trim is None:
            return
        trim(0)
    except Exception:
        return
