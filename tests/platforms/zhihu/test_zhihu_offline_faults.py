"""知乎离线故障与前沿阶段特征化（T13）：只替换 httpx 传输与搜索客户端边界，固定现行行为。

- 请求沿用原 Tenacity 3 次/1 秒/reraise，传输层异常原样上抛；
- 搜索与详情的传输层异常不属于 DataFetchError/RetryError，直接终止本轮且不宣称耗尽；
- 搜索结果全部为非目标类型时，过滤后为空即按 empty_page 宣称耗尽（规格 R03 基线，保持不改）；
- 续跑先刷新顶部有限页，再从保存的深层前沿继续。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from .support import config, make_crawler as ZhihuCrawler
from trippostcollect.application.contracts import ZhihuClientPorts
from trippostcollect.platforms.zhihu import models as constant
from trippostcollect.platforms.zhihu.client import ZhiHuClient
from trippostcollect.platforms.zhihu.models import ZhihuContent
from trippostcollect.platforms.zhihu.parser import ZhihuExtractor


class Transport:
    """只登记搜索与详情路径的 httpx 替身；未登记路径直接失败（F09 未匹配请求失败）。"""

    def __init__(self, fault: type[httpx.TransportError] = httpx.ConnectError):
        self.fault = fault
        self.calls: list[str] = []

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kwargs):
        path = httpx.URL(url).path
        if path != "/api/v4/search_v3" and not path.startswith(("/question/", "/p/")):
            raise AssertionError(f"未登记的请求：{method} {url}")
        self.calls.append(path)
        raise self.fault("离线网络中断", request=httpx.Request(method, url))


@pytest.fixture
def sleeps(monkeypatch):
    recorded: list[float] = []

    async def sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(ZhiHuClient.request.retry, "sleep", sleep)
    return recorded


def prepare(monkeypatch, tmp_path, *, start_page=1, refresh=0):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", str(refresh))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "START_PAGE", start_page)
    monkeypatch.setattr(config, "KEYWORDS", "青岛旅游")
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    crawler = ZhihuCrawler()
    crawler.zhihu_client = AsyncMock()
    monkeypatch.setattr(crawler, "_store_content", AsyncMock(return_value=None))
    return crawler, state_path


def events_of(state_path, kind):
    return [event["details"] for event in json.loads(state_path.read_text(encoding="utf-8"))["events"]
            if event["type"] == kind]


def answer(content_id: str) -> ZhihuContent:
    return ZhihuContent(
        content_id=content_id,
        question_id="question-1",
        content_type=constant.ANSWER_NAME,
        content_text="青岛完整正文",
        content_url=f"https://www.zhihu.com/question/1/answer/{content_id}",
        created_time=1_700_000_000,
        creator_hash=f"author-{content_id}",
        user_nickname="author",
        followers_observed=True,
        image_list=[f"https://pic1.zhimg.com/{content_id}_r.jpg"],
        content_detail_status="detail_observed",
        content_detail_source="search_content",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [httpx.ConnectError, httpx.ReadTimeout])
async def test_request_transport_error_reraises_after_three_attempts(sleeps, fault):
    transport = Transport(fault)
    client = ZhiHuClient(
        ports=ZhihuClientPorts(transport, AsyncMock()),
        headers={"cookie": "d_c0=t13", "user-agent": "t13"},
        playwright_page=MagicMock(),
        cookie_dict={"d_c0": "t13"},
    )

    with pytest.raises(fault):
        await client.request("GET", "https://www.zhihu.com/api/v4/search_v3?q=t13")

    assert transport.calls == ["/api/v4/search_v3"] * 3
    assert sleeps == [1, 1]


@pytest.mark.asyncio
async def test_unregistered_request_path_fails_instead_of_being_treated_as_detail(sleeps):
    client = ZhiHuClient(
        ports=ZhihuClientPorts(Transport(), AsyncMock()),
        headers={"cookie": "d_c0=t13"}, playwright_page=MagicMock(), cookie_dict={"d_c0": "t13"},
    )

    with pytest.raises(AssertionError, match="未登记的请求"):
        await client.request("GET", "https://www.zhihu.com/api/v4/unknown")


@pytest.mark.asyncio
async def test_search_transport_error_ends_run_without_exhaustion(monkeypatch, tmp_path):
    crawler, state_path = prepare(monkeypatch, tmp_path)
    crawler.zhihu_client.get_note_by_keyword.side_effect = httpx.ConnectError("离线网络中断")

    with pytest.raises(httpx.ConnectError):
        await crawler.search()

    # 只有 DataFetchError 被映射为 search_request_failed；传输层异常原样上抛，由父进程判运行失败。
    assert events_of(state_path, "adaptive_search_stopped") == []
    assert events_of(state_path, "adaptive_batch_completed") == []


@pytest.mark.asyncio
async def test_detail_transport_error_ends_run_without_candidate_skip(monkeypatch, tmp_path):
    crawler, state_path = prepare(monkeypatch, tmp_path)
    excerpt = answer("needs-detail")
    excerpt.content_detail_status = "search_only"
    excerpt.content_detail_source = ""
    excerpt.image_list = []
    crawler.zhihu_client.get_note_by_keyword.side_effect = [[excerpt], []]
    crawler.zhihu_client.get_answer_info.side_effect = httpx.ReadTimeout("离线读取超时")

    with pytest.raises(httpx.ReadTimeout):
        await crawler.search()

    # 详情只捕获 DataFetchError/RetryError（转为候选 skip）；reraise 的传输层异常终止本轮。
    assert crawler.zhihu_client.get_answer_info.await_count == 1
    assert events_of(state_path, "candidate_skipped") == []
    assert events_of(state_path, "adaptive_search_stopped") == []


@pytest.mark.asyncio
async def test_filtered_empty_search_page_claims_exhaustion_baseline(monkeypatch, tmp_path):
    crawler, state_path = prepare(monkeypatch, tmp_path)
    raw = {"data": [
        {"type": "relevant_query", "object": {"type": "answer", "id": "ignored-1"}},
        {"type": "knowledge_ad", "object": {"type": "article", "id": "ignored-2"}},
        {"type": "search_result", "object": {"type": "question", "id": "ignored-3"}},
    ], "paging": {"is_end": False}}
    extractor = ZhihuExtractor()
    assert extractor.extract_contents_from_search(raw) == []
    crawler.zhihu_client.get_note_by_keyword.side_effect = [extractor.extract_contents_from_search(raw)]

    await crawler.search()

    # 原始响应有 3 条且 is_end=False，过滤后为空仍按 empty_page 宣称耗尽：R03 基线，不冒充已核验终态。
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"], item["raw_batch_count"]) for item in stopped] == [
        ("source_exhausted", "empty_page", 0)
    ]


@pytest.mark.asyncio
async def test_top_refresh_precedes_saved_frontier(monkeypatch, tmp_path):
    crawler, state_path = prepare(monkeypatch, tmp_path, start_page=5, refresh=2)
    pages = {1: [answer("r1")], 5: [answer("f5")]}
    requested: list[int] = []

    async def search_page(**kwargs):
        requested.append(kwargs["page"])
        return pages.get(kwargs["page"], [])

    crawler.zhihu_client.get_note_by_keyword.side_effect = search_page
    crawler.enrich_search_content_detail = AsyncMock(side_effect=lambda item: item)
    crawler.get_content_images = AsyncMock(return_value=None)

    await crawler.search()

    # 刷新只到 min(start_page-1, 2) 页，刷新空页不宣称耗尽，随后从保存的前沿继续。
    assert requested == [1, 2, 5, 6]
    batches = [(item["source_page"], item["discovery_phase"]) for item in events_of(state_path, "adaptive_batch_completed")]
    assert batches == [(1, "refresh"), (5, "frontier")]
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert len(stopped) == 1
    assert stopped[0]["stop_reason"] == "source_exhausted"
    assert stopped[0]["discovery_phase"] == "frontier"
    assert stopped[0]["source_page"] == stopped[0]["resume_page"] == 6
