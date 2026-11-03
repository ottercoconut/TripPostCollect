"""#137：参数组装无副作用，崩溃目录只在真实浏览器启动前创建。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import ctf_resource_crawl
from trippostcollect.application.collection import behavior_environment
from trippostcollect.platforms.zhihu import core as zhihu_core
from trippostcollect.runtime import browser_launcher, browser_runtime


class FakeChromium:
    def __init__(self) -> None:
        self.launches: list[dict[str, Any]] = []

    async def launch_persistent_context(self, **kwargs: Any) -> dict[str, Any]:
        self.launches.append(kwargs)
        return kwargs


def runtime_args(crash_dir: Path) -> list[str]:
    return [
        "--disable-crash-reporter",
        f"--crash-dumps-dir={crash_dir}",
        "--use-mock-keychain",
    ]


def test_argument_assembly_and_behavior_environment_do_not_create_directories(
    monkeypatch, tmp_path: Path,
) -> None:
    crash_dir = tmp_path / "chrome_crash_dumps"
    monkeypatch.setattr(browser_runtime, "CHROME_CRASH_DUMPS", crash_dir)
    monkeypatch.setattr(
        browser_runtime, "ensure_dir", lambda path: pytest.fail(f"参数组装不得建目录: {path}"),
    )

    expected = runtime_args(crash_dir)
    assert browser_runtime.browser_runtime_args() == expected
    environment = behavior_environment(tmp_path / "evidence.json", "offline")
    assert json.loads(environment["TRIPPOSTCOLLECT_BROWSER_ARGS_JSON"]) == expected
    assert not crash_dir.exists()


def test_shared_launch_creates_crash_directory_before_fake_launch(monkeypatch, tmp_path: Path) -> None:
    crash_dir = tmp_path / "chrome_crash_dumps"
    monkeypatch.setattr(browser_runtime, "CHROME_CRASH_DUMPS", crash_dir)
    chromium = FakeChromium()

    asyncio.run(browser_runtime.launch_profile_context(
        chromium, tmp_path / "profile", headless=True, runtime_args=browser_runtime.browser_runtime_args(), env={},
    ))

    assert crash_dir.is_dir()
    (kwargs,) = chromium.launches
    assert kwargs["args"][-4:-1] == runtime_args(crash_dir)


def test_cdp_launch_creates_crash_directory_before_fake_popen(monkeypatch, tmp_path: Path) -> None:
    crash_dir = tmp_path / "chrome_crash_dumps"
    monkeypatch.setattr(browser_launcher, "CHROME_CRASH_DUMPS", crash_dir)
    popen = MagicMock()
    monkeypatch.setattr(browser_launcher.subprocess, "Popen", popen)

    browser_launcher.BrowserLauncher(
        project_browser_args=lambda: runtime_args(crash_dir),
    ).launch_browser("/fake/chrome", 9222, headless=True, user_data_dir=str(tmp_path / "profile"))

    assert crash_dir.is_dir()
    assert popen.call_count == 1
    arguments = popen.call_args.args[0]
    start = arguments.index(runtime_args(crash_dir)[0])
    assert arguments[start:start + 3] == runtime_args(crash_dir)


def test_page_evidence_launch_creates_crash_directory_before_fake_launch(monkeypatch, tmp_path: Path) -> None:
    crash_dir = tmp_path / "chrome_crash_dumps"
    monkeypatch.setattr(browser_runtime, "CHROME_CRASH_DUMPS", crash_dir)
    monkeypatch.setattr(ctf_resource_crawl, "CHROME_CRASH_DUMPS", crash_dir)
    monkeypatch.setattr(ctf_resource_crawl, "profile_dir_for_target", lambda target: tmp_path / "profile")
    monkeypatch.setattr(ctf_resource_crawl, "discover_chrome_for_testing_path", lambda: None)
    monkeypatch.setattr(ctf_resource_crawl, "browser_launch_environment", lambda: {})
    chromium = FakeChromium()

    asyncio.run(ctf_resource_crawl.open_context(
        SimpleNamespace(chromium=chromium), {"site": "offline"}, SimpleNamespace(headless=True),
    ))

    assert crash_dir.is_dir()
    (kwargs,) = chromium.launches
    assert kwargs["args"][-3:] == runtime_args(crash_dir)


def test_zhihu_persistent_launch_creates_crash_directory_before_fake_launch(monkeypatch, tmp_path: Path) -> None:
    crash_dir = tmp_path / "chrome_crash_dumps"
    monkeypatch.setattr(zhihu_core, "CHROME_CRASH_DUMPS", crash_dir)
    monkeypatch.setattr(zhihu_core, "platform_profile_dir", lambda platform: str(tmp_path / "profile"))
    crawler = object.__new__(zhihu_core.ZhihuCrawler)
    crawler.settings = SimpleNamespace(SAVE_LOGIN_STATE=True)
    crawler.ports = SimpleNamespace(project_browser_args=lambda: runtime_args(crash_dir))
    chromium = FakeChromium()

    asyncio.run(crawler.launch_browser(chromium, None, None, headless=True))

    assert crash_dir.is_dir()
    (kwargs,) = chromium.launches
    assert kwargs["args"] == runtime_args(crash_dir)
