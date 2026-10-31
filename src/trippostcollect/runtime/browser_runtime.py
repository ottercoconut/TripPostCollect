"""Shared writable runtime settings for Chromium subprocesses."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trippostcollect.core.paths import BROWSER_RUNTIME_HOME, CHROME_CRASH_DUMPS, ensure_dir

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, BrowserType


XHS_NATIVE_WINDOW_SIZE = (1450, 900)
# #139：通用平台持久 context 与小红书共用同一原生窗口尺寸，不再用 viewport 做设备模拟。
NATIVE_WINDOW_SIZE = XHS_NATIVE_WINDOW_SIZE
XHS_WINDOW_SIZE_ENV = "TRIPPOSTCOLLECT_XHS_WINDOW_SIZE"


def browser_launch_environment() -> dict[str, str]:
    original_home = Path.home()
    env = os.environ.copy()
    env["HOME"] = str(ensure_dir(BROWSER_RUNTIME_HOME))
    playwright_cache = original_home / "Library" / "Caches" / "ms-playwright"
    if playwright_cache.is_dir():
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(playwright_cache))
    return env


def browser_runtime_args() -> list[str]:
    crash_dir = ensure_dir(CHROME_CRASH_DUMPS)
    return [
        "--disable-crash-reporter",
        f"--crash-dumps-dir={crash_dir}",
        "--use-mock-keychain",
    ]


def xhs_window_size_value() -> str:
    return f"{XHS_NATIVE_WINDOW_SIZE[0]},{XHS_NATIVE_WINDOW_SIZE[1]}"


async def launch_profile_context(
    chromium: BrowserType,
    profile_dir: Path | str,
    *,
    headless: bool,
    browser_path: str | None = None,
    native_window_size: tuple[int, int] = NATIVE_WINDOW_SIZE,
    runtime_args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> BrowserContext:
    """#136：warmup、通用平台 crawl 标准模式与 B站行为会话共用的持久 context 启动配置。

    沙箱开启、locale/timezone 与运行时环境三处一致；不传 UA，浏览器自报真实 UA。
    #139：不设 viewport、不做设备模拟，按原生窗口 ``native_window_size``（缺省 ``NATIVE_WINDOW_SIZE``，
    与小红书相同的 1450×900）启动；macOS 会把窗口收进屏幕可用区域，``screen``、DPR、``outer*``、
    ``inner*`` 均由浏览器按真实值给出。
    ``runtime_args`` / ``env`` 缺省时调用时取 ``browser_runtime_args()`` / ``browser_launch_environment()``；
    warmup 与 B站行为会话经各自已有的注入点求值后传入，取值与缺省相同。
    """
    if runtime_args is None:
        runtime_args = browser_runtime_args()
    if env is None:
        env = browser_launch_environment()
    kwargs: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "no_viewport": True,
        "accept_downloads": True,
        "ignore_https_errors": True,
        # 显式开启沙箱：Playwright 在 chromium_sandbox 不为 True 时会自动追加 --no-sandbox。
        "chromium_sandbox": True,
        "args": [
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            *runtime_args,
            f"--window-size={native_window_size[0]},{native_window_size[1]}",
        ],
        "env": env,
    }
    if browser_path:
        kwargs["executable_path"] = browser_path
    return await chromium.launch_persistent_context(**kwargs)
