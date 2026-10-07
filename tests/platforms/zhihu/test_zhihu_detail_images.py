from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from .support import config, make_crawler as ZhihuCrawler
from trippostcollect.platforms.zhihu.core import (
    ZhihuDetailFetchError,
    ZhihuImageDownloadError,
)
from trippostcollect.platforms.zhihu.models import DataFetchError
from trippostcollect.platforms.zhihu.models import ZhihuContent
from trippostcollect.platforms.zhihu import parser as zhihu_store


def test_zhihu_numeric_width_variants_share_one_logical_asset() -> None:
    content = ZhihuContent(
        content_id="numeric-width-variants",
        content_type="answer",
        content_detail_status="detail_observed",
        image_list=[
            "https://pic1.zhimg.com/v2-same-asset_r.jpg",
            "https://pic2.zhimg.com/v2-same-asset_720w.jpg",
            "https://pic3.zhimg.com/v2-same-asset_1440w.webp",
        ],
    )

    assets = zhihu_store.zhihu_content_image_assets(content)

    assert len(assets) == 1
    assert assets[0]["url"] == "https://pic1.zhimg.com/v2-same-asset_r.jpg"


@pytest.mark.parametrize("hostname", ["cdn.example", "evilzhimg.com"])
def test_zhihu_transform_suffixes_do_not_merge_non_zhimg_assets(hostname: str) -> None:
    content = ZhihuContent(
        content_id=f"external-{hostname}",
        content_type="answer",
        content_detail_status="detail_observed",
        image_list=[
            f"https://{hostname}/v2-same-asset_r.jpg",
            f"https://{hostname}/v2-same-asset_720w.jpg",
        ],
    )

    assets = zhihu_store.zhihu_content_image_assets(content)

    assert len(assets) == 2


def test_zhihu_encoded_transform_suffix_uses_real_zhimg_asset_identity() -> None:
    content = ZhihuContent(
        content_id="encoded-width-variant",
        content_type="answer",
        content_detail_status="detail_observed",
        image_list=[
            "https://pic1.zhimg.com/v2-same-asset_r.jpg",
            "https://pic2.zhimg.com/v2-same-asset%5F720w.jpg",
        ],
    )

    assets = zhihu_store.zhihu_content_image_assets(content)

    assert len(assets) == 1


@pytest.mark.asyncio
async def test_search_payload_with_body_images_becomes_observed_without_detail_request(
    monkeypatch,
):
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    content = ZhihuContent(
        content_id="search-image",
        content_type="answer",
        content_text="complete search body",
        image_list=["https://pic1.zhimg.com/v2-search_r.jpg"],
    )

    result = await crawler.enrich_search_content_detail(content)

    assert result.content_detail_status == "detail_observed"
    assert result.content_detail_source == "search_content"
    crawler.zhihu_client.get_answer_info.assert_not_awaited()
    assert [asset["url"] for asset in zhihu_store.zhihu_content_image_assets(result)] == [
        "https://pic1.zhimg.com/v2-search_r.jpg"
    ]


@pytest.mark.asyncio
async def test_search_without_images_uses_detail_body_and_filters_formula(monkeypatch):
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    crawler.zhihu_client.get_answer_info.return_value = ZhihuContent(
        content_id="detail-image",
        content_type="answer",
        content_text="full body",
        image_list=[
            "https://pic1.zhimg.com/v2-detail_b.webp",
            "https://www.zhihu.com/equation?tex=y",
        ],
    )
    search = ZhihuContent(
        content_id="detail-image",
        question_id="question-1",
        content_type="answer",
        creator_hash="author",
        followers_observed=True,
    )

    result = await crawler.enrich_search_content_detail(search)

    assert result.content_detail_status == "detail_observed"
    assert result.content_detail_source == "answer_detail"
    assert result.content_text == "full body"
    assert [asset["url"] for asset in zhihu_store.zhihu_content_image_assets(result)] == [
        "https://pic1.zhimg.com/v2-detail_b.webp"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["request_failed", "parse_failed"])
async def test_failed_or_unparsed_detail_cannot_enter_image_success(monkeypatch, mode):
    monkeypatch.setattr(config, "ENABLE_GET_MEIDAS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    if mode == "request_failed":
        crawler.zhihu_client.get_answer_info.side_effect = DataFetchError("failed")
    else:
        crawler.zhihu_client.get_answer_info.return_value = None
    content = ZhihuContent(
        content_id=mode,
        question_id="question-1",
        content_type="answer",
        image_list=[],
    )

    with pytest.raises(ZhihuDetailFetchError) as exc_info:
        await crawler.enrich_search_content_detail(content)

    assert exc_info.value.code == f"detail_{mode}"
    crawler.zhihu_client.get_content_image.assert_not_awaited()
    assert zhihu_store.zhihu_content_image_assets(content) == []


@pytest.mark.asyncio
async def test_detail_failure_is_recorded_seen_and_search_continues(
    monkeypatch,
    tmp_path,
):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "START_PAGE", 4)
    monkeypatch.setattr(config, "KEYWORDS", "test")
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 20)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    crawler.zhihu_client.get_note_by_keyword.side_effect = [
        [
            ZhihuContent(
                content_id="retry-detail",
                question_id="question-1",
                content_type="answer",
                content_text="search excerpt",
            )
        ],
        [],
    ]
    crawler.zhihu_client.get_answer_info.side_effect = DataFetchError("temporary")
    store = AsyncMock(return_value=None)
    monkeypatch.setattr(crawler, "_store_content", store)

    await crawler.search()

    store.assert_not_awaited()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert skipped[0]["details"]["identity"] == "retry-detail"
    assert skipped[0]["details"]["failure_scope"] == "post"
    assert skipped[0]["details"]["attempts"] == 3
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["resume_page"] == 5
    assert stopped["details"]["candidate_identities"] == ["retry-detail"]


@pytest.mark.asyncio
async def test_image_failure_is_recorded_and_later_zhihu_candidate_continues(
    monkeypatch,
    tmp_path,
):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "START_PAGE", 4)
    monkeypatch.setattr(config, "KEYWORDS", "test")
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 20)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)

    def content(content_id: str) -> ZhihuContent:
        return ZhihuContent(
            content_id=content_id,
            question_id="question-1",
            content_type="answer",
            content_text="test full body",
            content_url=f"https://www.zhihu.com/question/1/answer/{content_id}",
            created_time=1_700_000_000,
            creator_hash=f"author-{content_id}",
            user_nickname="author",
            followers_observed=True,
            image_list=[f"https://pic1.zhimg.com/{content_id}_r.jpg"],
            content_detail_status="detail_observed",
            content_detail_source="search_content",
        )

    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    crawler.zhihu_client.get_note_by_keyword.side_effect = [
        [content("retry-image"), content("success-image")],
        [],
    ]
    crawler.enrich_search_content_detail = AsyncMock(side_effect=lambda item: item)
    crawler.get_content_images = AsyncMock(
        side_effect=[
            ZhihuImageDownloadError(
                "retry-image", 0, "image_download_retryable", attempts=3
            ),
            None,
        ]
    )
    store = AsyncMock(return_value=None)
    monkeypatch.setattr(crawler, "_store_content", store)

    await crawler.search()

    store.assert_awaited_once()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert skipped[0]["details"]["identity"] == "retry-image"
    assert skipped[0]["details"]["failure_scope"] == "image"
    assert skipped[0]["details"]["attempts"] == 3
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["resume_page"] == 5
    assert stopped["details"]["candidate_identities"] == [
        "retry-image",
        "success-image",
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
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "START_PAGE", 4)
    monkeypatch.setattr(config, "KEYWORDS", "test")
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 20)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    terminal = ZhihuContent(
        content_id="terminal-image",
        question_id="question-1",
        content_type="answer",
        content_text="test full body",
        content_url="https://www.zhihu.com/question/1/answer/terminal-image",
        created_time=1_700_000_000,
        creator_hash="author-terminal",
        user_nickname="author",
        followers_observed=True,
        image_list=["https://pic1.zhimg.com/terminal_r.jpg"],
        content_detail_status="detail_observed",
        content_detail_source="search_content",
    )
    success = ZhihuContent(
        content_id="success-image",
        question_id="question-2",
        content_type="answer",
        content_text="test full body",
        content_url="https://www.zhihu.com/question/2/answer/success-image",
        created_time=1_700_000_000,
        creator_hash="author-success",
        user_nickname="author",
        followers_observed=True,
        image_list=["https://pic1.zhimg.com/success_r.jpg"],
        content_detail_status="detail_observed",
        content_detail_source="search_content",
    )
    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    crawler.zhihu_client.get_note_by_keyword.side_effect = [
        [terminal, success],
        [],
    ]
    crawler.enrich_search_content_detail = AsyncMock(side_effect=lambda item: item)
    crawler.get_content_images = AsyncMock(
        side_effect=[
            ZhihuImageDownloadError(
                "terminal-image", 0, "image_non_raster_response", attempts=1
            ),
            None,
        ]
    )
    store = AsyncMock(return_value=None)
    monkeypatch.setattr(crawler, "_store_content", store)

    await crawler.search()

    store.assert_awaited_once()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    skipped = [event for event in events if event["type"] == "candidate_skipped"]
    assert skipped[0]["details"]["identity"] == "terminal-image"
    assert skipped[0]["details"]["failure_scope"] == "image"
    assert skipped[0]["details"]["retryable"] is False
    stopped = [event for event in events if event["type"] == "adaptive_search_stopped"][-1]
    assert stopped["details"]["stop_reason"] == "source_exhausted"
    assert stopped["details"]["resume_page"] == 5
    assert stopped["details"]["candidate_identities"] == [
        "success-image",
        "terminal-image",
    ]
