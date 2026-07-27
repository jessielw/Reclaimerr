from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend.enums import Service
from backend.models.media import AggregatedSeasonData, AggregatedSeriesData, ExternalIDs
from backend.tasks.sync import _dedupe_aggregated_series, _tvdb_sorts_first


def _season_data(service_series_id: str) -> list[AggregatedSeasonData]:
    """A minimal, distinguishable season payload for stash-identity assertions."""
    return [
        AggregatedSeasonData(
            service_series_id=service_series_id,
            season_number=1,
            size=1,
            episode_count=1,
            view_count=0,
            last_viewed_at=None,
        )
    ]


def _series_data(
    name: str,
    tmdb: int,
    tvdb: str | None,
    service: Service = Service.JELLYFIN,
    last_viewed_at: datetime | None = None,
    season_data: list[AggregatedSeasonData] | None = None,
) -> AggregatedSeriesData:
    return AggregatedSeriesData(
        id=f"item-{name}",
        name=name,
        year=2020,
        service=service,
        library_name="Shows",
        library_id="1",
        path=None,
        added_at=None,
        external_ids=ExternalIDs(tmdb=tmdb, imdb=None, tmdb_collection=None, tvdb=tvdb),
        size=1,
        view_count=0,
        last_viewed_at=last_viewed_at,
        season_data=season_data if season_data is not None else [],
    )


def test_numeric_tvdb_ids_sort_numerically_not_lexicographically() -> None:
    assert _tvdb_sorts_first("9001", "10001") is True
    assert _tvdb_sorts_first("10001", "9001") is False


def test_non_numeric_tvdb_ids_fall_back_to_string_order() -> None:
    assert _tvdb_sorts_first("abc", "abd") is True


def test_identical_tvdb_ids_do_not_sort_first_either_way() -> None:
    assert _tvdb_sorts_first("9001", "9001") is False


def test_two_series_sharing_a_tmdb_id_are_not_merged() -> None:
    """Distinct tvdb ids prove these are different series, not one seen twice.

    Merging them stashes the loser's season data as supplemental, which then
    writes the loser's episode ids onto the winner's episode rows.
    """
    a = _series_data("Series A", tmdb=1001, tvdb="9001")
    b = _series_data("Series B", tmdb=1001, tvdb="9002")

    unique, supplemental = _dedupe_aggregated_series([a, b])

    assert list(unique) == [1001]
    assert supplemental == {}


def test_collision_winner_is_the_lower_tvdb_id() -> None:
    a = _series_data("Series A", tmdb=1001, tvdb="9002")
    b = _series_data("Series B", tmdb=1001, tvdb="9001")

    unique, _ = _dedupe_aggregated_series([a, b])

    assert unique[1001].name == "Series B"


def test_collision_winner_ignores_watch_dates() -> None:
    """Ownership must not flip when someone watches the other series."""
    recent = datetime.now(UTC)
    a = _series_data("Series A", tmdb=1001, tvdb="9001", last_viewed_at=recent)
    b = _series_data(
        "Series B",
        tmdb=1001,
        tvdb="9002",
        last_viewed_at=recent + timedelta(days=1),
    )

    unique, _ = _dedupe_aggregated_series([a, b])

    assert unique[1001].name == "Series A"


def test_collision_winner_is_order_independent() -> None:
    a = _series_data("Series A", tmdb=1001, tvdb="9001")
    b = _series_data("Series B", tmdb=1001, tvdb="9002")

    forward, _ = _dedupe_aggregated_series([a, b])
    reverse, _ = _dedupe_aggregated_series([b, a])

    assert forward[1001].name == reverse[1001].name == "Series A"


def test_same_series_on_two_services_still_merges() -> None:
    """The cross-service merge this dedup was written for must be preserved."""
    older = datetime.now(UTC)
    jellyfin = _series_data(
        "Series A",
        tmdb=1001,
        tvdb="9001",
        service=Service.JELLYFIN,
        last_viewed_at=older,
        season_data=_season_data("jellyfin-series"),
    )
    plex = _series_data(
        "Series A",
        tmdb=1001,
        tvdb="9001",
        service=Service.PLEX,
        last_viewed_at=older + timedelta(days=1),
        season_data=_season_data("plex-series"),
    )

    unique, supplemental = _dedupe_aggregated_series([jellyfin, plex])

    assert unique[1001].service is Service.PLEX
    # exact contents, not just presence - a transposed stash (winner's data
    # instead of the loser's) must fail this, not slip through as "some entry"
    assert supplemental[1001] == [(Service.JELLYFIN, jellyfin.season_data)]


def test_collision_drops_stale_supplemental_from_the_incumbent_it_displaces() -> None:
    """A collision winner must not inherit supplemental stashed under the loser.

    Two servers report the same Series A, which merges normally first and
    stashes the losing server's season data under tmdb 1001. Series B then
    collides on that same tmdb id and wins. The Series A stash from that
    unrelated merge must not survive, or Series A's episode ids graft onto
    Series B's episode rows.
    """
    a_server1 = _series_data(
        "Series A",
        tmdb=1001,
        tvdb="9002",
        service=Service.JELLYFIN,
        season_data=_season_data("a-jellyfin"),
    )
    a_server2 = _series_data(
        "Series A",
        tmdb=1001,
        tvdb="9002",
        service=Service.PLEX,
        season_data=_season_data("a-plex"),
    )
    b = _series_data("Series B", tmdb=1001, tvdb="9001")

    unique, supplemental = _dedupe_aggregated_series([a_server1, a_server2, b])

    assert unique[1001].name == "Series B"
    assert supplemental == {}


def test_missing_tvdb_on_one_side_still_merges() -> None:
    """Only two present-and-different tvdb ids prove a genuine collision."""
    a = _series_data("Series A", tmdb=1001, tvdb="9001")
    b = _series_data("Series B", tmdb=1001, tvdb=None)

    unique, supplemental = _dedupe_aggregated_series([a, b])

    assert 1001 in supplemental
    assert list(unique) == [1001]


def test_series_without_a_tmdb_id_is_skipped() -> None:
    unique, _ = _dedupe_aggregated_series(
        [_series_data("Series A", tmdb=0, tvdb="9001")]
    )

    assert unique == {}
