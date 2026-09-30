#!/usr/bin/env python3
"""登录命令入口，转发到包内编排。"""

from pathlib import Path
from trippostcollect.core import paths
from trippostcollect.core.paths import COOKIE_SNAPSHOT_FILENAME as COOKIE_SNAPSHOT_FILENAME
from trippostcollect.application.warmup import (
    media_parse_args as parse_args,
    media_utc_stamp as utc_stamp,
    selected_platforms as selected_platforms,
    cookie_dict as cookie_dict,
    cookie_snapshot_info as cookie_snapshot_info,
    write_cookie_snapshot as write_cookie_snapshot,
    browser_path_for as browser_path_for,
    launch_login_context as launch_login_context,
    safe_local_storage as safe_local_storage,
    zhihu_api_check as zhihu_api_check,
    weibo_api_check as weibo_api_check,
    current_state as current_state,
    weibo_desktop_login_state as weibo_desktop_login_state,
    weibo_desktop_login_completed as weibo_desktop_login_completed,
    warmup_one as warmup_one,
    media_main_async as main_async,
    PLATFORMS as PLATFORMS,
    MEDIACRAWLER_ALIASES as ALIASES,
    discover_cdp_browser_path as discover_cdp_browser_path,
    required_cookie_names as required_cookie_names,
    ROOT as ROOT,
    DEFAULT_OUTPUT as DEFAULT_OUTPUT,
)
from trippostcollect.application.warmup import media_main

__all__ = [
    "parse_args", "utc_stamp", "selected_platforms", "profile_dir_for",
    "cookie_dict", "required_cookie_names", "cookie_snapshot_path",
    "cookie_snapshot_info", "write_cookie_snapshot", "browser_path_for",
    "launch_login_context", "safe_local_storage", "zhihu_api_check",
    "weibo_api_check", "current_state", "weibo_desktop_login_state",
    "weibo_desktop_login_completed", "warmup_one", "main_async", "main",
    "PLATFORMS", "ALIASES", "ROOT", "DEFAULT_OUTPUT", "COOKIE_SNAPSHOT_FILENAME",
    "discover_cdp_browser_path",
]


def profile_dir_for(platform_key: str) -> Path:
    return paths.platform_profile_dir(platform_key)


def cookie_snapshot_path(platform_key: str) -> Path:
    return paths.platform_cookie_snapshot_path(platform_key)


def main() -> int:
    return media_main()


if __name__ == "__main__":
    raise SystemExit(main())
