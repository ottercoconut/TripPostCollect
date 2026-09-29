# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/weibo/__init__.py
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
# @Time    : 2024/1/14 21:34
# @Desc    :


# TripPostCollect T05：迁自 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303；仅拆分职责与注入依赖。
import re
from hashlib import sha256
from typing import Any, Dict, List
from urllib.parse import urlsplit

from trippostcollect.records.identity import anonymize_user_id, mask_nickname
from trippostcollect.runtime.helpers import (
    _find_nested_platform_record, normalize_image_url,
    rfc2822_to_timestamp, rfc2822_to_china_datetime,
)

def _first_present(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _weibo_pic_url(pic):
    if isinstance(pic, str):
        return pic
    if not isinstance(pic, dict):
        return None
    for key in ("url", "large", "bmiddle", "middleplus", "thumbnail"):
        value = pic.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            nested_url = value.get("url")
            if nested_url:
                return nested_url
    return None


def _weibo_pic_urls(mblog: Dict) -> List[str]:
    urls: List[str] = []
    seen = set()
    pics = mblog.get("pics") or []
    if not isinstance(pics, list):
        return urls
    for pic in pics:
        url = _weibo_pic_url(pic)
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _weibo_pic_assets(mblog: Dict) -> List[Dict]:
    """Return authoritative body-image metadata in the same order as image_list."""

    assets: List[Dict] = []
    seen = set()
    pics = mblog.get("pics") or []
    if not isinstance(pics, list):
        return assets
    for pic in pics:
        url = _weibo_pic_url(pic)
        if not url or url in seen:
            continue
        seen.add(url)
        pid = ""
        if isinstance(pic, dict):
            pid = str(pic.get("pid") or pic.get("picture_id") or "").strip()
        assets.append(
            {
                "pid": pid,
                "url": url,
                "source_index": len(assets),
            }
        )
    return assets


def persisted_weibo_content_text(mblog: Dict) -> str:
    """Project the authoritative body exactly as it is persisted for ``web_posts``."""

    return re.sub(r"<.*?>", "", str(mblog.get("text") or ""))


def update_weibo_note(
    note_item: Dict, *, source_keyword: str, current_timestamp, save_data_option: str = "jsonl",
):
    """
    Update weibo note
    Args:
        note_item:

    Returns:

    """
    if not note_item:
        return

    mblog: Dict = note_item.get("mblog") or {}
    user_info: Dict = mblog.get("user") or {}
    note_id = mblog.get("id")
    clean_text = persisted_weibo_content_text(mblog)
    image_assets = _weibo_pic_assets(mblog)
    image_list = [asset["url"] for asset in image_assets]
    followers_count = _first_present(
        user_info.get("followers_count"),
        user_info.get("followers_count_str"),
        user_info.get("fans_count"),
        user_info.get("fans_count_str"),
    )
    followers_observed = any(
        key in user_info and user_info.get(key) not in (None, "")
        for key in ("followers_count", "followers_count_str", "fans_count", "fans_count_str")
    )
    # 教学版：原始 user_id 匿名化为 creator_hash，昵称脱敏；
    # 不采集头像/主页链接/性别/IP 归属地等可定位真人的信息。
    save_content_item = {
        # Weibo information
        "note_id": note_id,
        "content": clean_text,
        "create_time": rfc2822_to_timestamp(mblog.get("created_at")),
        "create_date_time": str(rfc2822_to_china_datetime(mblog.get("created_at"))),
        "liked_count": str(mblog.get("attitudes_count", 0)),
        "comments_count": str(mblog.get("comments_count", 0)),
        "shared_count": str(mblog.get("reposts_count", 0)),
        "last_modify_ts": current_timestamp(),
        "note_url": f"https://m.weibo.cn/detail/{note_id}",
        "image_list": image_list,
        "image_count": len(image_list),
        "image_list_source": "mblog.pics",
        "image_assets": image_assets,

        # 创作者信息（匿名化/脱敏，不含原始 user_id/avatar/gender/profile_url/ip_location）
        "creator_hash": anonymize_user_id(user_info.get("id")),
        "nickname": mask_nickname(user_info.get("screen_name", "")),
        "followers_count": followers_count,
        "fans_count": followers_count,
        "followers_observed": followers_observed,
        "author_followers_source": "search_author" if followers_observed else "missing",
        "source_keyword": source_keyword,
    }
    if save_data_option == "jsonl":
        save_content_item.update(
            {
                "content_detail_status": mblog.get(
                    "content_detail_status", "unobserved"
                ),
                "content_detail_source": mblog.get("content_detail_source", ""),
            }
        )
    return save_content_item

# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/weibo/help.py
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
# @Time    : 2023/12/24 17:37
# @Desc    :


# TripPostCollect T05：迁自 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303；仅拆分职责与注入依赖。
def filter_search_result_card(card_list: List[Dict]) -> List[Dict]:
    """
    Filter Weibo search results, only keep data with card_type of 9
    :param card_list: List of card items from search results
    :return: Filtered list of note items
    """
    note_list: List[Dict] = []
    for card_item in card_list:
        if card_item.get("card_type") == 9:
            note_list.append(card_item)
        if len(card_item.get("card_group", [])) > 0:
            card_group = card_item.get("card_group")
            for card_group_item in card_group:
                if card_group_item.get("card_type") == 9:
                    note_list.append(card_group_item)

    return note_list


def _find_weibo_detail(value: Any, note_id: str) -> dict[str, Any] | None:
    return _find_nested_platform_record(
        value,
        note_id,
        id_keys=("id", "idstr", "mid"),
        shape_keys=("text", "pics", "user", "created_at", "isLongText"),
    )


def weibo_source_asset_key(pid: str | None, source_url: str) -> str:
    if pid not in (None, ""):
        return f"weibo:pid:{str(pid).strip()}"
    normalized = normalize_image_url(source_url)
    parsed = urlsplit(normalized)
    digest = sha256(f"{parsed.hostname.lower()}{parsed.path}".encode("utf-8")).hexdigest()
    return f"weibo:urlsha256:{digest}"
