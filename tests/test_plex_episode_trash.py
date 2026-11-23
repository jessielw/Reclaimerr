import asyncio
from pathlib import PurePosixPath

import pytest

from backend.services.plex import PlexService


@pytest.mark.parametrize(
    ("section_deleted_ids", "detail_deleted_ids", "keeps_original"),
    [
        pytest.param({101}, set(), False, id="section-marker"),
        pytest.param(set(), {101}, False, id="detail-marker"),
        pytest.param({101}, None, False, id="section-media-fallback"),
        pytest.param({101}, {102}, True, id="all-copies-trashed"),
        pytest.param(set(), set(), True, id="both-copies-available"),
    ],
)
def test_plex_series_sync_filters_trashed_episode_media(
    monkeypatch, section_deleted_ids, detail_deleted_ids, keeps_original
):
    original_path = "/tv/Original/Season 1/episode.mkv"
    upgrade_path = "/tv/Upgrade/Season 1/episode.mkv"
    original = {
        "id": 101,
        "duration": 1800000,
        "videoResolution": "4k",
        "videoCodec": "hevc",
        "Part": [
            {
                "file": original_path,
                "size": 1000,
                "Stream": [{"streamType": 1, "DOVIPresent": True}],
            }
        ],
    }
    upgrade = {
        "id": 102,
        "duration": 2100000,
        "videoResolution": "1080",
        "videoCodec": "h264",
        "Part": [{"file": upgrade_path, "size": 2000}],
    }
    episode = {
        "ratingKey": "3",
        "parentRatingKey": "2",
        "grandparentRatingKey": "1",
        "parentIndex": 1,
        "index": 1,
    }

    def media_with_trash_markers(deleted_ids):
        return [
            dict(media, deletedAt="1795228800") if media["id"] in deleted_ids else media
            for media in (original, upgrade)
        ]

    section_episode = dict(episode, Media=media_with_trash_markers(section_deleted_ids))
    detail_episode = dict(episode)
    if detail_deleted_ids is not None:
        detail_episode["Media"] = media_with_trash_markers(detail_deleted_ids)
    show = {"type": "show", "ratingKey": "1", "Guid": [{"id": "tmdb://123"}]}
    section = {"type": "show", "key": "1", "uuid": "library"}

    async def make_request(self, path, *, params=None, **kwargs):
        if path == "library/sections/1/all":
            items_by_type = {2: [show], 4: [section_episode], 18: []}
            return {"MediaContainer": {"Metadata": items_by_type[params["type"]]}}, 200
        containers = {
            "library/sections": {"Directory": [section]},
            "library/metadata/3": {"Metadata": [detail_episode]},
            "status/sessions/history/all": {"Metadata": []},
        }
        return {"MediaContainer": containers[path]}, 200

    monkeypatch.setattr(PlexService, "_make_request", make_request)
    plex = PlexService("token", "http://plex.local")
    [series] = asyncio.run(plex.get_aggregated_series())
    [season] = series.season_data
    [synced_episode] = season.episode_data

    expected_path = original_path if keeps_original else upgrade_path
    assert series.path == str(PurePosixPath(expected_path).parent.parent)
    assert series.size == season.size == (3000 if keeps_original else 2000)
    assert season.path == str(PurePosixPath(expected_path).parent)
    assert season.episode_paths == [expected_path]
    assert season.max_video_width == (3840 if keeps_original else 1920)
    assert season.video_codec_families == (
        ["h264", "h265"] if keeps_original else ["h264"]
    )
    assert season.has_hdr is (True if keeps_original else None)
    assert season.has_dolby_vision is (True if keeps_original else None)
    assert synced_episode.path == expected_path
    assert synced_episode.size == (1000 if keeps_original else 2000)
    assert synced_episode.runtime_seconds == (1800 if keeps_original else 2100)
    assert [version.service_media_id for version in synced_episode.versions] == (
        ["101", "102"] if keeps_original else ["102"]
    )
