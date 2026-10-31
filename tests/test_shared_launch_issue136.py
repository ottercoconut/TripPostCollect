"""#136：warmup、抖音/微博 crawl 标准模式与 B站行为会话共用同一套持久 context 启动配置。

抖音去掉 stealth（#130）后首页被判 ``verify_check``，而同一 profile、同一 IP 以 warmup 方式打开同一搜索页正常；
差别在 crawl 的启动配置：1920×1080 视口、未开沙箱（Playwright 自动追加 ``--no-sandbox``）、无 locale/timezone/env、
传 ``user_agent=None``、另开空白页。现在四处都经 ``runtime.browser_runtime.launch_profile_context`` 启动。

#139：共享配置不再传 ``viewport``（1280×900 设备模拟），改为 ``no_viewport=True`` 加 ``--window-size=1450,900``，
与小红书原生窗口一致；本文件中视口相关断言随之更新。

全部离线：替身 chromium 只记录 ``launch_persistent_context(**kwargs)``，不访问网络、不启动浏览器。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from trippostcollect.core import paths
from trippostcollect.runtime import browser_runtime

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ARGS = ["--runtime-arg-136"]
ENV = {"HOME": "/tmp/tpc-136-home"}
# 各调用方自有的取值：headless 与 executable_path 之外的 kwargs 必须四者完全相同。
CALLER_SPECIFIC = {"user_data_dir", "headless", "executable_path"}


class _Stop(Exception):
    """浏览器启动与第一页选定之后中止 start()，不进入登录与搜索。"""


class FakePage:
    def __init__(self, context: "FakeContext") -> None:
        self.context = context

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.context.events.append(("goto", self, url))
        if self.context.stop_on_goto:
            raise _Stop

    async def evaluate(self, script: str) -> str:
        assert script == "() => navigator.userAgent", script
        return "Mozilla/5.0 Chrome/149.0.0.0"


class FakeContext:
    def __init__(self, *, stop_on_goto: bool, with_page: bool) -> None:
        self.stop_on_goto = stop_on_goto
        self.events: list[tuple[Any, ...]] = []
        self.pages = [FakePage(self)] if with_page else []

    async def new_page(self) -> FakePage:
        self.events.append(("new_page",))
        page = FakePage(self)
        self.pages.append(page)
        return page

    async def add_init_script(self, *args: Any, **kwargs: Any) -> None:
        self.events.append(("init_script", args, kwargs))

    async def route(self, pattern: str, handler: Any) -> None:
        return None

    async def cookies(self, urls: Any = None) -> list[dict[str, str]]:
        return [{"name": "SESSDATA", "value": "s"}]

    async def close(self) -> None:
        return None


class FakeChromium:
    def __init__(self, *, stop_on_goto: bool = True, with_page: bool = True) -> None:
        self.stop_on_goto, self.with_page = stop_on_goto, with_page
        self.launches: list[dict[str, Any]] = []
        self.contexts: list[FakeContext] = []

    async def launch_persistent_context(self, **kwargs: Any) -> FakeContext:
        self.launches.append(kwargs)
        context = FakeContext(stop_on_goto=self.stop_on_goto, with_page=self.with_page)
        self.contexts.append(context)
        return context


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium

    def __call__(self) -> "FakePlaywright":
        return self

    async def __aenter__(self) -> SimpleNamespace:
        return SimpleNamespace(chromium=self.chromium)

    async def __aexit__(self, *args: Any) -> bool:
        return False


@pytest.fixture
def shared(monkeypatch):
    """共享函数照真实实现执行，运行时参数与环境取替身值，不在仓库 data/runtime 下建目录。"""
    monkeypatch.setattr(browser_runtime, "browser_runtime_args", lambda: list(RUNTIME_ARGS))
    monkeypatch.setattr(browser_runtime, "browser_launch_environment", lambda: dict(ENV))
    return browser_runtime.launch_profile_context


def _warmup_launch(monkeypatch, tmp_path: Path, shared) -> dict[str, Any]:
    from trippostcollect.application import warmup

    monkeypatch.setattr(warmup, "browser_runtime_args", lambda: list(RUNTIME_ARGS))
    monkeypatch.setattr(warmup, "browser_launch_environment", lambda: dict(ENV))
    chromium = FakeChromium()
    asyncio.run(warmup.launch_login_context(SimpleNamespace(chromium=chromium), tmp_path / "warmup", "/fake/chrome"))
    (kwargs,) = chromium.launches
    return kwargs


def _douyin_crawler(shared, chromium: FakeChromium):
    from trippostcollect.platforms.douyin import core

    crawler = object.__new__(core.DouYinCrawler)
    crawler.settings = SimpleNamespace(SAVE_LOGIN_STATE=True, ENABLE_CDP_MODE=False, HEADLESS=False)
    crawler.ports = SimpleNamespace(
        async_playwright=FakePlaywright(chromium), launch_profile_context=shared,
        project_browser_args=lambda: pytest.fail("#136：抖音标准模式不再用 project_browser_args"),
    )
    crawler.index_url = "https://www.douyin.com"
    return crawler


def _weibo_crawler(shared, chromium: FakeChromium, monkeypatch):
    from trippostcollect.platforms.weibo import core

    crawler = object.__new__(core.WeiboCrawler)
    crawler.config = SimpleNamespace(SAVE_LOGIN_STATE=True, ENABLE_CDP_MODE=False, HEADLESS=False)
    crawler.ports = SimpleNamespace(
        launch_profile_context=shared,
        project_browser_args=lambda: pytest.fail("#136：微博标准模式不再用 project_browser_args"),
    )
    crawler.index_url = "https://www.weibo.com"
    monkeypatch.setattr(core, "async_playwright", FakePlaywright(chromium))
    return crawler


def _crawler_start(crawler) -> None:
    with pytest.raises(_Stop):
        asyncio.run(crawler.start())


def _bilibili_launch(tmp_path: Path, shared) -> dict[str, Any]:
    from trippostcollect.platforms.bilibili import login
    from trippostcollect.runtime import cookies

    chromium = FakeChromium(stop_on_goto=False)

    async def hints(context: Any) -> None:
        return None

    async def behavior(page: Any, **kwargs: Any) -> dict[str, str]:
        return {"finished_at": "2026-10-31T00:00:00+00:00"}

    ports = SimpleNamespace(
        discover_cdp_browser_path=lambda: "/fake/chrome", profile_dir_for=lambda key: tmp_path / "bilibili",
        async_playwright=FakePlaywright(chromium), browser_runtime_args=lambda: list(RUNTIME_ARGS),
        browser_launch_environment=lambda: dict(ENV), launch_profile_context=shared,
        install_runtime_hints=hints, run_page_behavior=behavior, write_evidence=lambda *args: None,
        platform_cookie_url=cookies.platform_cookie_url, cookies_to_header=cookies.cookies_to_header,
        cookie_snapshot_path=lambda key: tmp_path / "snapshot.json",
        cookie_names_from_header=cookies.cookie_names_from_header,
        required_cookie_names=cookies.required_cookie_names,
    )
    asyncio.run(login.run_bilibili_behavior_session(
        SimpleNamespace(keyword="青岛", headed=False), tmp_path / "evidence.json", ports=ports,
    ))
    (kwargs,) = chromium.launches
    return kwargs


def _all_launches(monkeypatch, tmp_path: Path, shared) -> dict[str, dict[str, Any]]:
    douyin_chromium, weibo_chromium = FakeChromium(), FakeChromium()
    _crawler_start(_douyin_crawler(shared, douyin_chromium))
    _crawler_start(_weibo_crawler(shared, weibo_chromium, monkeypatch))
    return {
        "warmup": _warmup_launch(monkeypatch, tmp_path, shared),
        "douyin": douyin_chromium.launches[0],
        "weibo": weibo_chromium.launches[0],
        "bilibili": _bilibili_launch(tmp_path, shared),
    }


def test_four_launchers_share_one_persistent_context_configuration(monkeypatch, tmp_path, shared) -> None:
    launches = _all_launches(monkeypatch, tmp_path, shared)
    common = {name: {k: v for k, v in kwargs.items() if k not in CALLER_SPECIFIC} for name, kwargs in launches.items()}
    reference = common["warmup"]
    assert all(value == reference for value in common.values()), common
    for name, kwargs in launches.items():
        assert kwargs["chromium_sandbox"] is True, name
        # #139：不做设备模拟，按原生窗口启动。
        assert "viewport" not in kwargs and kwargs["no_viewport"] is True, name
        assert kwargs["args"][-1] == "--window-size=1450,900", name
        assert kwargs["locale"] == "zh-CN" and kwargs["timezone_id"] == "Asia/Shanghai", name
        assert kwargs["env"] == ENV, name
        assert kwargs["args"][-len(RUNTIME_ARGS) - 1:-1] == RUNTIME_ARGS, name
        assert "--no-sandbox" not in kwargs["args"], name
        for absent in ("user_agent", "ignore_default_args", "screen", "proxy"):
            assert absent not in kwargs, (name, absent)
    assert launches["warmup"]["headless"] is False
    assert launches["douyin"]["headless"] is False and launches["weibo"]["headless"] is False
    assert launches["bilibili"]["headless"] is True
    assert launches["warmup"]["executable_path"] == launches["bilibili"]["executable_path"] == "/fake/chrome"
    assert "executable_path" not in launches["douyin"] and "executable_path" not in launches["weibo"]
    assert launches["douyin"]["user_data_dir"] == str(paths.platform_profile_dir("douyin"))
    assert launches["weibo"]["user_data_dir"] == str(paths.platform_profile_dir("weibo"))
    assert launches["bilibili"]["user_data_dir"] == str(tmp_path / "bilibili")


@pytest.mark.parametrize("platform", ["douyin", "weibo"])
def test_standard_mode_reuses_the_persistent_context_first_page(platform, monkeypatch, shared) -> None:
    chromium = FakeChromium()
    crawler = _douyin_crawler(shared, chromium) if platform == "douyin" else _weibo_crawler(shared, chromium, monkeypatch)
    _crawler_start(crawler)
    (context,) = chromium.contexts
    assert crawler.context_page is context.pages[0]
    assert len(context.pages) == 1
    assert context.events == [("goto", context.pages[0], crawler.index_url)]


@pytest.mark.parametrize("platform", ["douyin", "weibo"])
def test_standard_mode_opens_a_page_only_when_the_context_has_none(platform, monkeypatch, shared) -> None:
    chromium = FakeChromium(with_page=False)
    crawler = _douyin_crawler(shared, chromium) if platform == "douyin" else _weibo_crawler(shared, chromium, monkeypatch)
    _crawler_start(crawler)
    (context,) = chromium.contexts
    assert context.events[0] == ("new_page",)
    assert crawler.context_page is context.pages[0]


def test_native_window_size_defaults_to_the_shared_native_window(shared, tmp_path) -> None:
    """#139：缺省即原生窗口 1450×900；显式传入的尺寸照样写进 ``--window-size``。"""
    default, explicit = FakeChromium(), FakeChromium()
    asyncio.run(shared(default, tmp_path, headless=False))
    asyncio.run(shared(explicit, tmp_path, headless=False, native_window_size=(1400, 880)))
    (default_kwargs,), (explicit_kwargs,) = default.launches, explicit.launches
    for kwargs in (default_kwargs, explicit_kwargs):
        assert "viewport" not in kwargs and kwargs["no_viewport"] is True
        assert kwargs["chromium_sandbox"] is True
    assert default_kwargs["args"][-1] == "--window-size=1450,900"
    assert explicit_kwargs["args"][-1] == "--window-size=1400,880"
    assert default_kwargs["args"][:-1] == explicit_kwargs["args"][:-1]


def test_shared_launcher_defaults_read_runtime_values_at_call_time(shared, tmp_path) -> None:
    chromium = FakeChromium()
    asyncio.run(shared(chromium, tmp_path, headless=True))
    (kwargs,) = chromium.launches
    assert kwargs["args"][-2] == RUNTIME_ARGS[-1] and kwargs["env"] == ENV
    signature = inspect.signature(browser_runtime.launch_profile_context)
    assert [name for name, p in signature.parameters.items() if p.kind is p.KEYWORD_ONLY] == [
        "headless", "browser_path", "native_window_size", "runtime_args", "env",
    ]


def test_platform_adapters_receive_the_launcher_through_ports() -> None:
    """依赖方向：平台包不直接 import runtime.browser_runtime，经 ports 注入共享函数。"""
    for platform in ("douyin", "weibo", "bilibili"):
        for path in (ROOT / "src/trippostcollect/platforms" / platform).glob("*.py"):
            nodes = list(ast.walk(ast.parse(path.read_text(encoding="utf-8"))))
            imported = [node.module for node in nodes if isinstance(node, ast.ImportFrom)]
            assert not [module for module in imported if module and "browser_runtime" in module], path
            keywords = {node.arg for node in nodes if isinstance(node, ast.keyword)}
            assert not keywords & {"ignore_default_args", "screen", "chromium_sandbox"}, path
            assert not [node for node in nodes if isinstance(node, ast.Constant) and node.value == "--no-sandbox"], path
    entry = (ROOT / "src/trippostcollect/platforms/entry.py").read_text(encoding="utf-8")
    assert entry.count("launch_profile_context=launch_profile_context") == 2
    executor = (ROOT / "scripts/mediacrawler_crawl.py").read_text(encoding="utf-8")
    assert executor.count("launch_profile_context=launch_profile_context") == 1
