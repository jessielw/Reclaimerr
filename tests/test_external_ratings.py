from backend.services.external_ratings import (
    ProviderRateLimitSnapshot,
    _rate_limit_from_headers,
    parse_mdblist_ratings,
    parse_omdb_ratings,
)


def test_parse_omdb_ratings_extracts_rotten_tomatoes_and_metascore() -> None:
    ratings = parse_omdb_ratings(
        {
            "Metascore": "71",
            "Ratings": [
                {"Source": "Internet Movie Database", "Value": "7.7/10"},
                {"Source": "Rotten Tomatoes", "Value": "86%"},
                {"Source": "Metacritic", "Value": "71/100"},
            ],
        }
    )

    assert ratings.rottentomatoes_tomato_meter == 86
    assert ratings.rottentomatoes_popcorn_meter is None
    assert ratings.metacritic_metascore == 71


def test_parse_mdblist_ratings_handles_nested_provider_shapes() -> None:
    ratings = parse_mdblist_ratings(
        {
            "ratings": [
                {"source": "tomatoes", "score": 91, "votes": 147},
                {"source": "popcorn", "score": 82, "votes": 53440},
                {"source": "metacritic", "score": 67, "votes": 22},
                {"source": "metacriticuser", "score": 93, "votes": 2188},
                {"source": "trakt", "score": 90, "votes": 36405},
                {"source": "letterboxd", "score": 92, "votes": 2859264},
            ]
        }
    )

    assert ratings.rottentomatoes_tomato_meter == 91
    assert ratings.rottentomatoes_tomato_vote_count == 147
    assert ratings.rottentomatoes_popcorn_meter == 82
    assert ratings.rottentomatoes_popcorn_vote_count == 53440
    assert ratings.metacritic_metascore == 67
    assert ratings.metacritic_vote_count == 22
    assert ratings.metacritic_user_score == 93
    assert ratings.metacritic_user_vote_count == 2188
    assert ratings.trakt_rating == 90
    assert ratings.trakt_vote_count == 36405
    assert ratings.letterboxd_score == 92
    assert ratings.letterboxd_vote_count == 2859264


def test_parse_mdblist_ratings_uses_source_specific_value_scale_fallbacks() -> None:
    ratings = parse_mdblist_ratings(
        {
            "ratings": [
                {"source": "letterboxd", "value": 4.6, "votes": "1,200"},
                {"source": "metacriticuser", "value": 8.4, "votes": "50"},
            ]
        }
    )

    assert ratings.letterboxd_score == 92
    assert ratings.letterboxd_vote_count == 1200
    assert ratings.metacritic_user_score == 84
    assert ratings.metacritic_user_vote_count == 50


def test_rate_limit_headers_are_normalized() -> None:
    snapshot = _rate_limit_from_headers(
        {
            "X-RateLimit-Limit": "1000",
            "X-RateLimit-Remaining": "999",
            "X-RateLimit-Reset": "1782345600",
        }
    )

    assert snapshot == ProviderRateLimitSnapshot(
        limit=1000,
        remaining=999,
        reset_epoch=1782345600,
    )
    assert snapshot is not None
    assert snapshot.to_dict() == {
        "limit": 1000,
        "remaining": 999,
        "reset_epoch": 1782345600,
    }
