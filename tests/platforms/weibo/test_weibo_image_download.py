from __future__ import annotations

import asyncio
from io import BytesIO
import json
from unittest.mock import AsyncMock

from PIL import Image
import pytest

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


def test_weibo_image_urls_try_large_then_original_without_proxy() -> None:
    # #114：只直连新浪，large 优先，再退到平台原始地址；不再构造第三方图片代理地址。
    assert weibo_image_request_urls(
        "https://wx4.sinaimg.cn/orj360/example.jpg?token=ignored"
    ) == [
        "https://wx4.sinaimg.cn/large/example.jpg",
        "https://wx4.sinaimg.cn/orj360/example.jpg?token=ignored",
    ]
    assert weibo_image_request_urls("https://wx4.sinaimg.cn/large/example.jpg") == [
        "https://wx4.sinaimg.cn/large/example.jpg",
    ]


def test_weibo_image_urls_never_wrap_with_third_party_proxy() -> None:
    for url in (
        "https://ww3.sinaimg.cn/mw690/example.jpg",
        "https://wx.test/example.jpg",
    ):
        assert not any("wp.com" in candidate for candidate in weibo_image_request_urls(url))
    assert weibo_image_request_urls("https://wx.test/example.jpg") == ["https://wx.test/example.jpg"]


@pytest.mark.asyncio
async def test_weibo_image_request_sends_no_cookie_or_api_headers() -> None:
    from support.weibo_adapter import client_ports
    from trippostcollect.platforms.weibo.client import WeiboClient
    import httpx

    seen: list[tuple[str, dict]] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            seen.append((url, kwargs["headers"]))
            return httpx.Response(200, content=png_bytes(), request=httpx.Request(method, url))

    client = WeiboClient(
        headers={
            "User-Agent": "ua-test", "Cookie": "SUB=secret", "Origin": "https://m.weibo.cn",
            "Referer": "https://m.weibo.cn", "Content-Type": "application/json;charset=UTF-8",
        },
        playwright_page=None, cookie_dict={"SUB": "secret"},
        ports=client_ports(make_async_client=lambda **kwargs: Client()),
    )

    await client.get_note_image("https://wx1.sinaimg.cn/orj360/example.jpg")

    assert seen == [(
        "https://wx1.sinaimg.cn/large/example.jpg",
        {
            "User-Agent": "ua-test",
            "Referer": "https://m.weibo.cn/",
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8",
        },
    )]


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
