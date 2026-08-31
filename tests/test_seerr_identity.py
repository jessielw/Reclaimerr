"""A bare Seerr user id must never be mistaken for a requester identity.

Once more than one Seerr is configured a bare id names two different people, so
parsing has to refuse it rather than pick one. These tests pin that refusal, and
pin the bare-id accessor that identity matching still needs -- playback providers
record the Seerr-native id, never the qualified one.
"""

from __future__ import annotations

from backend.core.seerr_identity import (
    QualifiedSeerrUserId,
    is_qualified_seerr_user_id,
    normalize_qualified_seerr_user_id,
    parse_qualified_seerr_user_id,
    parse_qualified_seerr_user_ids,
    qualify_seerr_user_id,
    seerr_config_id_of,
    seerr_user_id_of,
)


def test_qualify_and_parse_round_trip() -> None:
    assert qualify_seerr_user_id(7, 3) == "7:3"
    assert parse_qualified_seerr_user_id("7:3") == QualifiedSeerrUserId(7, 3)
    assert str(QualifiedSeerrUserId(7, 3)) == "7:3"


def test_bare_user_id_is_not_an_identity() -> None:
    """The whole point: "3" names user 3 on every Seerr, so it names nobody."""
    assert parse_qualified_seerr_user_id("3") is None
    assert parse_qualified_seerr_user_id(3) is None
    assert seerr_user_id_of("3") is None
    assert seerr_config_id_of("3") is None
    assert is_qualified_seerr_user_id("3") is False


def test_malformed_values_are_refused() -> None:
    for value in ("", None, "  ", ":", "7:", ":3", "7:3:9", "-7:3", "7:-3", "a:3", "7:b"):
        assert parse_qualified_seerr_user_id(value) is None, value


def test_surrounding_whitespace_is_tolerated() -> None:
    assert parse_qualified_seerr_user_id(" 7:3 ") == QualifiedSeerrUserId(7, 3)
    assert normalize_qualified_seerr_user_id(" 7:3 ") == "7:3"


def test_accessors_split_the_two_halves() -> None:
    assert seerr_config_id_of("7:3") == 7
    assert seerr_user_id_of("7:3") == 3
    # Same user number on two instances stays two distinct people.
    assert seerr_config_id_of("9:3") == 9
    assert qualify_seerr_user_id(7, 3) != qualify_seerr_user_id(9, 3)


def test_parse_many_drops_unparseable_entries() -> None:
    parsed = parse_qualified_seerr_user_ids(["7:3", "garbage", "9:1", "3", None])
    assert [tuple(item) for item in parsed] == [(7, 3), (9, 1)]


def test_already_parsed_values_pass_through() -> None:
    qualified = QualifiedSeerrUserId(7, 3)
    assert parse_qualified_seerr_user_id(qualified) is qualified
