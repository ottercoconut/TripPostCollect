# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/crawler_util.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。


"""Cookie 转换与通用登录快照读取。"""

from __future__ import annotations

import json
import time
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from trippostcollect.core.paths import (
    ensure_parent,
    platform_cookie_snapshot_path as cookie_snapshot_path,
    platform_profile_dir as profile_dir_for, PROJECT_ROOT as ROOT,
)
from trippostcollect.runtime.browser_runtime import browser_runtime_args, browser_launch_environment

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Cookie


def convert_cookies(cookies: Optional[List[Cookie]]) -> Tuple[str, Dict]:
    if not cookies:
        return "", {}
    cookies_str = ";".join([f"{cookie.get('name')}={cookie.get('value')}" for cookie in cookies])
    cookie_dict = dict()
    for cookie in cookies:
        cookie_dict[cookie.get('name')] = cookie.get('value')
    return cookies_str, cookie_dict


async def convert_browser_context_cookies(
    browser_context: BrowserContext, urls: Optional[List[str]] = None
) -> Tuple[str, Dict]:
    cookies = (
        await browser_context.cookies(urls=urls)
        if urls
        else await browser_context.cookies()
    )
    return convert_cookies(cookies)


def convert_str_cookie_to_dict(cookie_str: str) -> Dict:
    cookie_dict: Dict[str, str] = dict()
    if not cookie_str:
        return cookie_dict
    for cookie in cookie_str.split(";"):
        cookie = cookie.strip()
        if not cookie:
            continue
        cookie_list = cookie.split("=")
        if len(cookie_list) != 2:
            continue
        cookie_value = cookie_list[1]
        if isinstance(cookie_value, list):
            cookie_value = "".join(cookie_value)
        cookie_dict[cookie_list[0]] = cookie_value
    return cookie_dict


def platform_cookie_url(platform_key: str) -> str:
    return {
        "bilibili": "https://www.bilibili.com/",
        "weibo": "https://m.weibo.cn/",
        "xhs": "https://www.xiaohongshu.com/",
        "douyin": "https://www.douyin.com/",
        "zhihu": "https://www.zhihu.com/",
    }[platform_key]


def required_cookie_names(platform_key: str) -> tuple[str, ...]:
    if platform_key == "zhihu":
        return ("d_c0", "z_c0")
    return ()


def cookie_names_from_header(cookie_header: str) -> list[str]:
    names = []
    for item in cookie_header.split(";"):
        if "=" not in item:
            continue
        name = item.split("=", 1)[0].strip()
        if name:
            names.append(name)
    return sorted(set(names))


def cookies_to_header(cookies: list[dict[str, Any]]) -> str:
    pairs = []
    now = time.time()
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        if not name or value in (None, ""):
            continue
        expires = item.get("expires")
        if isinstance(expires, (int, float)) and expires > 0 and expires < now:
            continue
        pairs.append(f"{name}={value}")
    return ";".join(pairs)


def load_cookie_snapshot(platform_key: str) -> dict[str, Any] | None:
    path = cookie_snapshot_path(platform_key)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cookies = payload.get("cookies")
    if not isinstance(cookies, list):
        return None
    cookie_header = cookies_to_header(cookies)
    names = cookie_names_from_header(cookie_header)
    missing = [name for name in required_cookie_names(platform_key) if name not in names]
    if missing:
        return None
    return {
        "cookie_header": cookie_header,
        "source": "snapshot",
        "snapshot_path": str(path),
        "saved_at": payload.get("saved_at"),
        "cookie_names": names,
        "required_cookie_names": list(required_cookie_names(platform_key)),
    }


def public_cookie_export(cookie_export: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in cookie_export.items() if key != "cookie_header"}


# T07：原执行器 Cookie 导出，子进程与 snapshot 优先级保持不变。
def export_profile_cookies(platform_key: str, browser_path: str | None) -> dict[str, Any] | None:
    snapshot = load_cookie_snapshot(platform_key)
    if snapshot:
        return snapshot

    profile_dir = profile_dir_for(platform_key)
    if not profile_dir.exists():
        return None
    script = r"""
import asyncio
import json
import sys
from playwright.async_api import async_playwright

async def main() -> int:
    profile_dir = sys.argv[1]
    executable_path = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
    target_url = sys.argv[3]
    runtime_args = json.loads(sys.argv[4])
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=True,
            executable_path=executable_path,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=["--disable-dev-shm-usage", "--no-sandbox", *runtime_args],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        cookies = await context.cookies([target_url])
        await context.close()
        sys.stdout.write(";".join(f"{item['name']}={item.get('value', '')}" for item in cookies))
    return 0

raise SystemExit(asyncio.run(main()))
"""
    cmd = [sys.executable, "-c", script, str(profile_dir)]
    if browser_path:
        cmd.append(browser_path)
    else:
        cmd.append("")
    cmd.append(platform_cookie_url(platform_key))
    cmd.append(json.dumps(browser_runtime_args()))
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
            env=browser_launch_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    cookie_str = result.stdout.strip()
    names = cookie_names_from_header(cookie_str)
    missing = [name for name in required_cookie_names(platform_key) if name not in names]
    if result.returncode != 0 or missing:
        return None
    return {
        "cookie_header": cookie_str,
        "source": "live_profile",
        "snapshot_path": str(cookie_snapshot_path(platform_key)),
        "saved_at": None,
        "cookie_names": names,
        "required_cookie_names": list(required_cookie_names(platform_key)),
    }


def cookie_dict(cookies: list[dict[str, Any]]) -> dict[str, str]:
    return {item["name"]: item.get("value", "") for item in cookies}



def cookie_snapshot_info(path: Path, cookies: list[dict[str, Any]], saved_at: str) -> dict[str, Any]:
    return {
        "path": str(path),
        "saved_at": saved_at,
        "cookie_names": sorted({item["name"] for item in cookies if item.get("name")}),
    }



def write_cookie_snapshot(
    platform_key: str,
    snapshot_path: Path,
    cookies: list[dict[str, Any]],
    *,
    source: str,
    label: str,
    urls: list[str],
    state: dict[str, Any],
) -> dict[str, Any] | None:
    cookie_values = cookie_dict(cookies)
    missing = [name for name in required_cookie_names(platform_key) if not cookie_values.get(name)]
    if missing:
        return None
    saved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # T14：快照与 profile 同属 <platform>/ 目录，路径由调用方经 platform_cookie_snapshot_path 给出。
    ensure_parent(snapshot_path)
    payload = {
        "platform": platform_key,
        "label": label,
        "saved_at": saved_at,
        "source": source,
        "urls": urls,
        "required_cookie_names": list(required_cookie_names(platform_key)),
        "state_markers": state.get("markers") if isinstance(state, dict) else {},
        "cookies": cookies,
    }
    snapshot_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        snapshot_path.chmod(0o600)
    except OSError:
        pass
    return cookie_snapshot_info(snapshot_path, cookies, saved_at)
