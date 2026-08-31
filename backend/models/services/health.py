from __future__ import annotations

from dataclasses import dataclass

from backend.core.utils.request import summarize_error_message


@dataclass(slots=True, frozen=True)
class HealthResult:
    """Outcome of a service health check, carrying why it failed.

    Health checks used to collapse every failure into ``False``, which left the
    logs able to say only which URL failed - never whether it was DNS, TLS, a
    401 from the service, or a 403 from a reverse proxy in front of it. Callers
    still branch on truthiness, so ``if not await client.health():`` reads the
    same, but the reason survives for the message they log.
    """

    ok: bool
    detail: str | None = None

    def __bool__(self) -> bool:
        return self.ok

    @classmethod
    def healthy(cls) -> HealthResult:
        return cls(True)

    @classmethod
    def failed(cls, detail: object = None) -> HealthResult:
        return cls(False, summarize_error_message(str(detail)) if detail else None)
