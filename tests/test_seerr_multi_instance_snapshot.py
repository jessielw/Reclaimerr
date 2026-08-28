"""Merging two Seerrs must not blur them together, or hide one going dark.

Requester ids are instance-qualified, so the merge is lossless. The half that
carries real risk is the failure path: answering a rule from the instances that
happened to respond reports "nobody requested this" for titles a silent Seerr
still holds active requests for, and that reads as a delete.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.core.service_manager import service_manager
from backend.enums import MediaType, SeerrRequestStatus
from backend.models.services.seerr import SeerrRequest, SeerrUser
from backend.services.seerr_cache import SeerrSnapshotCache

MOVIE_KEY = (MediaType.MOVIE, 10)


def _request(
    *,
    request_id: int,
    requested_by_id: int,
    created_at: datetime,
    status: SeerrRequestStatus = SeerrRequestStatus.APPROVED,
    tmdb_id: int = 10,
) -> SeerrRequest:
    return SeerrRequest(
        id=request_id,
        status=status,
        media_id=request_id,
        media_type=MediaType.MOVIE,
        tmdb_id=tmdb_id,
        created_at=created_at,
        requested_by_id=requested_by_id,
        is_4k=False,
    )


def _client(*, requests: list[SeerrRequest], users: list[SeerrUser] | None = None):
    return SimpleNamespace(
        get_all_requests=AsyncMock(return_value=requests),
        get_all_users=AsyncMock(return_value=users or []),
    )


def _failing_client():
    return SimpleNamespace(
        get_all_requests=AsyncMock(side_effect=RuntimeError("connection refused")),
        get_all_users=AsyncMock(return_value=[]),
    )


def test_same_user_number_on_two_seerrs_stays_two_requesters() -> None:
    async def run() -> None:
        early = datetime(2026, 1, 1, tzinfo=UTC)
        late = datetime(2026, 3, 1, tzinfo=UTC)
        clients = {
            7: _client(
                requests=[_request(request_id=1, requested_by_id=3, created_at=early)],
                users=[SeerrUser(id=3, username="alice", display_name="Alice")],
            ),
            9: _client(
                requests=[_request(request_id=2, requested_by_id=3, created_at=late)],
                users=[SeerrUser(id=3, username="bob", display_name="Bob")],
            ),
        }
        cache = SeerrSnapshotCache()
        with patch.object(service_manager, "_seerr_clients", clients):
            snapshot, error = await cache.get_request_snapshot(
                require_fresh=True, allow_stale_on_failure=False
            )

        assert error is None
        assert snapshot is not None
        assert snapshot.requester_ids_by_key[MOVIE_KEY] == {"7:3", "9:3"}
        assert snapshot.requester_users_by_id["7:3"].display_name == "Alice"
        assert snapshot.requester_users_by_id["9:3"].display_name == "Bob"
        # Each requester is gated on their own instance's request date.
        assert snapshot.first_request_at_by_key_user[MOVIE_KEY] == {
            "7:3": early,
            "9:3": late,
        }

    asyncio.run(run())


def test_latest_active_request_takes_the_newest_across_instances() -> None:
    async def run() -> None:
        older = datetime(2026, 1, 1, tzinfo=UTC)
        newer = datetime(2026, 5, 1, tzinfo=UTC)
        clients = {
            7: _client(
                requests=[
                    _request(
                        request_id=1,
                        requested_by_id=3,
                        created_at=older,
                        status=SeerrRequestStatus.PENDING,
                    )
                ]
            ),
            9: _client(
                requests=[
                    _request(
                        request_id=2,
                        requested_by_id=4,
                        created_at=newer,
                        status=SeerrRequestStatus.PENDING,
                    )
                ]
            ),
        }
        cache = SeerrSnapshotCache()
        with patch.object(service_manager, "_seerr_clients", clients):
            snapshot, _error = await cache.get_request_snapshot(
                require_fresh=True, allow_stale_on_failure=False
            )

        assert snapshot is not None
        assert snapshot.latest_active_request_at_by_key[MOVIE_KEY] == newer

    asyncio.run(run())


def test_one_unreachable_instance_withholds_the_whole_answer() -> None:
    """`allow_stale_on_failure=False` is the scan path, and must fail closed."""

    async def run() -> None:
        clients = {
            7: _client(
                requests=[
                    _request(
                        request_id=1,
                        requested_by_id=3,
                        created_at=datetime(2026, 1, 1, tzinfo=UTC),
                    )
                ]
            ),
            9: _failing_client(),
        }
        cache = SeerrSnapshotCache()
        with patch.object(service_manager, "_seerr_clients", clients):
            state = await cache.get_request_snapshot_state(
                require_fresh=True, allow_stale_on_failure=False
            )

        assert state.merged is None
        assert state.unavailable_config_ids == {9}
        assert state.healthy_config_ids == {7}
        assert state.error_summary is not None
        assert "connection refused" in state.error_summary

    asyncio.run(run())


def test_display_paths_still_get_the_instances_that_answered() -> None:
    """A missing instance costs a badge, not a deletion, so display allows it."""

    async def run() -> None:
        clients = {
            7: _client(
                requests=[
                    _request(
                        request_id=1,
                        requested_by_id=3,
                        created_at=datetime(2026, 1, 1, tzinfo=UTC),
                    )
                ]
            ),
            9: _failing_client(),
        }
        cache = SeerrSnapshotCache()
        with patch.object(service_manager, "_seerr_clients", clients):
            state = await cache.get_request_snapshot_state(
                require_fresh=True, allow_stale_on_failure=True
            )

        assert state.merged is not None
        assert state.merged.requester_ids_by_key[MOVIE_KEY] == {"7:3"}
        assert state.unavailable_config_ids == {9}

    asyncio.run(run())


def test_users_are_returned_per_instance_with_qualified_ids() -> None:
    async def run() -> None:
        clients = {
            7: _client(
                requests=[],
                users=[SeerrUser(id=3, username="alice", display_name="Alice")],
            ),
            9: _client(
                requests=[],
                users=[SeerrUser(id=3, username="bob", display_name="Bob")],
            ),
        }
        cache = SeerrSnapshotCache()
        with patch.object(service_manager, "_seerr_clients", clients):
            users = await cache.get_users()

        assert [entry.qualified_id for entry in users] == ["7:3", "9:3"]
        assert {entry.user.display_name for entry in users} == {"Alice", "Bob"}

    asyncio.run(run())


def test_no_configured_instance_reports_that_rather_than_an_empty_answer() -> None:
    async def run() -> None:
        cache = SeerrSnapshotCache()
        with patch.object(service_manager, "_seerr_clients", {}):
            snapshot, error = await cache.get_request_snapshot(
                require_fresh=True, allow_stale_on_failure=False
            )

        assert snapshot is None
        assert error == "Seerr service is not configured"

    asyncio.run(run())


def test_removing_an_instance_drops_its_cached_state() -> None:
    async def run() -> None:
        created_at = datetime(2026, 1, 1, tzinfo=UTC)
        both = {
            7: _client(
                requests=[
                    _request(request_id=1, requested_by_id=3, created_at=created_at)
                ]
            ),
            9: _client(
                requests=[
                    _request(request_id=2, requested_by_id=4, created_at=created_at)
                ]
            ),
        }
        cache = SeerrSnapshotCache()
        with patch.object(service_manager, "_seerr_clients", both):
            await cache.get_request_snapshot(
                require_fresh=True, allow_stale_on_failure=False
            )

        with patch.object(service_manager, "_seerr_clients", {7: both[7]}):
            state = await cache.get_request_snapshot_state(
                require_fresh=False, allow_stale_on_failure=True
            )

        assert state.configured_config_ids == {7}
        assert state.merged is not None
        assert state.merged.requester_ids_by_key[MOVIE_KEY] == {"7:3"}

    asyncio.run(run())
