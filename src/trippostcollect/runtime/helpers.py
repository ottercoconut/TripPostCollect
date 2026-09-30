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


"""运行时间戳、监督进度回调与 UA、HTML、URL 纯转换。"""

from __future__ import annotations

import random
import re
import time
import urllib.parse
from urllib.parse import urlsplit, urlunsplit

from trippostcollect.application.contracts import ImageStagingError
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f%z")


def _runtime_progress(callback: Callable[[], object] | None) -> None:
    if callback is not None:
        callback()


def _runtime_progress_if_due(
    callback: Callable[[], object] | None,
    last_checkpoint_at: float,
    *,
    interval_seconds: float = 5.0,
) -> float:
    if callback is None:
        return last_checkpoint_at
    now = time.monotonic()
    if now - last_checkpoint_at < interval_seconds:
        return last_checkpoint_at
    callback()
    return time.monotonic()


def get_user_agent(rng: random.Random | None = None) -> str:
    ua_list = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.5112.79 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.5060.53 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.84 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.5112.79 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.5060.53 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.4844.84 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5112.79 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.5060.53 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.4844.84 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.5112.79 Safari/537.36"
    ]
    return (random if rng is None else rng).choice(ua_list)


def get_mobile_user_agent(rng: random.Random | None = None) -> str:
    ua_list = [
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
    ]
    return (random if rng is None else rng).choice(ua_list)


def extract_text_from_html(html: str) -> str:
    """Extract text from HTML, removing all tags."""
    if not html:
        return ""

    # Remove script and style elements
    clean_html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL)
    # Remove all other tags
    clean_text = re.sub(r'<[^>]+>', '', clean_html).strip()
    return clean_text


def extract_url_params_to_dict(url: str) -> Dict:
    """Extract URL parameters to dict"""
    url_params_dict = dict()
    if not url:
        return url_params_dict
    parsed_url = urllib.parse.urlparse(url)
    url_params_dict = dict(urllib.parse.parse_qsl(parsed_url.query))
    return url_params_dict


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_image_url(value: str) -> str:
    text = str(value or "").strip().rstrip("\t\r\n ).];,，")
    if text.startswith("//"):
        text = f"https:{text}"
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise ImageStagingError("image_manifest_identity_mismatch", "invalid source URL") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ImageStagingError("image_manifest_identity_mismatch", "invalid source URL")
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, "")
    )



# TripPostCollect T05：迁自 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303；仅拆分职责与注入依赖。
def _find_nested_platform_record(
    value: Any,
    target_id: str,
    *,
    id_keys: tuple[str, ...],
    shape_keys: tuple[str, ...],
    depth: int = 0,
) -> dict[str, Any] | None:
    if depth > 12:
        return None
    if isinstance(value, dict):
        identity = next(
            (
                str(value.get(key) or "")
                for key in id_keys
                if value.get(key) not in (None, "")
            ),
            "",
        )
        if identity == str(target_id) and any(key in value for key in shape_keys):
            return value
        for nested in value.values():
            found = _find_nested_platform_record(
                nested,
                target_id,
                id_keys=id_keys,
                shape_keys=shape_keys,
                depth=depth + 1,
            )
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_nested_platform_record(
                nested,
                target_id,
                id_keys=id_keys,
                shape_keys=shape_keys,
                depth=depth + 1,
            )
            if found is not None:
                return found
    return None

# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/time_util.py
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


# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2023/12/2 12:52
# @Desc    : Time utility functions


# TripPostCollect T05：迁自 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303；仅拆分职责与注入依赖。


def get_current_timestamp() -> int:
    """
    Get current timestamp (13 digits): 1701493264496
    :return:
    """
    return int(time.time() * 1000)


def rfc2822_to_china_datetime(rfc2822_time):
    # Define RFC 2822 format
    rfc2822_format = "%a %b %d %H:%M:%S %z %Y"

    # Convert RFC 2822 time string to datetime object
    dt_object = datetime.strptime(rfc2822_time, rfc2822_format)

    # Convert datetime object timezone to China timezone
    dt_object_china = dt_object.astimezone(timezone(timedelta(hours=8)))
    return dt_object_china


def rfc2822_to_timestamp(rfc2822_time):
    # Define RFC 2822 format
    rfc2822_format = "%a %b %d %H:%M:%S %z %Y"

    # Convert RFC 2822 time string to datetime object
    dt_object = datetime.strptime(rfc2822_time, rfc2822_format)

    # Convert datetime object to UTC time
    dt_utc = dt_object.astimezone(timezone.utc)

    # Calculate Unix timestamp from UTC time
    timestamp = int(dt_utc.timestamp())

    return timestamp
