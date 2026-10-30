"""#113 知乎登录态：持久 profile 是唯一权威来源，worker 不注入快照 Cookie，/me 复验失败即运行级阻断。"""

from __future__ import annotations

from dataclasses import replace
import json
from unittest.mock import AsyncMock

import pytest

from .support import config
from trippostcollect.platforms.entry import _zhihu_dependencies
from trippostcollect.platforms.zhihu.core import ZhihuCrawler


class Page:
    url = "about:blank"

    def __init__(self, trace):
        self.trace = trace

    def is_closed(self):
        return False

    async def goto(self, url, **kwargs):
        self.url = url
        self.trace.append(("goto", url))

    async def reload(self, **kwargs):
        self.trace.append(("reload",))

    async def wait_for_load_state(self, *args, **kwargs):
        return None


class Context:
    def __init__(self, trace):
        self.trace = trace
        self.page = Page(trace)
        self.pages = [self.page]

    async def new_page(self):
        return self.page

    async def add_cookies(self, cookies):
        self.trace.append(("add_cookies", cookies))

    async def cookies(self, urls=None):
        self.trace.append(("cookies", urls))
        # 只按 URL 过滤得到的 www.zhihu.com 可发 Cookie；其他域名的 Cookie 不应进入 httpx。
        return [{"name": "d_c0", "value": "live"}, {"name": "z_c0", "value": "live-session"}]


class Browser:
    def __init__(self, context):
        self.context = context

    async def launch_and_connect(self, **kwargs):
        return self.context

    async def get_browser_info(self):
        return {}

    async def cleanup(self):
        return None


class Playwright:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def build(monkeypatch, tmp_path, *, pong: bool):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_ZHIHU_INITIAL_SETTLE_SECONDS", "0")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    # 父层仍可能导出快照 Cookie 到 COOKIES；worker 不得再把它写回浏览器。
    monkeypatch.setattr(config, "COOKIES", "d_c0=snapshot;z_c0=snapshot-session")
    monkeypatch.setattr(config, "KEYWORDS", "青岛旅游")
    trace: list = []
    context = Context(trace)
    clients = []

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.default_headers = kwargs["headers"]
            self.pong = AsyncMock(return_value=pong)
            self.update_cookies = AsyncMock()
            self.get_note_by_keyword = AsyncMock(side_effect=AssertionError("pong 失败后不得搜索"))
            clients.append(self)

        def set_search_referer(self, referer):
            self.default_headers["referer"] = referer

    settings, ports = _zhihu_dependencies(config)
    ports = replace(
        ports, async_playwright=Playwright, browser_manager_factory=lambda: Browser(context),
        run_required_human_behavior=AsyncMock(), client_factory=Client,
    )
    crawler = ZhihuCrawler(settings, ports)
    monkeypatch.setattr("trippostcollect.platforms.zhihu.core.asyncio.sleep", AsyncMock())
    return crawler, trace, clients, state_path


@pytest.mark.asyncio
async def test_start_never_injects_snapshot_cookies_and_reads_live_profile(monkeypatch, tmp_path):
    crawler, trace, clients, _ = build(monkeypatch, tmp_path, pong=True)
    crawler.search = AsyncMock()

    await crawler.start()

    assert [item for item in trace if item[0] in {"add_cookies", "reload"}] == []
    assert ("cookies", ["https://www.zhihu.com"]) in trace
    (client,) = clients
    assert client.kwargs["headers"]["cookie"] == "d_c0=live;z_c0=live-session"
    assert client.kwargs["cookie_dict"] == {"d_c0": "live", "z_c0": "live-session"}
    # Referer 使用本轮真实检索词，不再写死 q=python。
    assert client.kwargs["headers"]["referer"] == (
        "https://www.zhihu.com/search?q=%E9%9D%92%E5%B2%9B%E6%97%85%E6%B8%B8&type=content"
    )
    crawler.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_pong_failure_stops_with_login_required_without_reinjection(monkeypatch, tmp_path):
    crawler, trace, clients, state_path = build(monkeypatch, tmp_path, pong=False)

    await crawler.start()

    assert [item for item in trace if item[0] in {"add_cookies", "reload"}] == []
    # 不再导航搜索页、不跑行为阶段、不搜索。
    assert not any(item[0] == "goto" and "/search" in item[1] for item in trace)
    clients[0].get_note_by_keyword.assert_not_awaited()
    clients[0].update_cookies.assert_not_awaited()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event["details"] for event in events if event["type"] == "adaptive_search_stopped"]
    assert [(item["stop_reason"], item["stop_detail"]) for item in stopped] == [
        ("runtime_failed", "login_required")
    ]
