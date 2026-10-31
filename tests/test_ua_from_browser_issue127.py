"""#127 第 1 项：浏览器外请求的 UA 与浏览器自报的 navigator.userAgent 一致，不再写死或随机。

#136：微博标准模式持久 context 与 B站行为会话改由 ``runtime.browser_runtime.launch_profile_context`` 启动，
与 warmup 同一套参数，kwargs 中不再出现 ``user_agent`` 键；对应断言随之改为“无 user_agent 键”。
#139：共享函数不再传 ``viewport``，改为 ``no_viewport=True`` 加 ``--window-size=1450,900``；warmup 与微博标准模式
的视口断言随之改为原生窗口，知乎两条路径与微博非持久路径仍用 ``PROFILE_VIEWPORT``。
全部离线：假 page/context/playwright，不访问网络、不启动浏览器。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
WARMUP_VIEWPORT = {"width": 1280, "height": 900}
# #139：warmup 与共享持久 context 改按原生窗口启动，``WARMUP_VIEWPORT`` 只剩知乎与微博非持久路径对照。
NATIVE_WINDOW_ARG = "--window-size=1450,900"


class FakePage:
    """只回答 navigator.userAgent；其他脚本一律不应出现。"""

    def __init__(self, user_agent: str = BROWSER_UA) -> None:
        self.user_agent = user_agent
        self.scripts: list[str] = []

    async def evaluate(self, script, *args):
        self.scripts.append(script)
        assert "navigator.userAgent" in script, script
        return self.user_agent


class FakeChromium:
    def __init__(self) -> None:
        self.persistent: list[dict[str, Any]] = []
        self.contexts: list[dict[str, Any]] = []

    async def launch_persistent_context(self, **kwargs):
        self.persistent.append(kwargs)
        return "persistent-context"

    async def launch(self, **kwargs):
        chromium = self

        class Browser:
            async def new_context(self, **kwargs):
                chromium.contexts.append(kwargs)
                return "context"

        return Browser()


async def _no_cookies(context, urls=None):
    return "SUB=t127", {"SUB": "t127"}


# ---------- 1. 知乎、微博：浏览器不强制 UA，视口与 warmup 相同 ----------


def _zhihu_crawler(save_login_state: bool):
    from trippostcollect.platforms.zhihu import core

    crawler = object.__new__(core.ZhihuCrawler)
    crawler.settings = SimpleNamespace(SAVE_LOGIN_STATE=save_login_state)
    crawler.ports = SimpleNamespace(project_browser_args=lambda: [])
    return crawler


def _shared_launch(monkeypatch):
    """#136：共享启动函数照真实实现执行，运行时参数与环境取替身值，不在仓库下建目录。"""
    from trippostcollect.runtime import browser_runtime

    monkeypatch.setattr(browser_runtime, "browser_runtime_args", lambda: [])
    monkeypatch.setattr(browser_runtime, "browser_launch_environment", lambda: {})
    return browser_runtime.launch_profile_context


def _weibo_crawler(save_login_state: bool, monkeypatch):
    from trippostcollect.platforms.weibo import core

    crawler = object.__new__(core.WeiboCrawler)
    crawler.config = SimpleNamespace(SAVE_LOGIN_STATE=save_login_state)
    crawler.ports = SimpleNamespace(project_browser_args=lambda: [], launch_profile_context=_shared_launch(monkeypatch))
    return crawler


@pytest.mark.parametrize("platform", ["zhihu", "weibo"])
def test_crawler_start_does_not_force_user_agent(platform, monkeypatch) -> None:
    """start() 交给浏览器启动的 user_agent 为 None（标准与 CDP 两条路径）。"""
    from trippostcollect.platforms.weibo import core as weibo_core
    from trippostcollect.platforms.zhihu import core as zhihu_core

    module = zhihu_core if platform == "zhihu" else weibo_core
    source = Path(module.__file__).read_text(encoding="utf-8")
    start = re.search(r"    async def start\(self\).*?(?=\n    async def |\n    def )", source, re.S).group(0)
    for call in ("launch_browser_with_cdp(", "launch_browser("):
        # 每处调用的第三个位置参数（user_agent）都必须是 None。
        for match in re.finditer(re.escape("self." + call), start):
            segment = start[match.end(): match.end() + 200]
            args = [part.strip() for part in segment.split(")", 1)[0].split(",")]
            assert args[2] == "None", (platform, call, args)
    assert not hasattr(module, "get_mobile_user_agent")
    assert not hasattr(module, "get_user_agent")


@pytest.mark.parametrize("platform", ["zhihu", "weibo"])
@pytest.mark.parametrize("save_login_state", [True, False])
def test_crawler_viewport_matches_warmup(platform, save_login_state, monkeypatch) -> None:
    from trippostcollect.core.paths import PROFILE_VIEWPORT

    if platform == "zhihu":
        crawler = _zhihu_crawler(save_login_state)
    else:
        crawler = _weibo_crawler(save_login_state, monkeypatch)
    chromium = FakeChromium()
    asyncio.run(type(crawler).launch_browser(crawler, chromium, None, None, True))
    launched = (chromium.persistent or chromium.contexts)[0]
    if platform == "weibo" and save_login_state:
        # #136：微博标准模式持久 context 走共享启动配置，不传 user_agent 键。
        # #139：共享配置与 warmup 一样按原生窗口启动，不传 viewport。
        assert "user_agent" not in launched
        assert "viewport" not in launched and launched["no_viewport"] is True
        assert launched["args"][-1] == NATIVE_WINDOW_ARG
    else:
        assert launched["viewport"] == WARMUP_VIEWPORT == PROFILE_VIEWPORT
        assert launched["user_agent"] is None


def test_warmup_uses_shared_native_window(monkeypatch, tmp_path) -> None:
    """#139：warmup 不做设备模拟，按与小红书相同的原生窗口启动。"""
    from trippostcollect.application import warmup

    monkeypatch.setattr(warmup, "browser_runtime_args", lambda: [])
    monkeypatch.setattr(warmup, "browser_launch_environment", lambda: {})
    chromium = FakeChromium()
    asyncio.run(warmup.launch_login_context(SimpleNamespace(chromium=chromium), tmp_path, None))
    assert "viewport" not in chromium.persistent[0] and chromium.persistent[0]["no_viewport"] is True
    assert chromium.persistent[0]["args"][-1] == NATIVE_WINDOW_ARG
    assert "user_agent" not in chromium.persistent[0]


def test_zhihu_client_headers_use_browser_user_agent() -> None:
    from trippostcollect.platforms.zhihu import core

    crawler = object.__new__(core.ZhihuCrawler)
    crawler.index_url = "https://www.zhihu.com"
    crawler.cookie_urls = [crawler.index_url]
    crawler.browser_context = object()
    crawler.context_page = FakePage()
    created: list[dict[str, Any]] = []
    crawler.ports = SimpleNamespace(
        convert_browser_context_cookies=_no_cookies,
        client_factory=lambda **kwargs: created.append(kwargs) or SimpleNamespace(**kwargs),
    )
    asyncio.run(crawler.create_zhihu_client(None, referer_keyword="青岛旅游"))
    assert created[0]["headers"]["user-agent"] == BROWSER_UA
    assert crawler.context_page.scripts


def test_weibo_client_headers_use_browser_user_agent(monkeypatch) -> None:
    from trippostcollect.platforms.weibo import core

    crawler = object.__new__(core.WeiboCrawler)
    crawler.cookie_urls = ["https://m.weibo.cn"]
    crawler.browser_context = object()
    crawler.context_page = FakePage()
    crawler.ports = SimpleNamespace(
        client=SimpleNamespace(convert_browser_context_cookies=_no_cookies),
        post_repair=False,
    )
    client = asyncio.run(crawler.create_weibo_client(None))
    assert client.headers["User-Agent"] == BROWSER_UA
    assert crawler.context_page.scripts
    assert not hasattr(crawler, "user_agent")
    assert not hasattr(crawler, "mobile_user_agent")


# ---------- 2. 微博：图片请求 UA 等于 client 头 UA ----------


def test_weibo_image_request_uses_client_user_agent(monkeypatch) -> None:
    import httpx

    from trippostcollect.platforms.weibo import core

    crawler = object.__new__(core.WeiboCrawler)
    crawler.cookie_urls = ["https://m.weibo.cn"]
    crawler.browser_context = object()
    crawler.context_page = FakePage()
    requests: list[dict[str, Any]] = []

    class Http:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            requests.append(kwargs)
            return httpx.Response(200, content=b"image", request=httpx.Request(method, url))

    crawler.ports = SimpleNamespace(
        client=SimpleNamespace(
            convert_browser_context_cookies=_no_cookies,
            make_async_client=lambda **kwargs: Http(),
            image_max_bytes=1024,
        ),
        post_repair=False,
    )
    client = asyncio.run(crawler.create_weibo_client(None))
    assert asyncio.run(client.get_note_image("https://wx1.sinaimg.cn/large/abc.jpg")) == b"image"
    assert requests[0]["headers"]["User-Agent"] == client.headers["User-Agent"] == BROWSER_UA


# ---------- 3. warmup：Cookie 快照保存登录 page 的 navigator.userAgent ----------


def test_warmup_snapshot_records_login_page_user_agent(monkeypatch, tmp_path) -> None:
    from trippostcollect.application import warmup
    from trippostcollect.runtime import cookies

    target = tmp_path / "bilibili" / "snapshot.json"
    monkeypatch.setattr(warmup, "cookie_snapshot_path", lambda key: target)
    monkeypatch.setattr(cookies, "cookie_snapshot_path", lambda key: target)
    info = warmup.write_cookie_snapshot(
        "bilibili", [{"name": "SESSDATA", "value": "s"}], source="session", state={},
        user_agent=BROWSER_UA,
    )
    assert info["path"] == str(target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["user_agent"] == BROWSER_UA
    assert cookies.load_cookie_snapshot("bilibili")["user_agent"] == BROWSER_UA


@pytest.mark.parametrize("scenario", ["session", "skip", "reopen"])
def test_warmup_one_passes_page_user_agent_to_snapshot(monkeypatch, tmp_path, scenario) -> None:
    """warmup_one 每条写快照路径都把当时登录 page 的 navigator.userAgent 存进快照。"""
    from trippostcollect.application import warmup

    written: list[dict[str, Any]] = []
    agents = iter(["UA-session/1", "UA-reopen/2"])

    class Page:
        url = "about:blank"

        def __init__(self) -> None:
            self.user_agent = next(agents)

        async def goto(self, url, **kwargs):
            self.url = url

        async def wait_for_timeout(self, ms):
            if ms == 60_000:
                raise KeyboardInterrupt

        async def evaluate(self, script, *args):
            if "navigator.userAgent" in script:
                return self.user_agent
            raise AssertionError(script)

    class Context:
        def __init__(self) -> None:
            self.pages = [Page()]

        async def cookies(self, urls=None):
            return [{"name": "SESSDATA", "value": "s"}]

        async def close(self):
            return None

    async def launch(playwright, profile_dir, browser_path, **kwargs):
        return Context()

    async def state(context, page, platform_key):
        return {"ok": True, "markers": {}}

    def write(platform_key, cookie_rows, *, source, state, user_agent):
        written.append({"source": source, "user_agent": user_agent})
        return {"path": "snapshot"}

    monkeypatch.setattr(warmup, "launch_login_context", launch)
    monkeypatch.setattr(warmup, "current_state", state)
    monkeypatch.setattr(warmup, "write_cookie_snapshot", write)
    monkeypatch.setattr(warmup, "profile_dir_for", lambda key: tmp_path / "profile")
    monkeypatch.setattr(warmup, "browser_path_for", lambda args: None)
    args = SimpleNamespace(
        timeout_seconds=1, no_close_on_success=scenario == "session", skip_reopen_verify=scenario == "skip",
    )
    asyncio.run(warmup.warmup_one(None, "bilibili", tmp_path, args))
    expected = {
        "session": [{"source": "session", "user_agent": "UA-session/1"}],
        "skip": [{"source": "session_skip_reopen_verify", "user_agent": "UA-session/1"}],
        "reopen": [{"source": "reopen_verify", "user_agent": "UA-reopen/2"}],
    }[scenario]
    assert written == expected


# ---------- 4. B站：行为会话的 UA 用于五类请求；缺失时不发请求 ----------


class _Captured:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []


def _patch_urlopen(monkeypatch, captured: _Captured, payloads: dict[str, Any]) -> None:
    import io

    from trippostcollect.platforms.bilibili import client

    def urlopen(request, timeout):
        headers = {name.lower(): value for name, value in request.header_items()}
        captured.requests.append((request.full_url, headers))
        for path, payload in payloads.items():
            if path in request.full_url:
                return io.BytesIO(json.dumps(payload).encode())
        raise AssertionError(request.full_url)

    monkeypatch.setattr(client, "urlopen", urlopen)


def test_bilibili_api_requests_use_given_user_agent(monkeypatch) -> None:
    from trippostcollect.platforms.bilibili import client

    captured = _Captured()
    _patch_urlopen(monkeypatch, captured, {
        "/x/web-interface/nav": {"code": 0, "data": {"isLogin": True, "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png"}}},
        "/x/web-interface/wbi/search/type": {"code": 0, "data": {"result": [], "numPages": 0, "numResults": 0}},
        "/x/article/view": {"code": 0, "data": {"content": "<p>青岛正文</p>"}},
        "/x/relation/stat": {"code": 0, "data": {"follower": 1}},
    })
    keys = client.fetch_bilibili_wbi_keys("SESSDATA=s", user_agent=BROWSER_UA)
    client.fetch_bilibili_article_page("青岛", 1, wbi_keys=keys, cookie_header="SESSDATA=s", user_agent=BROWSER_UA)
    client.fetch_bilibili_article_detail_with_retry("1", "SESSDATA=s", user_agent=BROWSER_UA)
    client.fetch_bilibili_follower_count("2", "SESSDATA=s", user_agent=BROWSER_UA)
    assert len(captured.requests) == 4
    assert all(headers["user-agent"] == BROWSER_UA for _, headers in captured.requests)
    assert client.bilibili_image_headers("1", user_agent=BROWSER_UA)["User-Agent"] == BROWSER_UA
    assert client.bilibili_detail_headers("1", "", user_agent=BROWSER_UA)["User-Agent"] == BROWSER_UA


def test_bilibili_image_fetch_uses_given_user_agent() -> None:
    from trippostcollect.platforms.bilibili import client

    seen: list[dict[str, str]] = []

    def fetch_remote(url, **kwargs):
        seen.append(kwargs["headers"])
        return "preview"

    ports = SimpleNamespace(
        fetch_remote_image_bytes=fetch_remote, DEFAULT_ARCHIVE_IMAGE_MAX_BYTES=1, SUPPORTED_IMAGE_MIME_TYPES=(),
    )
    assert client.fetch_bilibili_image_bytes(
        "https://i0.hdslb.com/bfs/article/a.png", "1", user_agent=BROWSER_UA, ports=ports,
    ) == "preview"
    assert seen[0]["User-Agent"] == BROWSER_UA


def test_bilibili_behavior_session_exports_page_user_agent(monkeypatch, tmp_path) -> None:
    from trippostcollect.platforms.bilibili import login

    page = FakePage()

    class Context:
        pages = [page]

        async def add_init_script(self, **kwargs):
            return None

        async def route(self, pattern, handler):
            return None

        async def cookies(self, urls):
            return [{"name": "SESSDATA", "value": "s"}]

        async def close(self):
            return None

    async def goto(url, **kwargs):
        return None

    page.goto = goto

    class Playwright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=self)

        async def __aexit__(self, *args):
            return False

        async def launch_persistent_context(self, **kwargs):
            return Context()

    async def hints(context):
        return None

    async def behavior(page, **kwargs):
        return {"finished_at": "2026-10-30T00:00:00+00:00"}

    from trippostcollect.runtime import cookies

    ports = SimpleNamespace(
        discover_cdp_browser_path=lambda: None, profile_dir_for=lambda key: tmp_path / "profile",
        async_playwright=Playwright, browser_runtime_args=lambda: [], browser_launch_environment=lambda: {},
        launch_profile_context=_shared_launch(monkeypatch),
        install_runtime_hints=hints, run_page_behavior=behavior, write_evidence=lambda *args: None,
        platform_cookie_url=cookies.platform_cookie_url, cookies_to_header=cookies.cookies_to_header,
        cookie_snapshot_path=lambda key: tmp_path / "snapshot.json",
        cookie_names_from_header=cookies.cookie_names_from_header,
        required_cookie_names=cookies.required_cookie_names,
    )
    export, _ = asyncio.run(login.run_bilibili_behavior_session(
        SimpleNamespace(keyword="青岛", headed=False), tmp_path / "evidence.json", ports=ports,
    ))
    assert export["user_agent"] == BROWSER_UA
    assert page.scripts


def _bilibili_search_args(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        keyword="青岛旅游", db=str(tmp_path / "missing.sqlite"), headed=False, start_page=1,
        top_refresh_max_pages=0, discovery_source_exhausted=False, discovery_job_id=None,
        discovery_query_fingerprint="", resume_identities_path=None, download_images=True,
    )


def _install_bilibili_search(monkeypatch, export: dict[str, Any], calls: list[tuple[str, Any]]):
    from unittest.mock import AsyncMock

    import mediacrawler_crawl
    from support.bilibili_search import paged_search
    from trippostcollect.platforms.bilibili import core

    monkeypatch.setattr(
        mediacrawler_crawl, "run_bilibili_behavior_session",
        AsyncMock(return_value=(export, {"ok": True})),
    )
    monkeypatch.setattr(mediacrawler_crawl, "behavior_evidence_valid", lambda value: True)
    monkeypatch.setattr(mediacrawler_crawl.time, "sleep", lambda value: None)

    def wbi(cookie_header, **kwargs):
        calls.append(("nav", kwargs.get("user_agent")))
        return ("a" * 32, "b" * 32)

    search = paged_search({1: [{
        "id": 123, "title": "青岛旅游", "desc": "摘要", "pub_time": 1_700_000_000,
        "author": "作者", "mid": 456, "image_urls": [],
    }]})

    def page(keyword, number, **kwargs):
        calls.append(("search", kwargs.get("user_agent")))
        return search(keyword, number, **kwargs)

    def detail(post_id, cookie_header, **kwargs):
        calls.append(("detail", kwargs.get("user_agent")))
        return ({
            "title": "青岛详情", "content": '<p>青岛完整正文</p><img src="https://i0.hdslb.com/bfs/article/a.png">',
            "opus": {"content": {"paragraphs": []}}, "content_pic_list": [],
        }, 1, 0.0)

    def follower(creator_id, cookie_header, **kwargs):
        calls.append(("relation", kwargs.get("user_agent")))
        return 42

    monkeypatch.setattr(core, "fetch_bilibili_wbi_keys", wbi)
    monkeypatch.setattr(core, "fetch_bilibili_article_page", page)
    monkeypatch.setattr(core, "fetch_bilibili_article_detail_with_retry", detail)
    monkeypatch.setattr(core, "fetch_bilibili_follower_count", follower)

    def image(source_url, post_id, **kwargs):
        calls.append(("image", kwargs.get("user_agent")))
        raise RuntimeError("离线：图片阶段到此为止")

    monkeypatch.setattr(mediacrawler_crawl, "fetch_bilibili_image_bytes", image)
    return mediacrawler_crawl


def test_bilibili_search_threads_session_user_agent_to_all_requests(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, Any]] = []
    crawl = _install_bilibili_search(monkeypatch, {"cookie_header": "SESSDATA=s", "user_agent": BROWSER_UA}, calls)
    crawl.run_bilibili_article_search(_bilibili_search_args(tmp_path), tmp_path / "batch")
    kinds = {kind for kind, _ in calls}
    assert {"nav", "search", "detail", "image"} <= kinds, calls
    assert all(agent == BROWSER_UA for _, agent in calls), calls


@pytest.mark.parametrize("export", [{"cookie_header": "SESSDATA=s"}, {"cookie_header": "SESSDATA=s", "user_agent": ""}])
def test_bilibili_search_without_user_agent_stops_before_any_request(monkeypatch, tmp_path, export) -> None:
    calls: list[tuple[str, Any]] = []
    state = tmp_path / "state.json"
    from trippostcollect.core.execution_state import FrozenExecutionState

    FrozenExecutionState.create(
        state, run_id="t127", job_key="bilibili", site_key="bilibili",
        job_kind="mediacrawler_search", plan={}, frozen_inputs=[],
    )
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state))
    crawl = _install_bilibili_search(monkeypatch, export, calls)
    result = crawl.run_bilibili_article_search(_bilibili_search_args(tmp_path), tmp_path / "batch")
    assert calls == []
    assert result["run"]["returncode"] == 1
    stopped = [event["details"] for event in json.loads(state.read_text())["events"]
               if event["type"] == "adaptive_search_stopped"]
    assert stopped[-1]["stop_reason"] == "runtime_failed"
    assert stopped[-1]["stop_detail"] == "cookie_export_missing_user_agent"


def test_bilibili_repair_requires_snapshot_user_agent(monkeypatch) -> None:
    import repair_bilibili_articles as repair

    calls: list[str] = []
    monkeypatch.setattr(repair, "check_bilibili_login", lambda *a, **k: calls.append("login"))
    snapshot = {"cookie_header": "SESSDATA=s"}
    with pytest.raises(repair.BilibiliRepairLoginRequiredError, match="cookie_snapshot_missing_user_agent"):
        repair.snapshot_session(snapshot)
    assert repair.snapshot_session({**snapshot, "user_agent": BROWSER_UA}) == ("SESSDATA=s", BROWSER_UA)
    assert calls == []


def test_bilibili_repair_stops_when_snapshot_lacks_user_agent(monkeypatch, tmp_path) -> None:
    import repair_bilibili_articles as repair
    from test_bilibili_article_repair import config_for, create_target

    target = tmp_path / "target.sqlite"
    create_target(target, count=1)
    baseline = repair.sha256_file(target)
    backup = tmp_path / "backup.sqlite"
    repair.create_online_backup(target, backup)
    config = config_for(
        target, tmp_path / "state.sqlite", backup, tmp_path / "reports", apply=True, expected_sha256=baseline,
    )
    requests: list[str] = []
    monkeypatch.setattr(repair, "load_cookie_snapshot", lambda platform: {"cookie_header": "SESSDATA=s"})
    monkeypatch.setattr(repair, "check_bilibili_login", lambda *a, **k: requests.append("login"))
    monkeypatch.setattr(repair, "fetch_repair_article_detail", lambda *a, **k: requests.append("detail"))
    return_code, result = repair.run_repair(config)
    assert requests == []
    assert return_code == 4
    assert result["stopped_reason"] == "login_required"
    assert "cookie_snapshot_missing_user_agent" in result["login_check"]["error"]


def test_bilibili_repair_requests_use_snapshot_user_agent(monkeypatch) -> None:
    import io

    import repair_bilibili_articles as repair

    seen: list[dict[str, str]] = []

    def urlopen(request, timeout):
        seen.append({name.lower(): value for name, value in request.header_items()})
        if "/x/web-interface/nav" in request.full_url:
            return io.BytesIO(json.dumps({"code": 0, "data": {"isLogin": True}}).encode())
        return io.BytesIO(json.dumps({"code": 0, "data": {"content": "<p>青岛正文</p>"}}).encode())

    from trippostcollect.platforms.bilibili import client

    monkeypatch.setattr(repair, "urlopen", urlopen)
    monkeypatch.setattr(client, "urlopen", urlopen)
    repair.check_bilibili_login("SESSDATA=s", user_agent=BROWSER_UA)
    repair.fetch_repair_article_detail("1", "SESSDATA=s", user_agent=BROWSER_UA)
    assert [headers["user-agent"] for headers in seen] == [BROWSER_UA, BROWSER_UA]


# ---------- 5. 源码：src/ 下不再写死 Chrome 版本 UA ----------


def test_source_tree_has_no_hard_coded_chrome_user_agent() -> None:
    pattern = re.compile(r"Chrome/1\d\d")
    hits = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{number}")
    assert hits == []


def test_random_and_mobile_user_agent_helpers_are_removed() -> None:
    from trippostcollect.runtime import helpers

    assert not hasattr(helpers, "get_user_agent")
    assert not hasattr(helpers, "get_mobile_user_agent")
    from trippostcollect.platforms.bilibili import client

    assert not hasattr(client, "BILIBILI_BROWSER_USER_AGENT")


def test_login_qrcode_download_uses_page_user_agent() -> None:
    from trippostcollect.runtime import login_helpers

    page = FakePage()
    seen: list[dict[str, str]] = []

    class Element:
        async def get_property(self, name):
            return "https://qrcode.invalid/a.png"

    async def wait_for_selector(selector):
        return Element()

    page.wait_for_selector = wait_for_selector

    class Http:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers):
            seen.append(headers)
            return SimpleNamespace(status_code=200, content=b"png")

    result = asyncio.run(login_helpers.find_login_qrcode(
        page, "img", make_async_client=lambda **kwargs: Http(),
    ))
    assert result == "cG5n"
    assert seen == [{"User-Agent": BROWSER_UA}]
