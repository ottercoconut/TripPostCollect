"""#127 第 2 项：通用四平台不再向浏览器注入 stealth.min.js，包内也不再携带该资源。

有头 CfT 下不注入时 webdriver、chrome.runtime、plugins、permissions 本来就正常；注入后反而把 languages、
hardwareConcurrency 与 WebGL 改成与真实环境矛盾的假值。全部离线：假 playwright/context/page，不访问网络、
不启动浏览器。页面证据抓取的 patchright ``stealth`` 模式与专用 profile 目录与该脚本无关，不在本文件范围。
"""

from __future__ import annotations

import asyncio
from importlib import resources as importlib_resources
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "trippostcollect"
# patchright 独立模式名与其专用 profile 目录，和 stealth.min.js 无关。
PATCHRIGHT_STEALTH_MODE = frozenset({PACKAGE / "platforms" / "registry.py", PACKAGE / "core" / "paths.py"})


class _Stop(Exception):
    """浏览器启动阶段结束后中止 start()，不进入页面、登录与搜索。"""


class RecordingContext:
    """记录全部 init script；new_page 即中止，证明检查点在启动阶段之后。"""

    def __init__(self) -> None:
        self.init_scripts: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def add_init_script(self, *args: Any, **kwargs: Any) -> None:
        self.init_scripts.append((args, kwargs))

    async def new_page(self) -> None:
        raise _Stop


class FakePlaywright:
    async def __aenter__(self) -> SimpleNamespace:
        return SimpleNamespace(chromium=object())

    async def __aexit__(self, *args: Any) -> bool:
        return False


# ---------- 1. 源码不再引用该脚本 ----------


def test_source_has_no_stealth_script_or_injector() -> None:
    hits = []
    for path in sorted(PACKAGE.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if "stealth" in path.name.lower():
            hits.append((str(path.relative_to(ROOT)), "file name"))
        text = path.read_bytes().decode("utf-8", errors="ignore")
        for needle in ("stealth.min", "add_stealth_script"):
            if needle in text:
                hits.append((str(path.relative_to(ROOT)), needle))
        if path.suffix == ".py" and path not in PATCHRIGHT_STEALTH_MODE and "stealth" in text.lower():
            hits.append((str(path.relative_to(ROOT)), "stealth"))
    assert hits == []


# ---------- 2. 资源表与包资源目录 ----------


def test_package_resources_no_longer_ship_stealth_script() -> None:
    from trippostcollect.core import resources

    assert [name for name in resources.RESOURCE_SHA256 if "stealth" in name] == []
    js = importlib_resources.files("trippostcollect.resources").joinpath("js")
    assert sorted(item.name for item in js.iterdir() if item.is_file()) == ["douyin.js", "zhihu.js"]
    with pytest.raises(FileNotFoundError):
        resources.read_bytes("js/stealth.min.js")


# ---------- 3. 微博、知乎、抖音标准模式启动不注入 ----------


def _standard_mode_crawler(platform: str, monkeypatch: pytest.MonkeyPatch, context: RecordingContext):
    settings = SimpleNamespace(ENABLE_CDP_MODE=False, HEADLESS=False)
    if platform == "weibo":
        from trippostcollect.platforms.weibo import core

        crawler = object.__new__(core.WeiboCrawler)
        crawler.config = settings
        monkeypatch.setattr(core, "async_playwright", FakePlaywright)
    elif platform == "zhihu":
        from trippostcollect.platforms.zhihu import core

        crawler = object.__new__(core.ZhihuCrawler)
        crawler.settings = settings
        crawler.ports = SimpleNamespace(async_playwright=FakePlaywright)
    else:
        from trippostcollect.platforms.douyin import core

        crawler = object.__new__(core.DouYinCrawler)
        crawler.settings = settings
        crawler.ports = SimpleNamespace(async_playwright=FakePlaywright)
    launches = []

    async def launch_browser(chromium, playwright_proxy, user_agent, headless=True):
        launches.append(headless)
        return context

    crawler.launch_browser = launch_browser
    return crawler, launches


@pytest.mark.parametrize("platform", ["weibo", "zhihu", "douyin"])
def test_standard_mode_start_injects_no_init_script(platform: str, monkeypatch: pytest.MonkeyPatch) -> None:
    context = RecordingContext()
    crawler, launches = _standard_mode_crawler(platform, monkeypatch, context)
    with pytest.raises(_Stop):
        asyncio.run(crawler.start())
    assert launches == [False]
    assert crawler.browser_context is context
    assert context.init_scripts == []


# ---------- 4. 抖音 CDP 路径不调用注入 ----------


def test_douyin_cdp_launch_does_not_inject_stealth() -> None:
    from trippostcollect.platforms.douyin import core
    from trippostcollect.runtime.browser import CDPBrowserManager

    assert not hasattr(CDPBrowserManager, "add_stealth_script")
    context = RecordingContext()

    class Manager:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def launch_and_connect(self, **kwargs: Any) -> RecordingContext:
            self.calls.append("launch_and_connect")
            return context

        async def get_browser_info(self) -> dict[str, Any]:
            self.calls.append("get_browser_info")
            return {}

        def __getattr__(self, name: str):
            # 任何未声明的 manager 调用（包括旧的 add_stealth_script）都记下来，而不是抛错走回退分支。
            async def record(*args: Any, **kwargs: Any) -> None:
                self.calls.append(name)

            return record

    manager = Manager()
    fallbacks = []
    crawler = object.__new__(core.DouYinCrawler)
    crawler.ports = SimpleNamespace(cdp_manager=lambda: manager)

    async def launch_browser(*args: Any, **kwargs: Any) -> None:
        fallbacks.append(args)

    crawler.launch_browser = launch_browser
    result = asyncio.run(crawler.launch_browser_with_cdp(object(), None, None, headless=False))
    assert result is context
    assert fallbacks == []
    assert manager.calls == ["launch_and_connect", "get_browser_info"]
    assert context.init_scripts == []


# ---------- 5. B站行为会话只装 runtime hints ----------


def test_bilibili_behavior_session_only_installs_runtime_hints(tmp_path: Path) -> None:
    from trippostcollect.platforms.bilibili import login
    from trippostcollect.runtime import cookies
    from trippostcollect.runtime.human_flow import install_runtime_hints

    class Page:
        async def goto(self, url: str, **kwargs: Any) -> None:
            return None

        async def evaluate(self, script: str) -> str:
            assert script == "() => navigator.userAgent", script
            return "Mozilla/5.0 Chrome/149.0.0.0"

    class Context:
        def __init__(self) -> None:
            self.pages = [Page()]
            self.init_scripts: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        async def add_init_script(self, *args: Any, **kwargs: Any) -> None:
            self.init_scripts.append((args, kwargs))

        async def route(self, pattern: str, handler: Any) -> None:
            return None

        async def cookies(self, urls: list[str]) -> list[dict[str, str]]:
            return [{"name": "SESSDATA", "value": "s"}]

        async def close(self) -> None:
            return None

    context = Context()

    class Playwright:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace(chromium=self)

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def launch_persistent_context(self, **kwargs: Any) -> Context:
            return context

    async def behavior(page: Any, **kwargs: Any) -> dict[str, str]:
        return {"finished_at": "2026-10-30T00:00:00+00:00"}

    ports = SimpleNamespace(
        discover_cdp_browser_path=lambda: None, profile_dir_for=lambda key: tmp_path / "profile",
        async_playwright=Playwright, browser_runtime_args=lambda: [], browser_launch_environment=lambda: {},
        install_runtime_hints=install_runtime_hints, run_page_behavior=behavior, write_evidence=lambda *args: None,
        platform_cookie_url=cookies.platform_cookie_url, cookies_to_header=cookies.cookies_to_header,
        cookie_snapshot_path=lambda key: tmp_path / "snapshot.json",
        cookie_names_from_header=cookies.cookie_names_from_header,
        required_cookie_names=cookies.required_cookie_names,
    )
    asyncio.run(login.run_bilibili_behavior_session(
        SimpleNamespace(keyword="青岛", headed=False), tmp_path / "evidence.json", ports=ports,
    ))
    # 唯一一次是 runtime hints 的内联脚本；不再有以 path= 注入的文件脚本。
    assert [kwargs for _, kwargs in context.init_scripts] == [{}]
    ((args, _),) = context.init_scripts
    assert len(args) == 1 and "navigator" in args[0] and "stealth" not in args[0].lower()
