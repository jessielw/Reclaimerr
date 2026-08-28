"""Instance-qualified Seerr requester identities.

A Seerr user id is only unique inside the Seerr that issued it. Once more than
one Seerr is configured, user 3 on an Overseerr and user 3 on a Jellyseerr are
different people, so a bare id is not an identity -- it is half of one. The same
rule already applies to playback providers, where "Plex numbering its owner 1 and
Tautulli numbering its first user 1 are unrelated facts".

Everything that names a requester -- a rule condition value, a requester watch
mapping, an API response, the snapshot cache -- carries the qualified form
``"<service_config_id>:<user_id>"`` instead. Parsing is strict: a bare ``"3"``
returns ``None`` rather than a guess, because the migration that qualified the
saved values is what makes bare values impossible, and quietly accepting one
would hide a migration that did not run.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

QUALIFIED_SEPARATOR = ":"


class QualifiedSeerrUserId(NamedTuple):
    """One Seerr user, and which Seerr issued the id."""

    service_config_id: int
    user_id: int

    def __str__(self) -> str:
        return f"{self.service_config_id}{QUALIFIED_SEPARATOR}{self.user_id}"


def qualify_seerr_user_id(service_config_id: int, user_id: int) -> str:
    """Return the qualified text form for one requester."""
    return f"{int(service_config_id)}{QUALIFIED_SEPARATOR}{int(user_id)}"


def parse_qualified_seerr_user_id(value: object) -> QualifiedSeerrUserId | None:
    """Parse a qualified requester id, or return None if it is not one.

    Deliberately strict -- a bare user id, a negative part, or anything with a
    stray separator is not a requester identity and must not be treated as one.
    """
    if isinstance(value, QualifiedSeerrUserId):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    config_part, separator, user_part = text.partition(QUALIFIED_SEPARATOR)
    if not separator:
        return None
    if not config_part.isdigit() or not user_part.isdigit():
        return None
    return QualifiedSeerrUserId(int(config_part), int(user_part))


def parse_qualified_seerr_user_ids(
    values: Iterable[object],
) -> list[QualifiedSeerrUserId]:
    """Parse every qualified id in ``values``, dropping the ones that are not."""
    parsed: list[QualifiedSeerrUserId] = []
    for value in values:
        qualified = parse_qualified_seerr_user_id(value)
        if qualified is not None:
            parsed.append(qualified)
    return parsed


def seerr_config_id_of(value: object) -> int | None:
    """Return which Seerr issued this requester id, if it is qualified."""
    qualified = parse_qualified_seerr_user_id(value)
    return qualified.service_config_id if qualified else None


def seerr_user_id_of(value: object) -> int | None:
    """Return the Seerr-native user id, if this is a qualified requester id.

    This is what identity matching falls back to when Seerr reports no username
    or display name for a requester: providers record the bare id, never the
    qualified one.
    """
    qualified = parse_qualified_seerr_user_id(value)
    return qualified.user_id if qualified else None


def normalize_qualified_seerr_user_id(value: object) -> str | None:
    """Return the canonical text form, or None if the value is not qualified."""
    qualified = parse_qualified_seerr_user_id(value)
    return str(qualified) if qualified else None


def is_qualified_seerr_user_id(value: object) -> bool:
    """Return whether ``value`` names a requester on a specific Seerr."""
    return parse_qualified_seerr_user_id(value) is not None
