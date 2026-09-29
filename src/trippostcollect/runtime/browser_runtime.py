"""Shared writable runtime settings for Chromium subprocesses."""

from __future__ import annotations

import os
from pathlib import Path

from trippostcollect.core.paths import BROWSER_RUNTIME_HOME, CHROME_CRASH_DUMPS, ensure_dir


XHS_NATIVE_WINDOW_SIZE = (1450, 900)
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
