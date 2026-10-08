from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

from .support import config, make_crawler as ZhihuCrawler
import pytest
from trippostcollect.platforms.zhihu import models as constant
from trippostcollect.platforms.zhihu.core import (
    ZhihuDetailFetchError,
    ZhihuImageDownloadError,
)
from trippostcollect.platforms.zhihu.models import PlatformRuntimeError
from trippostcollect.platforms.zhihu.parser import (
    ZhihuExtractor,
    extract_zhihu_content_text,
    merge_search_content_detail,
)
from trippostcollect.platforms.zhihu.models import ZhihuContent


def test_merge_search_detail_keeps_search_author_evidence() -> None:
    search_content = ZhihuContent(
        content_id="answer-1",
        content_type="answer",
        content_text="search excerpt",
        creator_hash="search-author",
        followers_count=123,
        followers_observed=True,
        author_followers_source="search_author",
    )
    detail_content = ZhihuContent(
        content_id="answer-1",
        content_type="answer",
        content_text="full detail",
        image_list=["https://example.test/content.jpg"],
        creator_hash="detail-author",
        followers_count=0,
        followers_observed=False,
    )

    merged = merge_search_content_detail(search_content, detail_content)

    assert merged.content_text == "full detail"
    assert merged.image_list == ["https://example.test/content.jpg"]
    assert merged.image_count == 1
    assert merged.content_detail_status == "detail_observed"
    assert merged.content_detail_source == "answer_detail"
    assert merged.creator_hash == "search-author"
    assert merged.followers_count == 123
    assert merged.followers_observed is True
    assert merged.author_followers_source == "search_author"


def test_detail_html_selects_exact_non_empty_answer_entity() -> None:
    payload = {
        "initialState": {
            "entities": {
                "answers": {
                    "wrong": {"id": "wrong", "type": "answer", "content": "wrong body"},
                    "target": {
                        "id": "target",
                        "type": "answer",
                        "content": "<p>target body</p>",
                        "question": {"id": "question-1"},
                    },
                }
            }
        }
    }
    html = f'<script id="js-initialData">{json.dumps(payload)}</script>'

    result = ZhihuExtractor().extract_answer_content_from_html(html, "target")

    assert result is not None
    assert result.content_id == "target"
    assert result.content_text == "target body"


def test_content_text_preserves_blocks_and_omits_figures() -> None:
    content_html = (
        "<p>第一段<span>行内文字</span></p>"
        '<figure><img src="https://pic1.zhimg.com/one.jpg">'
        "<figcaption>重复图片说明</figcaption></figure>"
        '<figure><img data-original="https://pic1.zhimg.com/two.jpg">'
        "<figcaption>重复图片说明</figcaption></figure>"
        "<p>第二段<br>换行文字</p>"
        "<ul><li>列表甲</li><li>列表乙</li></ul>"
    )

    assert extract_zhihu_content_text(content_html) == (
        "第一段行内文字\n第二段\n换行文字\n列表甲\n列表乙"
    )


@pytest.mark.parametrize(
    ("content_type", "entity_key", "extract_method"),
    [
        ("answer", "answers", "extract_answer_content_from_html"),
        ("article", "articles", "extract_article_content_from_html"),
    ],
)
def test_answer_and_article_drop_captions_but_keep_figure_images(
    content_type: str,
    entity_key: str,
    extract_method: str,
) -> None:
    target_id = f"{content_type}-target"
    content_html = (
        "<p>正文甲</p>"
        '<figure><img data-original="https://pic1.zhimg.com/one_r.jpg">'
        "<figcaption>小鱼山公园</figcaption></figure>"
        '<figure><img data-actualsrc="https://pic1.zhimg.com/two_r.jpg">'
        "<figcaption>小鱼山公园</figcaption></figure>"
        "<p>正文乙</p>"
    )
    entity = {
        "id": target_id,
        "type": content_type,
        "content": content_html,
    }
    if content_type == "answer":
        entity["question"] = {"id": "question-1"}
    payload = {"initialState": {"entities": {entity_key: {target_id: entity}}}}
    page_html = f'<script id="js-initialData">{json.dumps(payload)}</script>'

    result = getattr(ZhihuExtractor(), extract_method)(page_html, target_id)

    assert result is not None
    assert result.content_text == "正文甲\n正文乙"
    assert result.image_list == [
        "https://pic1.zhimg.com/one_r.jpg",
        "https://pic1.zhimg.com/two_r.jpg",
    ]
    assert result.image_count == 2


def test_detail_html_rejects_empty_target_even_when_other_entity_has_body() -> None:
    payload = {
        "initialState": {
            "entities": {
                "articles": {
                    "other": {"id": "other", "type": "article", "content": "other body"},
                    "target": {"id": "target", "type": "article", "content": ""},
                }
            }
        }
    }
    html = f'<script id="js-initialData">{json.dumps(payload)}</script>'

    assert ZhihuExtractor().extract_article_content_from_html(html, "target") is None


@pytest.mark.asyncio
async def test_detail_rate_limit_is_preserved_as_run_level_failure() -> None:
    crawler = ZhihuCrawler()
    crawler.zhihu_client = type(
        "Client",
        (),
        {
            "get_answer_info": lambda self, question_id, answer_id: _raise_async(
                PlatformRuntimeError("HTTP 429", code="rate_limited")
            )
        },
    )()
    content = ZhihuContent(
        content_id="answer-rate-limited",
        question_id="question-1",
        content_type=constant.ANSWER_NAME,
    )

    with pytest.raises(ZhihuDetailFetchError) as exc_info:
        await crawler.enrich_search_content_detail(content)

    assert exc_info.value.code == "rate_limited"
    assert exc_info.value.runtime_blocking is True


async def _raise_async(error: BaseException):
    raise error


async def _return_none_async(*_: object) -> None:
    return None


@pytest.mark.asyncio
async def test_detail_mode_marks_success_and_parse_failure(monkeypatch) -> None:
    crawler = ZhihuCrawler()

    class Client:
        async def get_answer_info(self, question_id: str, answer_id: str):
            if answer_id == "missing":
                return None
            return ZhihuContent(
                content_id=answer_id,
                question_id=question_id,
                content_type=constant.ANSWER_NAME,
                content_text="full body",
                image_list=[],
            )

    crawler.zhihu_client = Client()

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    observed = await crawler.get_note_detail(
        "https://www.zhihu.com/question/123/answer/observed",
        asyncio.Semaphore(1),
    )
    failed = await crawler.get_note_detail(
        "https://www.zhihu.com/question/123/answer/missing",
        asyncio.Semaphore(1),
    )

    assert observed is not None
    assert observed.content_detail_status == "detail_observed"
    assert observed.content_detail_source == "answer_detail"
    assert failed is not None
    assert failed.content_detail_status == "parse_failed"


@pytest.mark.asyncio
async def test_specified_detail_mode_reuses_one_semaphore(monkeypatch) -> None:
    crawler = ZhihuCrawler()
    semaphore_ids: list[int] = []

    async def fake_detail(full_note_url: str, semaphore: asyncio.Semaphore):
        semaphore_ids.append(id(semaphore))
        return ZhihuContent(
            content_id=full_note_url.rsplit("/", 1)[-1],
            content_type=constant.ARTICLE_NAME,
            content_text="full body",
            image_list=["https://pic1.zhimg.com/detail_r.jpg"],
            content_detail_status="detail_observed",
            content_detail_source="article_detail",
        )

    stored: list[str] = []

    async def fake_store(content: ZhihuContent, **kwargs):
        stored.append(content.content_id)

    async def no_comments(_: list[ZhihuContent]) -> None:
        return None

    monkeypatch.setattr(config, "ZHIHU_SPECIFIED_ID_LIST", [
        "https://zhuanlan.zhihu.com/p/1",
        "https://zhuanlan.zhihu.com/p/2",
    ])
    monkeypatch.setattr(config, "MAX_CONCURRENCY_NUM", 1)
    monkeypatch.setattr(crawler, "get_note_detail", fake_detail)
    monkeypatch.setattr(crawler, "get_content_images", _return_none_async)
    monkeypatch.setattr(crawler, "_store_content", fake_store)

    await crawler.get_specified_notes()

    assert len(set(semaphore_ids)) == 1
    assert stored == ["1", "2"]


@pytest.mark.asyncio
async def test_specified_detail_image_failure_does_not_block_later_candidate(
    monkeypatch,
) -> None:
    crawler = ZhihuCrawler()

    async def fake_detail(full_note_url: str, semaphore: asyncio.Semaphore):
        del semaphore
        content_id = full_note_url.rsplit("/", 1)[-1]
        return ZhihuContent(
            content_id=content_id,
            content_type=constant.ARTICLE_NAME,
            content_text="full body",
            image_list=[f"https://pic1.zhimg.com/{content_id}_r.jpg"],
            content_detail_status="detail_observed",
            content_detail_source="article_detail",
        )

    image_calls: list[str] = []

    async def fake_images(content: ZhihuContent) -> None:
        image_calls.append(content.content_id)
        if content.content_id == "failed":
            raise ZhihuImageDownloadError(
                "failed",
                0,
                "image_source_unavailable",
                attempts=1,
            )

    stored: list[str] = []

    async def fake_store(content: ZhihuContent, **kwargs) -> None:
        stored.append(content.content_id)

    monkeypatch.setattr(
        config,
        "ZHIHU_SPECIFIED_ID_LIST",
        [
            "https://zhuanlan.zhihu.com/p/failed",
            "https://zhuanlan.zhihu.com/p/success",
        ],
    )
    monkeypatch.setattr(config, "MAX_CONCURRENCY_NUM", 1)
    monkeypatch.setattr(crawler, "get_note_detail", fake_detail)
    monkeypatch.setattr(crawler, "get_content_images", fake_images)
    monkeypatch.setattr(crawler, "_store_content", fake_store)

    await crawler.get_specified_notes()

    assert image_calls == ["failed", "success"]
    assert stored == ["success"]


@pytest.mark.asyncio
async def test_candidate_with_missing_raw_nickname_is_not_valid(monkeypatch, tmp_path) -> None:
    """#49：作者昵称存原始值后，缺昵称不再被脱敏成 "*" 掩盖，知乎候选判为无效、不下载正文图。"""
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "KEYWORDS", "青岛旅游")

    def answer(content_id: str, name: str) -> dict:
        return {"type": "search_result", "object": {
            "id": content_id, "type": "answer", "question": {"id": "10"}, "created_time": 1_700_000_000,
            "content": f'<p>青岛完整正文</p><img src="https://pic1.zhimg.com/v2-{content_id}_r.jpg">',
            "author": {"id": f"author-{content_id}", "url_token": f"token-{content_id}", "name": name,
                       "follower_count": 3},
        }}

    contents = ZhihuExtractor().extract_contents_from_search(
        {"data": [answer("named", "青岛作者"), answer("unnamed", "")]}
    )
    assert [item.user_nickname for item in contents] == ["青岛作者", ""]

    def observed(item):
        item.content_detail_status = "detail_observed"
        item.content_detail_source = "search_content"
        return item

    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    crawler.zhihu_client.get_note_by_keyword.side_effect = [contents, []]
    crawler.enrich_search_content_detail = AsyncMock(side_effect=observed)
    crawler.get_content_images = AsyncMock(return_value=None)
    monkeypatch.setattr(crawler, "_store_content", AsyncMock(return_value=None))

    await crawler.search()

    assert [call.args[0].content_id for call in crawler.get_content_images.await_args_list] == ["named"]
    batches = [event["details"] for event in json.loads(state_path.read_text(encoding="utf-8"))["events"]
               if event["type"] == "adaptive_batch_completed"]
    assert batches[0]["valid_new_count"] == 1
