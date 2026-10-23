# -*- coding: utf-8 -*-
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from trippostcollect.core import paths
from trippostcollect.runtime import cookies

from dataclasses import replace

from support.browser_settings import BROWSER_SETTINGS
from trippostcollect.runtime.browser import CDPBrowserManager
from trippostcollect.runtime.browser_launcher import BrowserLauncher


@pytest.mark.asyncio
async def test_launched_browser_uses_discovered_websocket_url(monkeypatch):
    manager = CDPBrowserManager(BROWSER_SETTINGS, project_browser_args=lambda: ["--lang=zh-CN"])
    manager.debug_port = 9223
    manager._get_browser_websocket_url = AsyncMock(  # type: ignore[method-assign]
        return_value="ws://localhost:9223/devtools/browser/generated-id"
    )

    browser = MagicMock()
    browser.is_connected.return_value = True
    browser.contexts = []

    playwright = MagicMock()
    playwright.chromium.connect_over_cdp = AsyncMock(return_value=browser)

    await manager._connect_via_cdp(playwright)

    manager._get_browser_websocket_url.assert_awaited_once_with(9223)
    playwright.chromium.connect_over_cdp.assert_awaited_once_with(
        "ws://localhost:9223/devtools/browser/generated-id"
    )


def test_xhs_browser_uses_stable_native_window_instead_of_maximizing(monkeypatch):
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_WINDOW_SIZE", "1450,900")
    popen = MagicMock()
    monkeypatch.setattr("trippostcollect.runtime.browser_launcher.subprocess.Popen", popen)

    BrowserLauncher(project_browser_args=lambda: ["--lang=zh-CN"]).launch_browser(
        browser_path="/Applications/Google Chrome",
        debug_port=9222,
        headless=False,
        user_data_dir="/tmp/xhs-profile",
    )

    arguments = popen.call_args.args[0]
    assert "--window-size=1450,900" in arguments
    assert "--start-maximized" not in arguments


# #83：调试端口对局域网暴露、关闭渲染进程沙箱、无效或可被页面测出的旧参数一律不传。
REMOVED_CDP_ARGUMENTS = (
    "--remote-debugging-address=0.0.0.0",
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--exclude-switches=enable-automation",
    "--disable-infobars",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
)


@pytest.mark.parametrize("headless", [False, True])
def test_cdp_launch_arguments_drop_exposed_and_detectable_switches(monkeypatch, headless):
    monkeypatch.delenv("TRIPPOSTCOLLECT_XHS_WINDOW_SIZE", raising=False)
    popen = MagicMock()
    monkeypatch.setattr("trippostcollect.runtime.browser_launcher.subprocess.Popen", popen)

    BrowserLauncher(project_browser_args=lambda: ["--lang=zh-CN"]).launch_browser(
        browser_path="/fake/chrome",
        debug_port=51234,
        headless=headless,
        user_data_dir="/tmp/profile",
    )

    arguments = popen.call_args.args[0]
    assert [item for item in arguments if item in REMOVED_CDP_ARGUMENTS] == []
    assert not any(item.startswith("--remote-debugging-address") for item in arguments)
    assert [item for item in arguments if item.startswith("--remote-debugging-")] == [
        "--remote-debugging-port=51234"
    ]
    # 保留：被遮挡的窗口仍按可见处理（行为门禁、IntersectionObserver 与动画帧依赖页面可见）。
    assert "--disable-backgrounding-occluded-windows" in arguments
    assert ("--headless=new" in arguments) is headless


def test_debug_port_is_assigned_by_the_system_on_loopback(monkeypatch):
    binds: list[tuple[str, int]] = []

    class FakeSocket:
        def __init__(self, family, kind):
            assert (family, kind) == (socket.AF_INET, socket.SOCK_STREAM)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def bind(self, address):
            binds.append(address)

        def getsockname(self):
            return ("127.0.0.1", 53117)

    monkeypatch.setattr("trippostcollect.runtime.browser_launcher.socket.socket", FakeSocket)

    assert BrowserLauncher.find_available_port() == 53117
    assert binds == [("127.0.0.1", 0)]


@pytest.mark.asyncio
async def test_cdp_probes_and_websocket_discovery_use_the_loopback_address(monkeypatch):
    requested: list[str] = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:53117/devtools/browser/id"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url, timeout):
            requested.append(url)
            return FakeResponse()

    monkeypatch.setattr("trippostcollect.runtime.browser.httpx.AsyncClient", FakeClient)
    connects: list[tuple[str, int]] = []
    socket_instance = MagicMock()
    socket_instance.__enter__.return_value = socket_instance
    socket_instance.connect_ex.side_effect = lambda address: connects.append(address) or 0
    monkeypatch.setattr("trippostcollect.runtime.browser.socket.socket", MagicMock(return_value=socket_instance))
    monkeypatch.setattr("trippostcollect.runtime.browser_launcher.socket.socket", MagicMock(return_value=socket_instance))
    manager = CDPBrowserManager(BROWSER_SETTINGS, project_browser_args=lambda: ["--lang=zh-CN"])
    manager.launcher.browser_process = MagicMock(pid=4242, poll=MagicMock(return_value=None))

    assert await manager._get_browser_websocket_url(53117) == "ws://127.0.0.1:53117/devtools/browser/id"
    assert await manager._test_cdp_connection(53117) is True
    assert manager.launcher.wait_for_browser_ready(53117, timeout=5) is True

    assert requested == ["http://127.0.0.1:53117/json/version"]
    assert connects == [("127.0.0.1", 53117), ("127.0.0.1", 53117)]


def test_cookie_export_launch_arguments_keep_the_renderer_sandbox(monkeypatch, tmp_path):
    """#83：Cookie 导出子进程的 Chromium 不传 --no-sandbox，并显式开启沙箱，其他参数不变。

    子进程脚本在本进程内对假 Playwright 执行一次，断言实际传给 launch_persistent_context 的参数。
    """
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    commands = []
    monkeypatch.setattr(cookies, "load_cookie_snapshot", lambda _key: None)
    monkeypatch.setattr(cookies, "profile_dir_for", lambda _key: profile_dir)
    monkeypatch.setattr(cookies, "browser_runtime_args", lambda: ["--lang=zh-CN"])
    monkeypatch.setattr(cookies, "browser_launch_environment", lambda: {})
    monkeypatch.setattr(
        cookies.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command) or MagicMock(returncode=1, stdout=""),
    )

    assert cookies.export_profile_cookies("zhihu", None) is None

    launches = []

    class FakePage:
        async def goto(self, *_args, **_kwargs):
            return None

        async def wait_for_timeout(self, _milliseconds):
            return None

    class FakeContext:
        pages = [FakePage()]

        async def cookies(self, _urls):
            return []

        async def close(self):
            return None

    class FakeChromium:
        async def launch_persistent_context(self, **kwargs):
            launches.append(kwargs)
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setitem(sys.modules, "playwright.async_api", SimpleNamespace(async_playwright=FakePlaywright))
    monkeypatch.setattr(sys, "argv", ["-c", *commands[0][3:]])
    with pytest.raises(SystemExit) as exited:
        exec(compile(commands[0][2], "<cookie-export>", "exec"), {"__name__": "__main__"})

    assert exited.value.code == 0
    assert launches[0]["chromium_sandbox"] is True
    assert launches[0]["args"] == ["--disable-dev-shm-usage", "--lang=zh-CN"]
    assert launches[0]["user_data_dir"] == str(profile_dir)


def test_xhs_browser_rejects_invalid_window_size(monkeypatch):
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_WINDOW_SIZE", "invalid")

    with pytest.raises(RuntimeError, match="invalid TRIPPOSTCOLLECT_XHS_WINDOW_SIZE"):
        BrowserLauncher.xhs_window_size_argument()


@pytest.mark.asyncio
async def test_failed_cdp_launch_is_force_cleaned_once_by_owning_manager(
    monkeypatch,
) -> None:
    manager = CDPBrowserManager(BROWSER_SETTINGS, project_browser_args=lambda: ["--lang=zh-CN"])
    manager._get_browser_path = AsyncMock(return_value="/fake/chrome")
    manager.launcher.find_available_port = MagicMock(return_value=9444)
    manager._launch_browser = AsyncMock()
    manager._register_cleanup_handlers = MagicMock()
    manager._connect_via_cdp = AsyncMock(
        side_effect=RuntimeError("cdp handshake failed")
    )
    manager._create_browser_context = AsyncMock(
        side_effect=AssertionError("context must not be created after failed handshake")
    )
    manager.cleanup = AsyncMock()

    with pytest.raises(RuntimeError, match="cdp handshake failed"):
        await manager.launch_and_connect(playwright=MagicMock())

    manager.launcher.find_available_port.assert_called_once_with()
    assert manager.debug_port == 9444
    manager._launch_browser.assert_awaited_once_with("/fake/chrome", False)
    manager._register_cleanup_handlers.assert_called_once_with()
    manager.cleanup.assert_awaited_once_with(force=True)
    manager._create_browser_context.assert_not_awaited()


def _ready_cdp_launch_manager(monkeypatch: pytest.MonkeyPatch, settings) -> CDPBrowserManager:
    manager = CDPBrowserManager(settings, project_browser_args=lambda: ["--lang=zh-CN"])
    manager.debug_port = 9444
    manager.launcher.launch_browser = MagicMock(return_value=object())
    manager.launcher.wait_for_browser_ready = MagicMock(return_value=True)
    manager._test_cdp_connection = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager._clean_session_restore_tabs = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr("trippostcollect.runtime.browser.asyncio.sleep", AsyncMock())
    return manager


@pytest.mark.asyncio
@pytest.mark.parametrize("save_login_state", [True, False])
async def test_xhs_cdp_launch_always_uses_the_run_scoped_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    save_login_state: bool,
) -> None:
    profile_dir = tmp_path / "empty-run-profile"
    profile_dir.mkdir()
    settings = replace(BROWSER_SETTINGS, PLATFORM="xhs", SAVE_LOGIN_STATE=save_login_state)
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_PROFILE_DIR", str(profile_dir))
    manager = _ready_cdp_launch_manager(monkeypatch, settings)

    await manager._launch_browser("/fake/chrome", False)

    manager.launcher.launch_browser.assert_called_once_with(
        browser_path="/fake/chrome",
        debug_port=9444,
        headless=False,
        user_data_dir=str(profile_dir),
    )
    manager._clean_session_restore_tabs.assert_called_once_with(str(profile_dir))
    manager.launcher.wait_for_browser_ready.assert_called_once_with(
        9444,
        settings.BROWSER_LAUNCH_TIMEOUT,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("save_login_state", [True, False])
@pytest.mark.parametrize("profile_value", [None, "", " \t "])
async def test_xhs_cdp_launch_rejects_missing_profile_before_chrome(
    monkeypatch: pytest.MonkeyPatch,
    save_login_state: bool,
    profile_value: str | None,
) -> None:
    settings = replace(BROWSER_SETTINGS, PLATFORM="xhs", SAVE_LOGIN_STATE=save_login_state)
    if profile_value is None:
        monkeypatch.delenv("TRIPPOSTCOLLECT_XHS_PROFILE_DIR", raising=False)
    else:
        monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_PROFILE_DIR", profile_value)
    manager = _ready_cdp_launch_manager(monkeypatch, settings)

    with pytest.raises(
        RuntimeError,
        match="^XHS requires TRIPPOSTCOLLECT_XHS_PROFILE_DIR from xhs_runner.py$",
    ):
        await manager._launch_browser("/fake/chrome", False)

    manager.launcher.launch_browser.assert_not_called()
    manager.launcher.wait_for_browser_ready.assert_not_called()
    manager._test_cdp_connection.assert_not_awaited()
    manager._clean_session_restore_tabs.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("save_login_state", "expected_profile"),
    [
        (False, None),
        (True, "browser_data/cdp_zhihu_user_data_dir"),
    ],
)
async def test_non_xhs_cdp_profile_behavior_is_unchanged_and_ignores_xhs_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    save_login_state: bool,
    expected_profile: str | None,
) -> None:
    # T14：参数保留旧 fork 目录名作为台账节点 ID；实际落点是 core.paths 中与之一一对应的新位置
    # （未共享 CDP profile 的 cdp_<code>_user_data_dir -> data/runtime/platform_sessions/<platform>/cdp_profile）。
    xhs_only_path = tmp_path / "must-not-be-used"
    monkeypatch.setattr("trippostcollect.core.paths.PLATFORM_SESSIONS_ROOT", tmp_path / "platform_sessions")
    legacy_to_new = {"browser_data/cdp_zhihu_user_data_dir": str(tmp_path / "platform_sessions/zhihu/cdp_profile")}
    assert legacy_to_new["browser_data/cdp_zhihu_user_data_dir"] == str(paths.platform_cdp_profile_dir("zhihu"))
    settings = replace(BROWSER_SETTINGS, PLATFORM="zhihu", SAVE_LOGIN_STATE=save_login_state)
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_PROFILE_DIR", str(xhs_only_path))
    monkeypatch.delenv("TRIPPOSTCOLLECT_SHARE_CDP_PROFILE", raising=False)
    manager = _ready_cdp_launch_manager(monkeypatch, settings)

    await manager._launch_browser("/fake/chrome", True)

    resolved_profile = (
        legacy_to_new[expected_profile] if expected_profile is not None else None
    )
    manager.launcher.launch_browser.assert_called_once_with(
        browser_path="/fake/chrome",
        debug_port=9444,
        headless=True,
        user_data_dir=resolved_profile,
    )
    assert not xhs_only_path.exists()
    if resolved_profile is None:
        manager._clean_session_restore_tabs.assert_not_called()
    else:
        manager._clean_session_restore_tabs.assert_called_once_with(
            resolved_profile
        )
