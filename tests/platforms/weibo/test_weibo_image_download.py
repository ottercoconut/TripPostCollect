from __future__ import annotations

import asyncio
from io import BytesIO
import json
import sqlite3
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from dataclasses import replace
from support.weibo_adapter import settings, crawler as make_crawler

from trippostcollect.platforms.weibo.core import (
    WeiboFullTextFetchError,
    WeiboImageDownloadError,
)
from trippostcollect.platforms.weibo.models import DataFetchError, PlatformRuntimeError
from trippostcollect.platforms.weibo.client import weibo_image_request_urls
from trippostcollect.runtime.image_retry import ImageDownloadFetchError

config = settings()


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 3), color="blue").save(output, format="PNG")
    return output.getvalue()


def test_weibo_image_urls_do_not_double_wrap_existing_proxy() -> None:
    assert weibo_image_request_urls(
        "https://i1.wp.com/ww3.sinaimg.cn/large/example.jpg"
    ) == [
        "https://i1.wp.com/ww3.sinaimg.cn/large/example.jpg",
        "https://ww3.sinaimg.cn/large/example.jpg",
    ]


def test_weibo_image_urls_keep_bounded_proxy_and_direct_fallbacks() -> None:
    assert weibo_image_request_urls(
        "https://wx4.sinaimg.cn/orj360/example.jpg?token=ignored"
    ) == [
        "https://i1.wp.com/wx4.sinaimg.cn/large/example.jpg",
        "https://wx4.sinaimg.cn/large/example.jpg",
    ]


def valid_mblog(note_id: str) -> dict:
    return {
        "id": note_id,
        "text": "valid body",
        "content_detail_status": "detail_observed",
        "content_detail_source": "search_mblog_complete",
        "created_at": "Sat Jun 14 12:00:00 +0800 2025",
        "attitudes_count": 1,
        "comments_count": 2,
        "reposts_count": 3,
        "pics": [{"pid": f"pid-{note_id}", "url": f"https://wx.test/{note_id}.jpg"}],
        "user": {"id": "author", "screen_name": "name", "followers_count": 4},
    }


@pytest.mark.asyncio
async def test_detail_task_marks_mobile_detail_as_authoritative(monkeypatch):
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    crawler = make_crawler(config)
    crawler.wb_client = AsyncMock()
    crawler.wb_client.get_note_info_by_id.return_value = {
        "mblog": {"id": "detail-note", "text": "完整正文"}
    }

    result = await crawler.get_note_info_task("detail-note", asyncio.Semaphore(1))

    assert result["mblog"]["content_detail_status"] == "detail_observed"
    assert result["mblog"]["content_detail_source"] == "mobile_detail"


@pytest.mark.asyncio
async def test_specified_detail_downloads_images_before_store(monkeypatch):
    monkeypatch.setattr(config, "WEIBO_SPECIFIED_ID_LIST", ["detail-note"])
    crawler = make_crawler(config)
    note = {"mblog": valid_mblog("detail-note")}
    crawler.get_note_info_task = AsyncMock(return_value=note)
    crawler.get_note_images = AsyncMock()
    crawler.batch_get_notes_comments = AsyncMock()
    store = AsyncMock()
    monkeypatch.setattr(crawler, "update_weibo_note", store)

    await crawler.get_specified_notes()

    crawler.get_note_images.assert_awaited_once_with(note["mblog"])
    store.assert_awaited_once_with(note)


@pytest.mark.asyncio
async def test_get_note_images_passes_note_pid_order_and_url(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    crawler = make_crawler(config)
    crawler.wb_client = AsyncMock()
    crawler.wb_client.get_note_image.side_effect = [png_bytes(), png_bytes()]
    mblog = valid_mblog("note-download")
    mblog["pics"] = [
        {"pid": "pid-first", "url": "https://wx.test/one.jpg?x=1"},
        {"pid": "pid-second", "large": {"url": "https://wx.test/two.webp?x=2"}},
    ]

    await crawler.get_note_images(mblog)

    rows = [
        json.loads(line)
        for line in (tmp_path / "weibo" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [(row["platform_post_id"], row["source_index"]) for row in rows] == [
        ("note-download", 0),
        ("note-download", 1),
    ]
    assert [row["source_asset_key"] for row in rows] == [
        "weibo:pid:pid-first",
        "weibo:pid:pid-second",
    ]
    assert all(row["mime_type"] == "image/png" for row in rows)
    assert all(row["staging_path"].endswith(".png") for row in rows)


@pytest.mark.asyncio
async def test_transient_fetch_recovers_and_records_attempts(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(
        "trippostcollect.runtime.image_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS",
        (0.0, 0.0),
    )
    crawler = make_crawler(config)
    crawler.wb_client = AsyncMock()
    crawler.wb_client.get_note_image.side_effect = [None, png_bytes()]

    await crawler.get_note_images(valid_mblog("recovered-note"))

    rows = [
        json.loads(line)
        for line in (tmp_path / "weibo" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [(row["fetch_status"], row["attempts"]) for row in rows] == [
        ("downloaded", 2)
    ]
    assert crawler.wb_client.get_note_image.await_count == 2


@pytest.mark.asyncio
async def test_failed_fetch_writes_only_failed_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(
        "trippostcollect.runtime.image_retry.IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS",
        (0.0, 0.0),
    )
    crawler = make_crawler(config)
    crawler.wb_client = AsyncMock()
    crawler.wb_client.get_note_image.return_value = None

    with pytest.raises(WeiboImageDownloadError):
        await crawler.get_note_images(valid_mblog("failed-note"))

    rows = [
        json.loads(line)
        for line in (tmp_path / "weibo" / "image_manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["fetch_status"] == "failed"
    assert rows[0]["attempts"] == 3
    assert rows[0]["staging_path"] is None
    assert crawler.wb_client.get_note_image.await_count == 3
    assert not list((tmp_path / "weibo").rglob("*.png"))


@pytest.mark.asyncio
async def test_terminal_http_fetch_error_preserves_manifest_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    crawler = make_crawler(config)
    crawler.wb_client = AsyncMock()
    crawler.wb_client.get_note_image.side_effect = ImageDownloadFetchError(
        "HTTP 404",
        code="image_source_unavailable",
        retryable=False,
        http_status=404,
    )

    with pytest.raises(WeiboImageDownloadError):
        await crawler.get_note_images(valid_mblog("missing-note"))

    row = json.loads(
        (tmp_path / "weibo" / "image_manifest.jsonl").read_text().splitlines()[0]
    )
    assert row["error_code"] == "image_source_unavailable"
    assert row["http_status"] == 404
    assert row["attempts"] == 1


class SearchClient:
    def __init__(self, cards):
        self.cards = cards
        self.calls = 0

    async def get_note_by_keyword(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return {"cards": self.cards}
        return {"cards": []}


def prepare_search(monkeypatch, tmp_path, cards):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "KEYWORDS", "test")
    monkeypatch.setattr(config, "WEIBO_SEARCH_TYPE", "default")
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    crawler = make_crawler(config)
    crawler.wb_client = SearchClient(cards)
    crawler.batch_get_notes_full_text = AsyncMock(
        side_effect=lambda notes: notes
    )
    crawler.batch_get_notes_comments = AsyncMock(return_value=None)
    crawler.get_note_images = AsyncMock(return_value=None)
    monkeypatch.setattr(crawler, "update_weibo_note", AsyncMock(return_value=None))
    return crawler, state_path


@pytest.mark.asyncio
async def test_known_id_and_invalid_note_are_skipped_before_media(monkeypatch, tmp_path):
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
        connection.execute("INSERT INTO web_posts VALUES ('weibo', 'known', NULL)")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DB_PATH", str(db_path))
    invalid = valid_mblog("invalid")
    invalid["user"].pop("followers_count")
    crawler, _ = prepare_search(
        monkeypatch,
        tmp_path,
        [
            {"card_type": 9, "mblog": valid_mblog("known")},
            {"card_type": 9, "mblog": invalid},
            {"card_type": 9, "mblog": valid_mblog("new")},
        ],
    )

    await crawler.search()

    crawler.get_note_images.assert_awaited_once()
    assert crawler.get_note_images.await_args.args[0]["id"] == "new"


@pytest.mark.asyncio
async def test_topic_relevance_uses_the_body_that_store_persists(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    false_raw_match = valid_mblog("html-attribute-only")
    false_raw_match["text"] = '<a title="青岛">普通正文</a>'
    true_persisted_match = valid_mblog("html-split-literal")
    true_persisted_match["text"] = "<span>青</span><span>岛</span>攻略"
    crawler, state_path = prepare_search(
        monkeypatch,
        tmp_path,
        [
            {"card_type": 9, "mblog": false_raw_match},
            {"card_type": 9, "mblog": true_persisted_match},
        ],
    )
    monkeypatch.setattr(crawler, "config", replace(crawler.config, KEYWORDS="青岛旅游"))

    await crawler.search()

    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["candidate_count"] == 2
    assert stopped["details"]["valid_new_count"] == 1
    assert stopped["details"]["candidate_identities"] == [
        "html-attribute-only",
        "html-split-literal",
    ]
    assert not [event for event in events if event["type"] == "candidate_skipped"]


@pytest.mark.asyncio
async def test_image_failure_is_recorded_and_later_candidate_continues(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, state_path = prepare_search(
        monkeypatch,
        tmp_path,
        [
            {"card_type": 9, "mblog": valid_mblog("retry-note")},
            {"card_type": 9, "mblog": valid_mblog("success-note")},
        ],
    )
    crawler.get_note_images.side_effect = [
        WeiboImageDownloadError(
            "retry-note", 0, "image_download_retryable", attempts=3
        ),
        None,
    ]

    await crawler.search()

    crawler.update_weibo_note.assert_awaited_once()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    assert skipped[0]["details"]["identity"] == "retry-note"
    assert skipped[0]["details"]["failure_scope"] == "image"
    assert skipped[0]["details"]["attempts"] == 3
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["resume_page"] == 2
    assert stopped["details"]["candidate_count"] == 2
    assert stopped["details"]["candidate_identities"] == [
        "retry-note",
        "success-note",
    ]


@pytest.mark.asyncio
async def test_terminal_image_failure_is_recorded_and_later_candidate_continues(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    crawler, state_path = prepare_search(
        monkeypatch,
        tmp_path,
        [
            {"card_type": 9, "mblog": valid_mblog("terminal-note")},
            {"card_type": 9, "mblog": valid_mblog("must-not-store")},
        ],
    )
    crawler.get_note_images.side_effect = [
        WeiboImageDownloadError(
            "terminal-note", 0, "image_decode_failed", attempts=1
        ),
        None,
    ]

    await crawler.search()

    crawler.update_weibo_note.assert_awaited_once()
    assert crawler.get_note_images.await_count == 2
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    assert skipped[0]["details"]["identity"] == "terminal-note"
    assert skipped[0]["details"]["failure_scope"] == "image"
    assert skipped[0]["details"]["retryable"] is False
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["resume_page"] == 2
    assert stopped["details"]["candidate_identities"] == [
        "must-not-store",
        "terminal-note",
    ]


@pytest.mark.asyncio
async def test_short_and_long_posts_record_authoritative_full_text_sources(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_WEIBO_FULL_TEXT", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    crawler = make_crawler(config)
    crawler.wb_client = AsyncMock()
    short = {"mblog": {"id": "short", "text": "complete", "isLongText": False}}
    long = {"mblog": {"id": "long", "text": "truncated", "isLongText": True}}
    crawler.wb_client.get_note_info_by_id.return_value = {
        "mblog": {"id": "long", "text": "complete long body", "isLongText": True}
    }

    short_result = await crawler.get_note_full_text(short)
    long_result = await crawler.get_note_full_text(long)

    assert short_result["mblog"]["content_detail_status"] == "detail_observed"
    assert short_result["mblog"]["content_detail_source"] == "search_mblog_complete"
    assert long_result["mblog"]["text"] == "complete long body"
    assert long_result["mblog"]["content_detail_status"] == "detail_observed"
    assert long_result["mblog"]["content_detail_source"] == "mobile_detail"


@pytest.mark.asyncio
async def test_long_text_detail_failure_is_recorded_seen_and_search_continues(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    long_mblog = valid_mblog("long-retry")
    long_mblog.update({"isLongText": True, "text": "truncated...全文"})
    crawler, state_path = prepare_search(
        monkeypatch,
        tmp_path,
        [{"card_type": 9, "mblog": long_mblog}],
    )
    crawler.get_note_full_text = AsyncMock(
        side_effect=WeiboFullTextFetchError(
            "long-retry",
            "detail_request_failed",
            attempts=3,
        )
    )

    await crawler.search()

    crawler.update_weibo_note.assert_not_awaited()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert skipped[0]["details"]["identity"] == "long-retry"
    assert skipped[0]["details"]["failure_scope"] == "post"
    assert skipped[0]["details"]["attempts"] == 3
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["resume_page"] == 2
    assert stopped["details"]["candidate_identities"] == ["long-retry"]


@pytest.mark.asyncio
async def test_long_text_detail_request_error_never_returns_search_excerpt(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_WEIBO_FULL_TEXT", True)
    crawler = make_crawler(config)
    crawler.wb_client = AsyncMock()
    crawler.wb_client.get_note_info_by_id.side_effect = DataFetchError("temporary")
    note = {"mblog": {"id": "long", "text": "truncated...全文", "isLongText": True}}

    with pytest.raises(WeiboFullTextFetchError) as exc_info:
        await crawler.get_note_full_text(note)

    assert exc_info.value.code == "detail_request_failed"
    assert "content_detail_status" not in note["mblog"]


@pytest.mark.asyncio
async def test_long_text_rate_limit_is_preserved_as_run_level_failure(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_WEIBO_FULL_TEXT", True)
    crawler = make_crawler(config)
    crawler.wb_client = AsyncMock()
    crawler.wb_client.get_note_info_by_id.side_effect = PlatformRuntimeError(
        "HTTP 429",
        code="rate_limited",
    )
    note = {"mblog": {"id": "long", "text": "truncated...全文", "isLongText": True}}

    with pytest.raises(WeiboFullTextFetchError) as exc_info:
        await crawler.get_note_full_text(note)

    assert exc_info.value.code == "rate_limited"
    assert exc_info.value.runtime_blocking is True
    assert "content_detail_status" not in note["mblog"]
