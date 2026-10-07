from __future__ import annotations

from io import BytesIO
import json
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from support.douyin import config
from trippostcollect.platforms.douyin import core as douyin_core
from support.douyin import make_crawler as DouYinCrawler
from trippostcollect.platforms.douyin.models import DouyinImageDownloadError
from trippostcollect.runtime.image_retry import ImageDownloadFetchError


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (9, 6), color="orange").save(output, format="PNG")
    return output.getvalue()


def image_aweme(aweme_id: str = "dy-note") -> dict:
    return {
        "aweme_id": aweme_id,
        "aweme_type": 68,
        "desc": "test image note",
        "create_time": 1_700_000_000,
        "author": {"uid": "author", "nickname": "name", "follower_count": 5},
        "statistics": {
            "digg_count": 1,
            "collect_count": 2,
            "comment_count": 3,
            "share_count": 4,
        },
        "images": [
            {
                "uri": "uri-body-one",
                "url_list": [
                    "https://p3.test/body-one.jpeg?signature=old",
                    "https://p9.test/body-one.jpeg?signature=fresh",
                ],
            }
        ],
        "video": {
            "raw_cover": {"url_list": ["", "https://media.test/cover.jpg"]},
            "play_addr": {"url_list": ["", "https://media.test/video.mp4"]},
        },
        "music": {"play_url": {"uri": "https://media.test/music.mp3"}},
    }


@pytest.mark.asyncio
async def test_strict_image_entry_requests_only_note_image_and_writes_true_format(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr("trippostcollect.platforms.douyin.core.random.random", lambda: 0)
    crawler = DouYinCrawler()
    crawler.dy_client = AsyncMock()
    crawler.dy_client.get_aweme_media.return_value = png_bytes()
    crawler.get_aweme_video = AsyncMock(return_value=None)
    video_store = AsyncMock(return_value=None)
    monkeypatch.setattr(crawler, "update_dy_aweme_video", video_store, raising=False)
    aweme = image_aweme()

    await crawler.get_aweme_media(aweme)

    crawler.dy_client.get_aweme_media.assert_awaited_once_with(
        "https://p9.test/body-one.jpeg?signature=fresh"
    )
    crawler.get_aweme_video.assert_not_awaited()
    video_store.assert_not_awaited()
    stored = tmp_path / "douyin" / "images" / "dy-note" / "000.png"
    assert stored.read_bytes() == png_bytes()
    rows = [
        json.loads(line)
        for line in (tmp_path / "douyin" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["source_asset_key"] == "douyin:uri:uri-body-one"
    assert rows[0]["mime_type"] == "image/png"
    serialized = json.dumps(rows)
    assert "cover.jpg" not in serialized
    assert "video.mp4" not in serialized
    assert "music.mp3" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("aweme", [{}, {"video": {"play_addr": {"url_list": ["v"]}}}])
async def test_empty_or_video_candidate_reaches_no_image_or_video_bytes(monkeypatch, aweme):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    crawler = DouYinCrawler()
    crawler.dy_client = AsyncMock()
    crawler.get_aweme_video = AsyncMock(return_value=None)

    await crawler.get_aweme_media(aweme)

    crawler.dy_client.get_aweme_media.assert_not_awaited()
    crawler.get_aweme_video.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_note_image_failure_recovers_and_records_attempts(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr("trippostcollect.platforms.douyin.core.random.random", lambda: 0)
    monkeypatch.setattr(
        "trippostcollect.runtime.image_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS",
        (0.0, 0.0),
    )
    crawler = DouYinCrawler()
    crawler.dy_client = AsyncMock()
    crawler.dy_client.get_aweme_media.side_effect = [None, png_bytes()]

    await crawler.get_aweme_images(image_aweme("dy-recovered"))

    rows = [
        json.loads(line)
        for line in (tmp_path / "douyin" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [(row["fetch_status"], row["attempts"]) for row in rows] == [
        ("downloaded", 2)
    ]
    assert crawler.dy_client.get_aweme_media.await_count == 2


@pytest.mark.asyncio
async def test_failed_note_image_writes_failed_manifest_only(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr("trippostcollect.platforms.douyin.core.random.random", lambda: 0)
    monkeypatch.setattr(
        "trippostcollect.runtime.image_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS",
        (0.0, 0.0),
    )
    crawler = DouYinCrawler()
    crawler.dy_client = AsyncMock()
    crawler.dy_client.get_aweme_media.return_value = None

    with pytest.raises(DouyinImageDownloadError):
        await crawler.get_aweme_images(image_aweme("dy-failed"))

    rows = [
        json.loads(line)
        for line in (tmp_path / "douyin" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows[0]["fetch_status"] == "failed"
    assert rows[0]["attempts"] == 3
    assert rows[0]["staging_path"] is None
    assert crawler.dy_client.get_aweme_media.await_count == 3
    assert not (tmp_path / "douyin" / "images" / "dy-failed").exists()


@pytest.mark.asyncio
async def test_terminal_http_fetch_error_preserves_manifest_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr("trippostcollect.platforms.douyin.core.random.random", lambda: 0)
    crawler = DouYinCrawler()
    crawler.dy_client = AsyncMock()
    crawler.dy_client.get_aweme_media.side_effect = ImageDownloadFetchError(
        "HTTP 404",
        code="image_source_unavailable",
        retryable=False,
        http_status=404,
    )

    with pytest.raises(DouyinImageDownloadError):
        await crawler.get_aweme_images(image_aweme("missing-aweme"))

    row = json.loads(
        (tmp_path / "douyin" / "image_manifest.jsonl").read_text().splitlines()[0]
    )
    assert row["error_code"] == "image_source_unavailable"
    assert row["http_status"] == 404
    assert row["attempts"] == 1


class SearchClient:
    async def search_info_by_keyword(self, **kwargs):
        return {
            "status_code": 0,
            "data": [
                {"aweme_info": image_aweme("retry-aweme")},
                {"aweme_info": image_aweme("success-aweme")},
            ],
            "has_more": 0,
            "extra": {"logid": "fresh-search-id"},
        }


@pytest.mark.asyncio
async def test_creator_profile_failure_is_recorded_and_later_candidate_continues(
    monkeypatch, tmp_path
):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR", raising=False)
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 10)
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "KEYWORDS", "test")
    monkeypatch.setattr(config, "PUBLISH_TIME_TYPE", 0)
    crawler = DouYinCrawler()
    crawler.dy_client = SearchClient()

    async def enrich(aweme):
        if aweme["aweme_id"] == "retry-aweme":
            return {**aweme, "creator_profile_error": "detail_request_failed"}
        return aweme

    crawler.enrich_aweme_creator = enrich
    crawler.get_aweme_images = AsyncMock(return_value=None)
    store = AsyncMock(return_value=None)
    monkeypatch.setattr(crawler, "update_douyin_aweme", store)

    await crawler.search()

    store.assert_awaited_once()
    crawler.get_aweme_images.assert_awaited_once()
    assert crawler.get_aweme_images.await_args.kwargs["aweme_item"]["aweme_id"] == "success-aweme"
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert skipped[0]["details"]["identity"] == "retry-aweme"
    assert skipped[0]["details"]["failure_scope"] == "post"
    assert skipped[0]["details"]["attempts"] == 1
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["candidate_identities"] == [
        "retry-aweme",
        "success-aweme",
    ]


@pytest.mark.asyncio
async def test_empty_creator_profile_retries_then_becomes_candidate_failure(
    monkeypatch,
):
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_ENRICH_CREATORS", "1")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_ENRICH_ONLY_IMAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DOUYIN_MAX_CREATOR_ENRICH", "30")
    crawler = DouYinCrawler()
    crawler.dy_client = AsyncMock()
    crawler.dy_client.get_user_info.return_value = {}
    monkeypatch.setattr(douyin_core.asyncio, "sleep", AsyncMock())
    aweme = image_aweme("empty-creator")
    aweme["author"]["sec_uid"] = "sec-empty-creator"

    enriched = await crawler.enrich_aweme_creator(aweme)

    second_aweme = image_aweme("empty-creator-second")
    second_aweme["author"]["sec_uid"] = "sec-empty-creator"
    second_enriched = await crawler.enrich_aweme_creator(second_aweme)

    assert crawler.dy_client.get_user_info.await_count == 3
    assert enriched["creator_profile_attempts"] == 3
    assert enriched["creator_profile_error"] == "creator_profile_empty"
    assert "creator_profile" not in enriched
    assert second_enriched["creator_profile_attempts"] == 3
    assert second_enriched["creator_profile_error"] == "creator_profile_empty"
    assert "creator_profile" not in second_enriched


@pytest.mark.asyncio
async def test_image_rate_limit_stops_run_without_candidate_skip(
    monkeypatch,
    tmp_path,
):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR", raising=False)
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 10)
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "KEYWORDS", "test")
    monkeypatch.setattr(config, "PUBLISH_TIME_TYPE", 0)
    crawler = DouYinCrawler()
    crawler.dy_client = SearchClient()
    crawler.enrich_aweme_creator = AsyncMock(side_effect=lambda aweme: aweme)
    crawler.get_aweme_images = AsyncMock(
        side_effect=DouyinImageDownloadError(
            "retry-aweme", 0, "image_rate_limited", attempts=1
        )
    )
    store = AsyncMock(return_value=None)
    monkeypatch.setattr(crawler, "update_douyin_aweme", store)

    await crawler.search()

    store.assert_not_awaited()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    assert not [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "runtime_failed"
    assert stopped["details"]["stop_detail"] == "image_rate_limited"
    assert stopped["details"]["candidate_identities"] == []


@pytest.mark.asyncio
async def test_image_failure_is_recorded_seen_and_later_candidate_continues(
    monkeypatch, tmp_path
):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR", raising=False)
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 10)
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "KEYWORDS", "test")
    monkeypatch.setattr(config, "PUBLISH_TIME_TYPE", 0)
    crawler = DouYinCrawler()
    crawler.dy_client = SearchClient()
    crawler.enrich_aweme_creator = AsyncMock(side_effect=lambda aweme: aweme)
    crawler.get_aweme_images = AsyncMock(
        side_effect=[
            DouyinImageDownloadError(
                "retry-aweme", 0, "image_download_retryable", attempts=3
            ),
            None,
        ]
    )
    store = AsyncMock(return_value=None)
    monkeypatch.setattr(crawler, "update_douyin_aweme", store)

    await crawler.search()

    store.assert_awaited_once()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    assert skipped[0]["details"]["identity"] == "retry-aweme"
    assert skipped[0]["details"]["failure_scope"] == "image"
    assert skipped[0]["details"]["attempts"] == 3
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["resume_page"] == 2
    assert stopped["details"]["resume_offset"] == 10
    assert stopped["details"]["resume_cursor"] == "fresh-search-id"
    assert stopped["details"]["candidate_count"] == 2
    assert stopped["details"]["candidate_identities"] == [
        "retry-aweme",
        "success-aweme",
    ]


@pytest.mark.asyncio
async def test_terminal_image_failure_is_recorded_and_later_candidate_continues(
    monkeypatch, tmp_path
):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR", raising=False)
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 10)
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "KEYWORDS", "test")
    monkeypatch.setattr(config, "PUBLISH_TIME_TYPE", 0)
    crawler = DouYinCrawler()
    crawler.dy_client = SearchClient()
    crawler.enrich_aweme_creator = AsyncMock(side_effect=lambda aweme: aweme)
    crawler.get_aweme_images = AsyncMock(
        side_effect=[
            DouyinImageDownloadError(
                "retry-aweme", 0, "image_too_large", attempts=1
            ),
            None,
        ]
    )
    store = AsyncMock(return_value=None)
    monkeypatch.setattr(crawler, "update_douyin_aweme", store)

    await crawler.search()

    store.assert_awaited_once()
    assert crawler.get_aweme_images.await_count == 2
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    assert skipped[0]["details"]["identity"] == "retry-aweme"
    assert skipped[0]["details"]["failure_scope"] == "image"
    assert skipped[0]["details"]["retryable"] is False
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["resume_page"] == 2
    assert stopped["details"]["resume_offset"] == 10
    assert stopped["details"]["candidate_identities"] == [
        "retry-aweme",
        "success-aweme",
    ]
