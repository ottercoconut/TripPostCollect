# -*- coding: utf-8 -*-
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from trippostcollect.core import paths

from dataclasses import replace

from support.browser_settings import BROWSER_SETTINGS
from trippostcollect.runtime.browser import CDPBrowserManager
from trippostcollect.runtime.browser_launcher import BrowserLauncher


@pytest.mark.asyncio
async def test_existing_browser_connects_directly_to_devtools_browser(monkeypatch):
    settings = replace(BROWSER_SETTINGS, CDP_CONNECT_EXISTING=True, BROWSER_LAUNCH_TIMEOUT=60)

    manager = CDPBrowserManager(settings, project_browser_args=lambda: ["--lang=zh-CN"])
    manager.debug_port = 9222
    manager._get_browser_websocket_url = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("existing browser mode must not call /json/version")
    )

    browser = MagicMock()
    browser.is_connected.return_value = True
    browser.contexts = []

    playwright = MagicMock()
    playwright.chromium.connect_over_cdp = AsyncMock(return_value=browser)

    await manager._connect_via_cdp(playwright)

    playwright.chromium.connect_over_cdp.assert_awaited_once_with(
        "ws://localhost:9222/devtools/browser",
        timeout=60000,
    )


@pytest.mark.asyncio
async def test_existing_browser_falls_back_to_discovered_websocket_url(monkeypatch):
    settings = replace(BROWSER_SETTINGS, CDP_CONNECT_EXISTING=True, BROWSER_LAUNCH_TIMEOUT=60)

    manager = CDPBrowserManager(settings, project_browser_args=lambda: ["--lang=zh-CN"])
    manager.debug_port = 9222
    manager._get_browser_websocket_url = AsyncMock(  # type: ignore[method-assign]
        return_value="ws://localhost:9222/devtools/browser/generated-id"
    )

    browser = MagicMock()
    browser.is_connected.return_value = True
    browser.contexts = []

    playwright = MagicMock()
    playwright.chromium.connect_over_cdp = AsyncMock(
        side_effect=[RuntimeError("direct websocket failed"), browser]
    )

    await manager._connect_via_cdp(playwright)

    manager._get_browser_websocket_url.assert_awaited_once_with(9222)
    assert playwright.chromium.connect_over_cdp.await_args_list[0].args == (
        "ws://localhost:9222/devtools/browser",
    )
    assert playwright.chromium.connect_over_cdp.await_args_list[0].kwargs == {
        "timeout": 60000,
    }
    assert playwright.chromium.connect_over_cdp.await_args_list[1].args == (
        "ws://localhost:9222/devtools/browser/generated-id",
    )
    assert playwright.chromium.connect_over_cdp.await_args_list[1].kwargs == {
        "timeout": 60000,
    }


@pytest.mark.asyncio
async def test_launched_browser_uses_discovered_websocket_url(monkeypatch):
    settings = replace(BROWSER_SETTINGS, CDP_CONNECT_EXISTING=False)

    manager = CDPBrowserManager(settings, project_browser_args=lambda: ["--lang=zh-CN"])
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


def test_xhs_browser_rejects_invalid_window_size(monkeypatch):
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_WINDOW_SIZE", "invalid")

    with pytest.raises(RuntimeError, match="invalid TRIPPOSTCOLLECT_XHS_WINDOW_SIZE"):
        BrowserLauncher.xhs_window_size_argument()


@pytest.mark.asyncio
async def test_failed_cdp_launch_is_force_cleaned_once_by_owning_manager(
    monkeypatch,
) -> None:
    settings = replace(BROWSER_SETTINGS, CDP_CONNECT_EXISTING=False)
    manager = CDPBrowserManager(settings, project_browser_args=lambda: ["--lang=zh-CN"])
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


@pytest.mark.asyncio
async def test_xhs_cdp_rejects_existing_browser_without_a_retry_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "empty-run-profile"
    profile_dir.mkdir()
    settings = replace(BROWSER_SETTINGS, PLATFORM="xhs", CDP_CONNECT_EXISTING=True)
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_PROFILE_DIR", str(profile_dir))
    manager = CDPBrowserManager(settings, project_browser_args=lambda: ["--lang=zh-CN"])
    manager._connect_existing_browser = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("XHS must not attach to an unrelated profile")
    )
    manager._get_browser_path = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("forbidden mode must not launch Chrome")
    )
    manager.cleanup = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="^xhs_cdp_connect_existing_forbidden$"):
        await manager.launch_and_connect(MagicMock())
    with pytest.raises(RuntimeError, match="^cdp_browser_manager_already_started$"):
        await manager.launch_and_connect(MagicMock())

    manager._connect_existing_browser.assert_not_awaited()
    manager._get_browser_path.assert_not_awaited()
    manager.cleanup.assert_awaited_once_with(force=True)
