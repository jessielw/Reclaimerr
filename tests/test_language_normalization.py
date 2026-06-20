from __future__ import annotations

from backend.core.rule_engine import _matches_operator
from backend.core.utils.language import normalize_language, normalize_languages
from backend.services.emby_base import EmbyServiceBase
from backend.services.plex import PlexService


def test_normalize_language_equates_iso_aliases_names_and_case() -> None:
    assert normalize_language("en") == "eng"
    assert normalize_language("ENG") == "eng"
    assert normalize_language("English") == "eng"
    assert normalize_language("fr") == "fra"
    assert normalize_language("fre") == "fra"
    assert normalize_language("FRA") == "fra"


def test_normalize_language_strips_regional_tag() -> None:
    assert normalize_language("en-US") == "eng"
    assert normalize_language("pt_BR") == "por"


def test_normalize_languages_deduplicates_aliases_and_drops_unknowns() -> None:
    assert normalize_languages(["en", "ENG", "unknown", "und", "fr"]) == [
        "eng",
        "fra",
    ]
    assert normalize_languages(["unknown", "und", "mul", "mis"]) is None


def test_language_list_rules_are_case_insensitive_and_alias_aware() -> None:
    assert _matches_operator(
        ["ENG"],
        "contains_any",
        ["en"],
        field="audio.languages",
    )
    assert not _matches_operator(
        ["English"],
        "not_contains_any",
        ["EN"],
        field="audio.languages",
    )


def test_unknown_language_does_not_match_negative_list_operator() -> None:
    for actual in (None, [], ["unknown"], ["und"], ["mul"], ["mis"]):
        assert not _matches_operator(
            actual,
            "not_contains_any",
            ["en"],
            field="audio.languages",
        )
        assert not _matches_operator(
            actual,
            "not_contains_all",
            ["en", "fr"],
            field="subtitle.languages",
        )


def test_unknown_language_matches_explicit_missing_operator() -> None:
    for actual in (None, [], ["unknown"], ["und"], ["mul"], ["mis"]):
        assert _matches_operator(
            actual,
            "not_exists",
            None,
            field="audio.languages",
        )
        assert not _matches_operator(
            actual,
            "exists",
            None,
            field="audio.languages",
        )


def test_unrelated_negative_list_rule_still_matches_empty_values() -> None:
    assert _matches_operator(
        [],
        "not_contains_any",
        ["garbage"],
        field="arr.tags",
    )


def test_media_server_language_ingestion_uses_canonical_codes() -> None:
    plex_streams = [
        {"languageCode": "en"},
        {"languageTag": "FRE"},
        {"language": "unknown"},
    ]
    emby_streams = [
        {"Language": "EN"},
        {"Language": "fr-FR"},
        {"Language": "und"},
    ]

    assert PlexService._unique_languages(plex_streams) == ["eng", "fra"]
    assert EmbyServiceBase._unique_languages(emby_streams) == ["eng", "fra"]


def test_plex_requests_details_when_lightweight_audio_language_is_missing() -> None:
    missing_language = {
        "Part": [
            {
                "Stream": [
                    {"streamType": 1, "codec": "h264"},
                    {
                        "streamType": 2,
                        "codec": "ac3",
                        "displayTitle": "Unknown (AC3 5.1)",
                    },
                ]
            }
        ]
    }
    known_language = {
        "Part": [
            {
                "Stream": [
                    {"streamType": 1, "codec": "h264"},
                    {"streamType": 2, "codec": "ac3", "languageCode": "eng"},
                ]
            }
        ]
    }

    assert PlexService._needs_detailed_stream_metadata(missing_language)
    assert not PlexService._needs_detailed_stream_metadata(known_language)
