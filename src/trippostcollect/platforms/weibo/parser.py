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
import html
import re
from hashlib import sha256
from typing import Any, Dict, List
from urllib.parse import urlsplit

from trippostcollect.records.identity import platform_nickname, platform_user_id
from trippostcollect.runtime.helpers import (
    _find_nested_platform_record, normalize_image_url,
    rfc2822_to_timestamp, rfc2822_to_china_datetime,
)

_WEIBO_BR = re.compile(r"<br\s*/?>", re.I)

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

    # 段落换行先落成 "\n" 再去标签，最后还原 HTML 实体（&amp;、&quot; 等）。
    text = _WEIBO_BR.sub("\n", str(mblog.get("text") or ""))
    return html.unescape(re.sub(r"<.*?>", "", text))


def weibo_created_at(mblog: Dict) -> tuple[int, str] | None:
    """平台原始发帖时间（RFC 2822）换算的时间戳与上海时区时间；缺失或格式异常返回 None。"""

    created_at = mblog.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip():
        return None
    try:
        return (
            rfc2822_to_timestamp(created_at),
            str(rfc2822_to_china_datetime(created_at)),
        )
    except ValueError:
        return None


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
    created = weibo_created_at(mblog)
    if created is None:
        # 发帖时间只能来自平台原值；缺失或无法解析时不写出该候选（#114）。
        return
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
    # 作者平台 ID（creator_hash 键）与昵称保存平台原始值；头像、性别、IP 归属地等仍不采集。
    save_content_item = {
        # Weibo information
        "note_id": note_id,
        "content": clean_text,
        "create_time": created[0],
        "create_date_time": created[1],
        "liked_count": str(mblog.get("attitudes_count", 0)),
        "comments_count": str(mblog.get("comments_count", 0)),
        "shared_count": str(mblog.get("reposts_count", 0)),
        "last_modify_ts": current_timestamp(),
        "note_url": f"https://m.weibo.cn/detail/{note_id}",
        "image_list": image_list,
        "image_count": len(image_list),
        "image_list_source": "mblog.pics",
        "image_assets": image_assets,

        # 创作者信息：原始用户 ID 与昵称；不含 avatar/gender/profile_url/ip_location
        "creator_hash": platform_user_id(user_info.get("id")),
        "nickname": platform_nickname(user_info.get("screen_name", "")),
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
    for card_item in card_list or []:
        if not isinstance(card_item, dict):
            continue
        if card_item.get("card_type") == 9:
            note_list.append(card_item)
        card_group = card_item.get("card_group")
        if isinstance(card_group, list):
            for card_group_item in card_group:
                if isinstance(card_group_item, dict) and card_group_item.get("card_type") == 9:
                    note_list.append(card_group_item)

    return note_list


# 平台在搜索结果末尾给出的明确无内容提示（卡片或卡片组内的文本）。
WEIBO_EMPTY_RESULT_MARKERS = ("暂无内容", "这里还没有内容")


def _card_texts(card: Any) -> List[str]:
    if not isinstance(card, dict):
        return []
    texts = [str(card.get(key) or "") for key in ("desc", "desc1", "title_sub", "name")]
    for nested in card.get("card_group") or []:
        texts.extend(_card_texts(nested))
    return texts


def search_page_exhausted(card_list: List[Dict]) -> bool:
    """只有原始 ``cards`` 为空，或页面没有微博卡片且带明确的无内容标记时才算来源耗尽。

    有卡片但都不是 ``card_type=9``（推荐、话题、用户卡）是正常页，应继续翻页。
    """
    cards = card_list or []
    if not cards:
        return True
    if filter_search_result_card(cards):
        return False
    return any(
        marker in text
        for card in cards
        for text in _card_texts(card)
        for marker in WEIBO_EMPTY_RESULT_MARKERS
    )


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
