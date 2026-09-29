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
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from trippostcollect.core.paths import platform_cookie_snapshot_path as cookie_snapshot_path

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
