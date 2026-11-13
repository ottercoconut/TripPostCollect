"""#113/#185 知乎登录态：持久 profile 是唯一权威来源，worker 不注入快照 Cookie，页面截获的 /me 复验失败即运行级阻断。"""

from __future__ import annotations

from dataclasses import replace
import json
from unittest.mock import AsyncMock

import pytest

from .support import config
from trippostcollect.platforms.entry import _zhihu_dependencies
from trippostcollect.platforms.zhihu.core import ZhihuCrawler


class MeResponse:
    def __init__(self, page, payload, status=200):
        self.url = "https://www.zhihu.com/api/v4/me?include=email"
        self.status = status
        self.headers = {}
        self.request = type("Request", (), {
            "method": "GET", "resource_type": "fetch", "url": self.url,
            "frame": type("Frame", (), {"page": page})(),
        })()
        self._body = json.dumps(payload).encode()

    async def body(self):
        return self._body


class Page:
    url = "about:blank"

    def __init__(self, trace, me_payload):
        self.trace = trace
        self.me_payload = me_payload
        self.listeners = []

    def is_closed(self):
        return False

    def on(self, event, callback):
        if event == "response":
            self.listeners.append(callback)

    async def goto(self, url, **kwargs):
        self.url = url
        self.trace.append(("goto", url))
        if "zhihu.com" in url and "/search" not in url:
            response = MeResponse(self, self.me_payload)
            for callback in list(self.listeners):
                callback(response)

    async def reload(self, **kwargs):
        self.trace.append(("reload",))

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def wait_for_timeout(self, timeout):
        return None

    async def evaluate(self, script, arg=None):
        assert script == "() => navigator.userAgent", script
        return "Mozilla/5.0 test-browser"


class Context:
    def __init__(self, trace, me_payload):
        self.trace = trace
        self.page = Page(trace, me_payload)
        self.page.context = self
        self.pages = [self.page]

    def on(self, event, callback):
        assert event == "page"

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


def build(monkeypatch, tmp_path, *, logged_in: bool):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events": []}', encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_ZHIHU_INITIAL_SETTLE_SECONDS", "0")
    monkeypatch.setenv("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", str(tmp_path / "behavior.json"))
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    # 父层仍可能导出快照 Cookie 到 COOKIES；worker 不得再把它写回浏览器。
    monkeypatch.setattr(config, "COOKIES", "d_c0=snapshot;z_c0=snapshot-session")
    monkeypatch.setattr(config, "KEYWORDS", "青岛旅游")
    trace: list = []
    me_payload = {"uid": "author", "name": "作者"} if logged_in else {"uid": "", "name": ""}
    context = Context(trace, me_payload)
    clients = []

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.default_headers = kwargs["headers"]
            self.update_cookies = AsyncMock()
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
    crawler, trace, clients, _ = build(monkeypatch, tmp_path, logged_in=True)
    crawler.search = AsyncMock()

    await crawler.start()

    assert [item for item in trace if item[0] in {"add_cookies", "reload", "cookies"}] == []
    (client,) = clients
    assert client.kwargs["headers"] == {"user-agent": "Mozilla/5.0 test-browser"}
    assert "cookie" not in client.kwargs
    crawler.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_me_capture_failure_stops_with_login_required_without_reinjection(monkeypatch, tmp_path):
    crawler, trace, clients, state_path = build(monkeypatch, tmp_path, logged_in=False)

    await crawler.start()

    assert [item for item in trace if item[0] in {"add_cookies", "reload"}] == []
    # 不再导航搜索页、不跑行为阶段、不搜索。
    assert not any(item[0] == "goto" and "/search" in item[1] for item in trace)
    clients[0].update_cookies.assert_not_awaited()
    events = json.loads(state_path.read_text(encoding="utf-8"))["events"]
    stopped = [event["details"] for event in events if event["type"] == "adaptive_search_stopped"]
    assert [(item["stop_reason"], item["stop_detail"]) for item in stopped] == [
        ("runtime_failed", "login_required")
    ]
