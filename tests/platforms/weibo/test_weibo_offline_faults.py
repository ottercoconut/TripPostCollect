"""微博离线故障与前沿阶段特征化（T13）：只替换 httpx 传输与浏览器边界，固定现行行为。

- 搜索请求 5 次/3 秒，只对传输层异常与 5xx 重试，重试用尽后原样上抛（reraise）；传输层异常记
  network_unreachable 运行级失败（#129），其余请求失败记 search_request_failed；
  401/403/418/432/429 直接分类为运行级阻断、不重试（#114）；
- 长文详情 3 次/1 秒/reraise，重试用尽后的传输异常按运行级 network_unreachable 停止本轮（#129）；
- 续跑先刷新顶部有限页，再从保存的深层前沿继续。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from support.weibo_adapter import client_ports, crawler as make_crawler, settings
from trippostcollect.platforms.weibo import client as weibo_client
from trippostcollect.platforms.weibo.client import WeiboClient
from trippostcollect.platforms.weibo.models import DataFetchError, PlatformRuntimeError


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

    def __init__(self, pages: dict[int, list[dict]], *, search_fault=False, detail_fault=(),
                 search_status=200, raw_pages=None, detail_status=200):
        self.pages = pages
        self.search_fault = search_fault
        self.detail_fault = set(detail_fault)
        self.search_status = search_status
        self.raw_pages = raw_pages or {}
        self.detail_status = detail_status
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
            if self.search_status != 200:
                # 风控页常带 HTML 与作者资料片段；分类后不应把原文写进异常或日志。
                return httpx.Response(self.search_status, text="<html>avatar_hd=https://avatar.invalid/x.jpg</html>",
                                      request=request)
            if number in self.raw_pages:
                return httpx.Response(200, json={"ok": 1, "data": {"cards": self.raw_pages[number]}},
                                      request=request)
            cards = [{"card_type": 9, "mblog": item} for item in self.pages.get(number, [])]
            if not cards:
                return httpx.Response(200, json={"ok": 0, "msg": "这里还没有内容", "data": {"cards": []}},
                                      request=request)
            return httpx.Response(200, json={"ok": 1, "data": {"cards": cards}}, request=request)
        if parsed.path.startswith("/detail/"):
            note_id = parsed.path.rsplit("/", 1)[1]
            if note_id in self.detail_fault:
                raise httpx.ReadTimeout("离线读取超时", request=request)
            if self.detail_status != 200:
                return httpx.Response(self.detail_status, text="blocked", request=request)
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
async def test_search_transport_error_exhausts_retry_budget_and_reraises(sleeps):
    transport = Transport({}, search_fault=True)

    # reraise=True：重试用尽后抛出原始传输层异常，而不是 RetryError（#114）。
    with pytest.raises(httpx.ConnectError):
        await make_client(transport).get_note_by_keyword(keyword="青岛旅游", page=1)

    assert transport.calls == ["/api/container/getIndex"] * 5
    assert sleeps == [3, 3, 3, 3]


@pytest.mark.asyncio
async def test_search_transport_failure_is_runtime_failed_not_exhaustion(monkeypatch, tmp_path, sleeps):
    transport = Transport({}, search_fault=True)
    crawler, state_path = prepare(monkeypatch, tmp_path, transport)

    with pytest.raises(httpx.ConnectError):
        await crawler.search()

    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"]) for item in stopped] == [
        ("runtime_failed", "network_unreachable")
    ]
    assert stopped[0]["resume_page"] == 1
    assert events_of(state_path, "adaptive_batch_completed") == []
    crawler.update_weibo_note.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [(401, "login_required"), (403, "login_required"), (418, "blocked_or_forbidden"),
     (432, "blocked_or_forbidden"), (429, "rate_limited")],
)
async def test_search_platform_rejection_blocks_run_without_retry(monkeypatch, tmp_path, sleeps, caplog, status, code):
    transport = Transport({}, search_status=status)
    crawler, state_path = prepare(monkeypatch, tmp_path, transport)

    with pytest.raises(PlatformRuntimeError) as raised:
        await crawler.search()

    assert raised.value.code == code
    assert transport.calls == ["/api/container/getIndex"]
    assert sleeps == []
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"]) for item in stopped] == [("runtime_failed", code)]
    # 阻断页原文不进异常或日志（#114 P5）。
    assert "avatar" not in str(raised.value)
    assert "avatar" not in caplog.text


@pytest.mark.asyncio
async def test_search_5xx_is_retried_then_reraised(monkeypatch, tmp_path, sleeps):
    transport = Transport({}, search_status=502)

    with pytest.raises(DataFetchError):
        await make_client(transport).get_note_by_keyword(keyword="青岛旅游", page=1)

    assert transport.calls == ["/api/container/getIndex"] * 5


@pytest.mark.asyncio
async def test_non_json_search_response_is_not_retried_and_logs_no_body(sleeps, caplog):
    class HtmlTransport(Transport):
        async def request(self, method, url, **kwargs):
            self.calls.append(httpx.URL(url).path)
            return httpx.Response(200, text="<html>profile_image_url=https://avatar.invalid/p.jpg</html>",
                                  request=httpx.Request(method, url))

    transport = HtmlTransport({})

    with pytest.raises(DataFetchError) as raised:
        await make_client(transport).get_note_by_keyword(keyword="青岛旅游", page=1)

    # 不再逐次 goto + 刷新 Cookie；业务层错误不重试。
    assert transport.calls == ["/api/container/getIndex"]
    assert "avatar" not in str(raised.value)
    assert "avatar" not in caplog.text
    assert "length=" in caplog.text


@pytest.mark.asyncio
async def test_ok_error_payload_logs_only_summary(sleeps, caplog):
    class ErrorTransport(Transport):
        async def request(self, method, url, **kwargs):
            self.calls.append(httpx.URL(url).path)
            payload = {"ok": 0, "msg": "离线失败", "data": {"user": {"avatar_hd": "https://avatar.invalid/a.jpg"}}}
            return httpx.Response(200, json=payload, request=httpx.Request(method, url))

    with pytest.raises(DataFetchError):
        await make_client(ErrorTransport({})).get_note_by_keyword(keyword="青岛旅游", page=1)

    assert "avatar" not in caplog.text
    assert "ok=0" in caplog.text and "离线失败" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(403, "login_required"), (429, "rate_limited")])
async def test_long_text_detail_rejection_blocks_without_retry(monkeypatch, tmp_path, sleeps, status, code):
    transport = Transport({1: [mblog("long", long=True)]}, detail_status=status)
    crawler, state_path = prepare(monkeypatch, tmp_path, transport)

    await crawler.search()

    assert transport.calls.count("/detail/long") == 1
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert (stopped[0]["stop_reason"], stopped[0]["stop_detail"]) == ("runtime_failed", code)


@pytest.mark.asyncio
async def test_page_without_note_cards_continues_to_next_page(monkeypatch, tmp_path, sleeps):
    # 有卡片但都不是 card_type=9（话题、用户卡、card_group=None）：正常页，继续翻页（#114）。
    raw = {1: [{"card_type": 11, "card_group": None}, {"card_type": 4, "desc": "相关用户"}]}
    transport = Transport({2: [mblog("p2")]}, raw_pages=raw)
    crawler, state_path = prepare(monkeypatch, tmp_path, transport)

    await crawler.search()

    assert transport.search_pages == [1, 2, 3]
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["source_page"]) for item in stopped] == [("source_exhausted", 3)]
    crawler.update_weibo_note.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_no_content_marker_is_exhaustion(monkeypatch, tmp_path, sleeps):
    raw = {1: [{"card_type": 11, "card_group": [{"card_type": 4, "desc": "暂无内容"}]}]}
    transport = Transport({}, raw_pages=raw)
    crawler, state_path = prepare(monkeypatch, tmp_path, transport)

    await crawler.search()

    assert transport.search_pages == [1]
    stopped = events_of(state_path, "adaptive_search_stopped")
    assert [(item["stop_reason"], item["stop_detail"]) for item in stopped] == [("source_exhausted", "empty_page")]


@pytest.mark.asyncio
async def test_unparseable_created_at_skips_candidate_and_continues(monkeypatch, tmp_path, sleeps):
    broken = {**mblog("broken"), "created_at": "not a date"}
    missing = {**mblog("missing")}
    missing.pop("created_at")
    transport = Transport({1: [broken, missing, mblog("ok")]})
    crawler, state_path = prepare(monkeypatch, tmp_path, transport)

    await crawler.search()

    skipped = events_of(state_path, "candidate_skipped")
    assert [(item["identity"], item["error_code"], item["retryable"]) for item in skipped] == [
        ("broken", "created_at_unparseable", False), ("missing", "created_at_unparseable", False),
    ]
    stored = [call.args[0]["mblog"]["id"] for call in crawler.update_weibo_note.await_args_list]
    assert stored == ["ok"]
    assert events_of(state_path, "adaptive_search_stopped")[-1]["stop_reason"] == "source_exhausted"


@pytest.mark.asyncio
async def test_login_flow_rechecks_mobile_login_before_search(monkeypatch):
    crawler = make_crawler(config)
    crawler.browser_context = AsyncMock()
    crawler.context_page = AsyncMock()
    crawler.wb_client = AsyncMock()
    crawler.wb_client.pong.side_effect = [False, False]
    begin = AsyncMock()
    monkeypatch.setattr("trippostcollect.platforms.weibo.core.WeiboLogin.begin", begin)
    monkeypatch.setattr("trippostcollect.platforms.weibo.core.asyncio.sleep", AsyncMock())

    with pytest.raises(PlatformRuntimeError) as raised:
        await crawler.ensure_login()

    assert raised.value.code == "login_required"
    begin.assert_awaited_once()
    crawler.wb_client.update_cookies.assert_awaited_once()
    assert crawler.wb_client.pong.await_count == 2

    crawler.wb_client.pong.side_effect = [False, True]
    await crawler.ensure_login()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(429, "rate_limited"), (432, "blocked_or_forbidden")])
async def test_pong_raises_non_login_blocks_instead_of_relogin(sleeps, status, expected):
    class ConfigTransport(Transport):
        async def request(self, method, url, **kwargs):
            self.calls.append(httpx.URL(url).path)
            return httpx.Response(status, text="blocked", request=httpx.Request(method, url))

    with pytest.raises(PlatformRuntimeError) as raised:
        await make_client(ConfigTransport({})).pong()
    assert raised.value.code == expected


@pytest.mark.asyncio
async def test_pong_login_rejection_returns_false(sleeps):
    class ConfigTransport(Transport):
        async def request(self, method, url, **kwargs):
            return httpx.Response(403, text="", request=httpx.Request(method, url))

    assert await make_client(ConfigTransport({})).pong() is False


@pytest.mark.asyncio
async def test_long_text_detail_transport_error_stops_run_after_three_attempts(
    monkeypatch, tmp_path, sleeps,
):
    transport = Transport({1: [mblog("long", long=True), mblog("short")]}, detail_fault={"long"})
    crawler, state_path = prepare(monkeypatch, tmp_path, transport)

    await crawler.search()

    # #129：重试预算不变；用尽后仍是传输层失败按运行级 network_unreachable 停止，不按候选跳过。
    assert transport.calls.count("/detail/long") == 3
    assert sleeps.count(1) == 2
    assert events_of(state_path, "candidate_skipped") == []
    stopped = events_of(state_path, "adaptive_search_stopped")[-1]
    assert (stopped["stop_reason"], stopped["stop_detail"]) == ("runtime_failed", "network_unreachable")
    assert stopped["resume_page"] == 1
    assert "long" not in stopped.get("candidate_identities", [])
    crawler.update_weibo_note.assert_not_awaited()


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
