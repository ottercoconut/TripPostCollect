"""#142：知乎 CDP 与标准回退的中文、时区和原生窗口配置。"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from support.browser_settings import BROWSER_SETTINGS
from trippostcollect.platforms.zhihu.core import ZhihuCrawler
from trippostcollect.runtime import behavior
from trippostcollect.runtime.browser import CDPBrowserManager
from trippostcollect.runtime.browser_launcher import BrowserLauncher


def test_external_launch_accepts_native_window_args_and_environment(monkeypatch, tmp_path):
    popen = MagicMock()
    monkeypatch.setattr("trippostcollect.runtime.browser_launcher.subprocess.Popen", popen)
    env = {"TZ": "Asia/Shanghai"}

    BrowserLauncher(project_browser_args=lambda: ["--lang=zh-CN"]).launch_browser(
        "/fake/chrome", 9222, headless=False, user_data_dir=str(tmp_path / "profile"),
        extra_args=["--accept-lang=zh-CN", "--window-size=1450,900"], env=env,
    )

    arguments = popen.call_args.args[0]
    assert "--accept-lang=zh-CN" in arguments
    assert "--window-size=1450,900" in arguments
    assert "--start-maximized" not in arguments
    assert popen.call_args.kwargs["env"] is env


@pytest.mark.asyncio
async def test_only_zhihu_manager_adds_language_window_and_timezone(monkeypatch, tmp_path):
    manager = CDPBrowserManager(
        replace(BROWSER_SETTINGS, PLATFORM="zhihu", SAVE_LOGIN_STATE=True),
        project_browser_args=lambda: [],
    )
    manager.debug_port = 9222
    manager.launcher.launch_browser = MagicMock(return_value=MagicMock())
    manager.launcher.wait_for_browser_ready = MagicMock(return_value=True)
    manager._test_cdp_connection = AsyncMock(return_value=True)
    manager._clean_session_restore_tabs = MagicMock()
    monkeypatch.setenv("TRIPPOSTCOLLECT_SHARE_CDP_PROFILE", "1")
    monkeypatch.setattr("trippostcollect.runtime.browser.platform_profile_dir", lambda _: tmp_path / "profile")
    monkeypatch.setattr("trippostcollect.runtime.browser.asyncio.sleep", AsyncMock())

    await manager._launch_browser("/fake/chrome", headless=False)

    kwargs = manager.launcher.launch_browser.call_args.kwargs
    assert kwargs["extra_args"] == ["--accept-lang=zh-CN", "--window-size=1450,900"]
    assert kwargs["env"]["TZ"] == "Asia/Shanghai"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["zhihu", "xhs"])
async def test_new_cdp_context_viewport_is_platform_specific(platform):
    manager = CDPBrowserManager(
        replace(BROWSER_SETTINGS, PLATFORM=platform), project_browser_args=lambda: [],
    )
    manager.browser = SimpleNamespace(
        contexts=[], new_context=AsyncMock(return_value="context"),
    )

    assert await manager._create_browser_context() == "context"
    kwargs = manager.browser.new_context.call_args.kwargs
    if platform == "zhihu":
        assert kwargs == {
            "accept_downloads": True, "no_viewport": True,
            "locale": "zh-CN", "timezone_id": "Asia/Shanghai",
        }
    else:
        assert kwargs["viewport"] == {"width": 1920, "height": 1080}
        assert "no_viewport" not in kwargs


@pytest.mark.asyncio
async def test_zhihu_standard_fallback_uses_shared_profile_launcher(monkeypatch, tmp_path):
    crawler = object.__new__(ZhihuCrawler)
    crawler.settings = SimpleNamespace(SAVE_LOGIN_STATE=True)
    crawler.ports = SimpleNamespace(launch_profile_context=AsyncMock(return_value="context"))
    monkeypatch.setattr(
        "trippostcollect.platforms.zhihu.core.platform_profile_dir", lambda _: tmp_path / "profile",
    )
    chromium = MagicMock()

    assert await crawler.launch_browser(chromium, None, None, headless=False) == "context"
    crawler.ports.launch_profile_context.assert_awaited_once_with(
        chromium, tmp_path / "profile", headless=False,
    )


@pytest.mark.asyncio
async def test_runtime_fingerprint_records_timezone():
    page = SimpleNamespace(evaluate=AsyncMock(return_value={}))

    assert await behavior.runtime_fingerprint(page) == {}
    script = page.evaluate.call_args.args[0]
    assert "Intl.DateTimeFormat().resolvedOptions().timeZone" in script
