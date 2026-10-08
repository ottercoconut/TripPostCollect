"""微博离线故障与前沿阶段特征化（T13）：只替换 httpx 传输与浏览器边界，固定现行行为。

- 搜索请求沿用原 Tenacity 5 次/3 秒，传输层异常最终为 RetryError，运行失败且不宣称耗尽；
- 长文详情 3 次/1 秒/reraise，传输异常转为非阻断的候选 skip；
- 续跑先刷新顶部有限页，再从保存的深层前沿继续。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from tenacity import RetryError

from support.weibo_adapter import client_ports, crawler as make_crawler, settings
from trippostcollect.platforms.weibo import client as weibo_client
from trippostcollect.platforms.weibo.client import WeiboClient


config = settings()


def mblog(note_id: str, *, long: bool = False) -> dict:
    return {
        "id": note_id,
        "text": "青岛正文",
        "isLongText": long,
        "created_at": "Sat Jun 14 12:00:00 +0800 2025",
        "attitudes_count": 1,
        "comments_count": 2,
        "reposts_count": 3,
        "pics": [{"pid": f"pid-{note_id}", "url": f"https://wx.test/{note_id}.jpg"}],
        "user": {"id": "author", "screen_name": "name", "followers_count": 4},
    }


class Transport:
    """按路径路由的 httpx 替身；故障路径抛传输层异常，未登记路径直接失败。"""

    def __init__(self, pages: dict[int, list[dict]], *, search_fault=False, detail_fault=()):
        self.pages = pages
        self.search_fault = search_fault
        self.detail_fault = set(detail_fault)
        self.calls: list[str] = []
        self.search_pages: list[int] = []

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kwargs):
        parsed = httpx.URL(url)
        request = httpx.Request(method, url)
        self.calls.append(parsed.path)
        if parsed.path == "/api/container/getIndex":
            number = int(parsed.params["page"])
            self.search_pages.append(number)
            if self.search_fault:
                raise httpx.ConnectError("离线网络中断", request=request)
            cards = [{"card_type": 9, "mblog": item} for item in self.pages.get(number, [])]
            if not cards:
                return httpx.Response(200, json={"ok": 0, "msg": "这里还没有内容", "data": {"cards": []}},
                                      request=request)
            return httpx.Response(200, json={"ok": 1, "data": {"cards": cards}}, request=request)
        if parsed.path.startswith("/detail/"):
            note_id = parsed.path.rsplit("/", 1)[1]
            if note_id in self.detail_fault:
                raise httpx.ReadTimeout("离线读取超时", request=request)
            body = json.dumps([{"status": {**mblog(note_id), "text": "青岛长文完整正文"}}])
            return httpx.Response(200, text=f"var $render_data = {body}[0]", request=request)
        raise AssertionError(f"未登记的请求：{method} {url}")


@pytest.fixture
def sleeps(monkeypatch):
    recorded: list[float] = []

    async def sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr(WeiboClient.request.retry, "sleep", sleep)
    monkeypatch.setattr(WeiboClient._get_note_info_direct.retry, "sleep", sleep)
    monkeypatch.setattr(weibo_client.asyncio, "sleep", AsyncMock(return_value=None))
    return recorded


def make_client(transport: Transport) -> WeiboClient:
    return WeiboClient(
        headers={"User-Agent": "t13", "Cookie": ""}, playwright_page=None, cookie_dict={},
        ports=client_ports(make_async_client=transport),
    )


def prepare(monkeypatch, tmp_path, transport: Transport, *, start_page=1, refresh=0):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", str(refresh))
    monkeypatch.setenv("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setattr(config, "START_PAGE", start_page)
    monkeypatch.setattr(config, "KEYWORDS", "青岛旅游")
    monkeypatch.setattr(config, "WEIBO_SEARCH_TYPE", "default")
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "ENABLE_WEIBO_FULL_TEXT", True)
    crawler = make_crawler(config)
    crawler.wb_client = make_client(transport)
    crawler.get_note_images = AsyncMock(return_value=None)
    monkeypatch.setattr(crawler, "update_weibo_note", AsyncMock(return_value=None))
    return crawler, state_path


def events_of(state_path, kind):
    return [event["details"] for event in json.loads(state_path.read_text(encoding="utf-8"))["events"]
            if event["type"] == kind]


@pytest.mark.asyncio
async def test_search_transport_error_exhausts_original_retry_budget(sleeps):
    transport = Transport({}, search_fault=True)

    with pytest.raises(RetryError) as raised:
        await make_client(transport).get_note_by_keyword(keyword="青岛旅游", page=1)

    assert transport.calls == ["/api/container/getIndex"] * 5
    assert sleeps == [3, 3, 3, 3]
    assert isinstance(raised.value.last_attempt.exception(), httpx.ConnectError)


@pytest.mark.asyncio
async def test_search_transport_failure_propagates_without_exhaustion(monkeypatch, tmp_path, sleeps):
    transport = Transport({}, search_fault=True)
    crawler, state_path = prepare(monkeypatch, tmp_path, transport)

    with pytest.raises(RetryError):
        await crawler.search()

    # 只有 DataFetchError 被映射为 search_request_failed；传输层 RetryError 原样上抛，由父进程判运行失败。
    assert events_of(state_path, "adaptive_search_stopped") == []
    assert events_of(state_path, "adaptive_batch_completed") == []
    crawler.update_weibo_note.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_text_detail_transport_error_skips_candidate_after_three_attempts(
    monkeypatch, tmp_path, sleeps,
):
    transport = Transport({1: [mblog("long", long=True), mblog("short")]}, detail_fault={"long"})
    crawler, state_path = prepare(monkeypatch, tmp_path, transport)

    await crawler.search()

    assert transport.calls.count("/detail/long") == 3
    assert sleeps.count(1) == 2
    skipped = events_of(state_path, "candidate_skipped")
    assert [(item["identity"], item["detail"], item["error_code"], item["attempts"]) for item in skipped] == [
        ("long", "full_text_request_failed", "unexpected_detail_failure", 3)
    ]
    stopped = events_of(state_path, "adaptive_search_stopped")[-1]
    assert stopped["stop_reason"] == "source_exhausted"
    assert stopped["resume_page"] == 2
    crawler.update_weibo_note.assert_awaited_once()


@pytest.mark.asyncio
async def test_top_refresh_precedes_saved_frontier(monkeypatch, tmp_path, sleeps):
    transport = Transport({1: [mblog("r1")], 5: [mblog("f5")]})
    crawler, state_path = prepare(monkeypatch, tmp_path, transport, start_page=5, refresh=2)

    await crawler.search()

    # 刷新只到 min(start_page-1, 2) 页，刷新空页不宣称耗尽，随后从保存的前沿继续。
    assert transport.search_pages == [1, 2, 5, 6]
    batches = [(item["source_page"], item["discovery_phase"]) for item in events_of(state_path, "adaptive_batch_completed")]
    assert batches == [(1, "refresh"), (5, "frontier")]
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert len(stopped) == 1
    assert stopped[0]["stop_reason"] == "source_exhausted"
    assert stopped[0]["discovery_phase"] == "frontier"
    assert stopped[0]["source_page"] == stopped[0]["resume_page"] == 6
