"""知乎离线故障与前沿阶段特征化（T13）：只替换 httpx 传输与搜索客户端边界，固定现行行为。

- 请求沿用原 Tenacity 3 次/1 秒/reraise，传输层异常原样上抛；401/403/429/404 不重试（#113）；
- 搜索与详情的传输层异常不属于 DataFetchError/RetryError，直接终止本轮且不宣称耗尽；
- #113 改掉 R03 基线：只有原始 paging.is_end=true 才是来源耗尽；过滤后为空但 is_end=false 继续翻页，
  空 data 无 is_end、缺 paging 或 HTTP 404 都是运行级失败；
- 续跑先刷新顶部有限页，再从保存的深层前沿继续。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from .support import END, config, make_crawler as ZhihuCrawler, search_client, search_page
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
    crawler.zhihu_client = search_client()
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
    crawler.zhihu_client.get_note_by_keyword.side_effect = [search_page([excerpt]), END]
    crawler.zhihu_client.get_answer_info.side_effect = httpx.ReadTimeout("离线读取超时")

    with pytest.raises(httpx.ReadTimeout):
        await crawler.search()

    # 详情只捕获 DataFetchError/RetryError（转为候选 skip）；reraise 的传输层异常终止本轮。
    assert crawler.zhihu_client.get_answer_info.await_count == 1
    assert events_of(state_path, "candidate_skipped") == []
    assert events_of(state_path, "adaptive_search_stopped") == []


@pytest.mark.asyncio
async def test_top_refresh_precedes_saved_frontier(monkeypatch, tmp_path):
    crawler, state_path = prepare(monkeypatch, tmp_path, start_page=5, refresh=2)
    pages = {1: search_page([answer("r1")]), 5: search_page([answer("f5")])}
    requested: list[int] = []

    async def fetch_page(**kwargs):
        requested.append(kwargs["page"])
        return pages.get(kwargs["page"], END)

    crawler.zhihu_client.get_note_by_keyword.side_effect = fetch_page
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


# ---------- #113：来源耗尽只认原始 paging.is_end ----------


class SearchHttp:
    """按页返回固定 search_v3 原始响应的 httpx 替身；记录请求路径与 offset。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kwargs):
        parsed = httpx.URL(url)
        offset = int(parsed.params.get("offset", "0"))
        self.calls.append((parsed.path, offset))
        status, payload = self.responses.pop(0)
        request = httpx.Request(method, url)
        if isinstance(payload, str):
            return httpx.Response(status, request=request, text=payload)
        return httpx.Response(status, request=request, json=payload)


def search_client_for(http: SearchHttp) -> ZhiHuClient:
    return ZhiHuClient(
        ports=ZhihuClientPorts(http, AsyncMock()),
        headers={"cookie": "d_c0=t13", "user-agent": "t13"},
        playwright_page=MagicMock(),
        cookie_dict={"d_c0": "t13"},
    )


def raw_answer(identity: str) -> dict:
    return {"type": "search_result", "object": {
        "id": identity, "type": "answer", "question": {"id": "10"}, "created_time": 1_700_000_000,
        "content": f'<p>青岛正文</p><img src="https://pic1.zhimg.com/v2-{identity}_r.jpg">',
        "author": {"id": f"author-{identity}", "name": "作者", "url_token": "t", "follower_count": 3,
                   "avatar_url": "https://pic1.zhimg.com/v2-avatar-never.jpg"},
    }}


FILTERED_ONLY = {"data": [
    {"type": "relevant_query", "object": {"type": "answer", "id": "ignored-1"}},
    {"type": "knowledge_ad", "object": {"type": "article", "id": "ignored-2"}},
    {"type": "search_result", "object": {"type": "question", "id": "ignored-3"}},
    {"type": "zvideo", "object": {"type": "zvideo", "id": "video-1", "video_url": "https://v.test/1"}},
], "paging": {"is_end": False, "next": "https://www.zhihu.com/api/v4/search_v3?offset=20"}}


def observed_search_content(item):
    item.content_detail_status = "detail_observed"
    item.content_detail_source = "search_content"
    return item


def prepare_real_client(monkeypatch, tmp_path, responses, sleeps):
    crawler, state_path = prepare(monkeypatch, tmp_path)
    http = SearchHttp(responses)
    crawler.zhihu_client = search_client_for(http)
    monkeypatch.setattr("trippostcollect.platforms.zhihu.client.sign",
                        lambda uri, cookie: {"x-zst-81": "3_2.0", "x-zse-96": "2.0_t13"})
    crawler.enrich_search_content_detail = AsyncMock(side_effect=observed_search_content)
    crawler.get_content_images = AsyncMock(return_value=None)
    return crawler, state_path, http


@pytest.mark.asyncio
async def test_filtered_only_page_with_is_end_false_continues_to_next_page(monkeypatch, tmp_path, sleeps):
    # 整页都是被过滤的类型（含 zvideo）且 is_end=false：照常完成批次并翻页，不能当作耗尽。
    crawler, state_path, http = prepare_real_client(monkeypatch, tmp_path, [
        (200, FILTERED_ONLY),
        (200, {"data": [raw_answer("a2")], "paging": {"is_end": True}}),
    ], sleeps)

    await crawler.search()

    assert http.calls == [("/api/v4/search_v3", 0), ("/api/v4/search_v3", 20)]
    batches = events_of(state_path, "adaptive_batch_completed")
    assert [(b["source_page"], b["raw_batch_count"], b["source_has_more"]) for b in batches] == [
        (1, 4, True), (2, 1, False),
    ]
    # zvideo 计为已处理候选并进入候选记忆，不产生内容记录。
    assert batches[0]["candidate_identities"] == ["video-1"]
    assert [call.args[0].content_id for call in crawler.get_content_images.await_args_list] == ["a2"]
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"], item["source_page"], item["source_has_more"],
             item["raw_batch_count"]) for item in stopped] == [
        ("source_exhausted", "paging_is_end", 2, False, 1)
    ]


@pytest.mark.asyncio
async def test_empty_page_with_is_end_true_is_source_exhaustion(monkeypatch, tmp_path, sleeps):
    crawler, state_path, _ = prepare_real_client(monkeypatch, tmp_path, [
        (200, {"data": [raw_answer("a1")], "paging": {"is_end": False}}),
        (200, {"data": [], "paging": {"is_end": True}}),
    ], sleeps)

    await crawler.search()

    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"], item["source_page"], item["source_has_more"])
            for item in stopped] == [("source_exhausted", "paging_is_end", 2, False)]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"data": []},
    {"data": [], "paging": {"is_end": False}},
    {"data": [raw_answer("a1")]},
    {"paging": {"is_end": True}},
], ids=["empty-no-paging", "empty-is-end-false", "data-no-paging", "paging-no-data"])
async def test_unverifiable_search_response_is_runtime_failure(monkeypatch, tmp_path, sleeps, payload):
    crawler, state_path, http = prepare_real_client(monkeypatch, tmp_path, [(200, payload)], sleeps)

    await crawler.search()

    assert len(http.calls) == 1
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"]) for item in stopped] == [
        ("runtime_failed", "search_response_invalid")
    ]
    assert events_of(state_path, "adaptive_batch_completed") == []


@pytest.mark.asyncio
async def test_search_http_404_is_runtime_failure_without_retry(monkeypatch, tmp_path, sleeps):
    crawler, state_path, http = prepare_real_client(monkeypatch, tmp_path, [(404, {"error": "gone"})], sleeps)

    await crawler.search()

    assert http.calls == [("/api/v4/search_v3", 0)]
    assert sleeps == []
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"]) for item in stopped] == [
        ("runtime_failed", "search_response_invalid")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "code"), [(401, "login_required"), (403, "login_required"),
                                              (429, "rate_limited")])
async def test_search_run_level_status_keeps_code_and_is_not_retried(
    monkeypatch, tmp_path, sleeps, status, code,
):
    crawler, state_path, http = prepare_real_client(monkeypatch, tmp_path, [(status, "blocked")], sleeps)

    await crawler.search()

    assert len(http.calls) == 1
    assert sleeps == []
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"]) for item in stopped] == [("runtime_failed", code)]


@pytest.mark.asyncio
async def test_detail_404_is_candidate_miss_not_run_failure(sleeps):
    http = SearchHttp([(404, "gone")])

    assert await search_client_for(http).get_answer_info("10", "deleted") is None
    assert http.calls == [("/question/10/answer/deleted", 0)]
    assert sleeps == []


def test_search_page_drops_zvideo_and_keeps_raw_paging():
    page = ZhihuExtractor().extract_search_page(FILTERED_ONLY)

    assert page.contents == []
    assert page.video_ids == ["video-1"]
    assert (page.raw_count, page.is_end) == (4, False)


# ---------- #113：日志、头像与图片请求头 ----------


@pytest.mark.asyncio
async def test_search_and_store_logs_never_contain_avatar_or_body(monkeypatch, tmp_path, sleeps, caplog):
    crawler, _, _ = prepare_real_client(monkeypatch, tmp_path, [
        (200, {"data": [raw_answer("a1")], "paging": {"is_end": True}}),
    ], sleeps)
    stored: list[dict] = []

    class Sink:
        async def store_content(self, item):
            stored.append(item)

    from trippostcollect.platforms.zhihu import core as zhihu_core

    async def store(content):
        await zhihu_core.store_zhihu_content(
            content, source_keyword=lambda: "青岛旅游", current_timestamp=lambda: 1,
            content_sink_factory=Sink,
        )

    monkeypatch.setattr(crawler, "_store_content", store)
    caplog.set_level("INFO", logger="MediaCrawler")

    await crawler.search()

    assert [item["content_id"] for item in stored] == ["a1"]
    assert all("avatar_url" not in item for item in stored)
    assert "avatar" not in caplog.text
    assert "青岛正文" not in caplog.text
    assert "id:a1" in caplog.text


@pytest.mark.asyncio
async def test_pong_failure_log_omits_me_response(caplog):
    class Me(SearchHttp):
        async def request(self, method, url, **kwargs):
            return httpx.Response(200, request=httpx.Request(method, url), json={
                "uid": "", "name": "", "avatar_url": "https://pic1.zhimg.com/v2-me-avatar.jpg",
            })

    client = search_client_for(Me([]))
    client.get = AsyncMock(return_value={"uid": "", "avatar_url": "https://pic1.zhimg.com/v2-me-avatar.jpg"})
    caplog.set_level("INFO", logger="MediaCrawler")

    assert await client.pong() is False
    assert "avatar" not in caplog.text
    assert "uid_present=False" in caplog.text


@pytest.mark.asyncio
async def test_request_error_log_omits_response_body(caplog, sleeps):
    http = SearchHttp([(500, '{"avatar_url": "https://pic1.zhimg.com/v2-err-avatar.jpg"}')] * 3)
    caplog.set_level("INFO", logger="MediaCrawler")

    with pytest.raises(Exception):
        await search_client_for(http).request("GET", "https://www.zhihu.com/api/v4/search_v3?offset=0")

    assert "avatar" not in caplog.text
    assert "status=500" in caplog.text
