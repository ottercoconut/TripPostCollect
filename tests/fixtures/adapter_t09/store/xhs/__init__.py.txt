# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/xhs/__init__.py
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
# @Time    : 2024/1/14 17:34
# @Desc    :
import json
import os
from typing import Any, Dict, List

import config
from var import source_keyword_var
from tools.image_manifest import ImageStagingError, xhs_source_asset_key
from tools.user_hash import anonymize_user_id, mask_nickname

from .xhs_store_media import *
from ._store_impl import *


def _xhs_image_assets(note_item: Dict) -> List[Dict]:
    """Choose one authoritative URL per XHS image object and deduplicate assets."""

    assets: List[Dict] = []
    seen = set()
    image_list = note_item.get("image_list") or []
    if not isinstance(image_list, list):
        return assets
    for image in image_list:
        if isinstance(image, str):
            url = image.strip()
        elif isinstance(image, dict):
            url = next(
                (
                    str(image.get(key) or "").strip()
                    for key in ("url_default", "url", "url_pre")
                    if str(image.get(key) or "").strip()
                ),
                "",
            )
        else:
            continue
        if not url:
            continue
        try:
            asset_key = xhs_source_asset_key(url)
        except ImageStagingError:
            continue
        if asset_key in seen:
            continue
        seen.add(asset_key)
        assets.append(
            {"url": url, "source_index": len(assets), "source_asset_key": asset_key}
        )
    return assets


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _nested_value(data: Dict, *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = data
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current not in (None, ""):
            return current
    return None


def _interaction_count(creator: Dict, *names: str) -> Any:
    expected = {name.lower() for name in names}
    interactions = (
        creator.get("interactions")
        or creator.get("interaction")
        or creator.get("interactionList")
        or creator.get("interaction_list")
        or []
    )
    if not isinstance(interactions, list):
        return None
    for item in interactions:
        if not isinstance(item, dict):
            continue
        item_type = str(_first_nonempty(item.get("type"), item.get("name"), item.get("key")) or "").lower()
        if item_type in expected or any(name in item_type for name in expected):
            return _first_nonempty(item.get("count"), item.get("num"), item.get("value"))
    return None


def _creator_basic_info(creator: Dict) -> Dict:
    basic = creator.get("basicInfo") or creator.get("basic_info") or creator.get("basic") or {}
    return basic if isinstance(basic, dict) else {}


def _creator_metric(creator: Dict, *keys: str) -> Any:
    basic = _creator_basic_info(creator)
    for key in keys:
        value = _first_nonempty(creator.get(key), basic.get(key))
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return value
    return _interaction_count(creator, *keys)


def _creator_profile_url(user_id: str, xsec_token: str = "", xsec_source: str = "pc_search") -> str:
    if not user_id:
        return ""
    url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
    if xsec_token:
        url += f"?xsec_token={xsec_token}&xsec_source={xsec_source or 'pc_search'}"
    return url


def _normalized_creator_item(user_id: str, creator: Dict) -> Dict:
    basic = _creator_basic_info(creator)
    return {
        "user_id": user_id or _first_nonempty(basic.get("userId"), basic.get("user_id")),
        "nickname": _first_nonempty(basic.get("nickname"), creator.get("nickname")),
        "avatar_url": _first_nonempty(basic.get("imageb"), basic.get("avatar"), basic.get("avatarUrl"), creator.get("avatar_url")),
        "author_desc": _first_nonempty(basic.get("desc"), basic.get("description"), creator.get("desc")),
        "gender": _first_nonempty(basic.get("gender"), creator.get("gender")),
        "ip_location": _first_nonempty(basic.get("ipLocation"), basic.get("ip_location"), creator.get("ip_location")),
        "fans": _creator_metric(creator, "fans", "粉丝"),
        "fans_count": _creator_metric(creator, "fans", "fansCount", "fans_count", "followerCount", "followers_count", "粉丝"),
        "follows": _creator_metric(creator, "follows", "关注"),
        "following_count": _creator_metric(creator, "follows", "followsCount", "following_count", "follow_count", "关注"),
        "note_count": _creator_metric(creator, "notes", "noteCount", "note_count", "posts_count", "笔记"),
        "interaction_count": _creator_metric(creator, "interaction", "interactions", "获赞与收藏"),
        "creator_profile_json": json.dumps(creator, ensure_ascii=False, sort_keys=True),
        "last_modify_ts": utils.get_current_timestamp(),
    }


class XhsStoreFactory:
    STORES = {
        "csv": XhsCsvStoreImplement,
        "db": XhsDbStoreImplement,
        "postgres": XhsDbStoreImplement,
        "json": XhsJsonStoreImplement,
        "jsonl": XhsJsonlStoreImplement,
        "sqlite": XhsSqliteStoreImplement,
        "mongodb": XhsMongoStoreImplement,
        "excel": XhsExcelStoreImplement,
    }

    @staticmethod
    def create_store() -> AbstractStore:
        store_class = XhsStoreFactory.STORES.get(config.SAVE_DATA_OPTION)
        if not store_class:
            raise ValueError("[XhsStoreFactory.create_store] Invalid save option only supported csv or db or json or sqlite or mongodb or excel ...")
        return store_class()


def get_video_url_arr(note_item: Dict) -> List:
    """
    Get video url array
    Args:
        note_item:

    Returns:

    """
    if note_item.get('type') != 'video':
        return []

    video_dict = note_item.get('video')
    if not video_dict:
        return []

    videoArr = []
    consumer = video_dict.get('consumer', {})
    originVideoKey = consumer.get('origin_video_key', '')
    if originVideoKey == '':
        originVideoKey = consumer.get('originVideoKey', '')
    # Fallback with watermark
    if originVideoKey == '':
        media = video_dict.get('media', {})
        stream = media.get('stream', {})
        videos = stream.get('h264')
        if type(videos).__name__ == 'list':
            videoArr = [v.get('master_url') for v in videos]
    else:
        videoArr = [f"http://sns-video-bd.xhscdn.com/{originVideoKey}"]

    return videoArr


async def update_xhs_note(note_item: Dict):
    """
    Update Xiaohongshu note
    Args:
        note_item:

    Returns:

    """
    note_id = note_item.get("note_id")
    user_info = note_item.get("user", {})
    interact_info = note_item.get("interact_info", {})
    image_assets = _xhs_image_assets(note_item)
    tag_list: List[Dict] = note_item.get("tag_list", [])
    creator_profile: Dict = note_item.get("creator_profile") or {}
    creator_item = _normalized_creator_item(user_info.get("user_id", ""), creator_profile) if creator_profile else {}
    followers_observed = any(
        creator_item.get(key) not in (None, "")
        for key in ("fans_count", "fans")
    )
    keep_author_detail = os.environ.get("TRIPPOSTCOLLECT_XHS_KEEP_AUTHOR_DETAIL") == "1"

    video_url = ""
    raw_user_id = _first_nonempty(user_info.get("user_id"), creator_item.get("user_id"))
    raw_nickname = _first_nonempty(user_info.get("nickname"), creator_item.get("nickname"))

    local_db_item = {
        "note_id": note_item.get("note_id"),  # Note ID
        "type": note_item.get("type"),  # Note type
        "title": note_item.get("title") or note_item.get("desc", "")[:255],  # Note title
        "desc": note_item.get("desc", ""),  # Note description
        "video_url": video_url,  # Note video url
        "time": note_item.get("time"),  # Note publish time
        "last_update_time": note_item.get("last_update_time", 0),  # Note last update time
        "creator_hash": anonymize_user_id(raw_user_id),  # Creator anonymous hash.
        "user_id": raw_user_id if keep_author_detail else "",
        "nickname": raw_nickname if keep_author_detail else mask_nickname(raw_nickname),
        "author_profile_url": _creator_profile_url(
            raw_user_id or "",
            note_item.get("xsec_token", ""),
            note_item.get("xsec_source", "pc_search"),
        ),
        "avatar_url": _first_nonempty(
            user_info.get("avatar"),
            user_info.get("image"),
            user_info.get("imageb"),
            creator_item.get("avatar_url"),
        ),
        "author_desc": creator_item.get("author_desc"),
        "gender": creator_item.get("gender"),
        "ip_location": creator_item.get("ip_location"),
        "fans": creator_item.get("fans"),
        "fans_count": creator_item.get("fans_count"),
        "followers_count": creator_item.get("fans_count") or creator_item.get("fans"),
        "followers_observed": followers_observed,
        "author_followers_source": "creator_profile" if followers_observed else "missing",
        "follows": creator_item.get("follows"),
        "following_count": creator_item.get("following_count") or creator_item.get("follows"),
        "note_count": creator_item.get("note_count"),
        "interaction_count": creator_item.get("interaction_count"),
        "liked_count": interact_info.get("liked_count"),  # Like count
        "collected_count": interact_info.get("collected_count"),  # Collection count
        "comment_count": interact_info.get("comment_count"),  # Comment count
        "share_count": interact_info.get("share_count"),  # Share count
        "image_list": ','.join(asset["url"] for asset in image_assets),  # Image URLs
        "image_assets": image_assets,
        "image_list_source": "note_detail.image_list",
        "tag_list": ','.join([tag.get('name', '') for tag in tag_list if tag.get('type') == 'topic']),  # Tags
        "last_modify_ts": utils.get_current_timestamp(),  # Last modification timestamp (Generated by MediaCrawler, mainly used to record the latest update time of a record in DB storage)
        "note_url": f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={note_item.get('xsec_token')}&xsec_source=pc_search",  # Note URL
        "source_keyword": source_keyword_var.get(),  # Search keyword
        "xsec_token": note_item.get("xsec_token"),  # xsec_token
        "creator_profile_json": creator_item.get("creator_profile_json", ""),
    }
    if config.SAVE_DATA_OPTION == "jsonl":
        local_db_item.update(
            {
                "content_detail_status": note_item.get(
                    "content_detail_status", "unobserved"
                ),
                "content_detail_source": note_item.get("content_detail_source", ""),
            }
        )
    utils.logger.info(f"[store.xhs.update_xhs_note] xhs note: {local_db_item}")
    await XhsStoreFactory.create_store().store_content(local_db_item)


async def batch_update_xhs_note_comments(note_id: str, comments: List[Dict]):
    """
    Batch update Xiaohongshu note comments
    Args:
        note_id:
        comments:

    Returns:

    """
    if not comments:
        return
    for comment_item in comments:
        await update_xhs_note_comment(note_id, comment_item)


async def update_xhs_note_comment(note_id: str, comment_item: Dict):
    """
    Update Xiaohongshu note comment
    Args:
        note_id:
        comment_item:

    Returns:

    """
    user_info = comment_item.get("user_info", {})
    comment_id = comment_item.get("id")
    comment_pictures = [item.get("url_default", "") for item in comment_item.get("pictures", [])]
    target_comment = comment_item.get("target_comment", {})
    local_db_item = {
        "comment_id": comment_id,  # Comment ID
        "create_time": comment_item.get("create_time"),  # Comment time
        "note_id": note_id,  # Note ID
        "content": comment_item.get("content"),  # Comment content
        "creator_hash": anonymize_user_id(user_info.get("user_id")),  # 创作者匿名哈希(不存原始 user_id)
        "nickname": mask_nickname(user_info.get("nickname")),  # 用户昵称(已脱敏)
        "sub_comment_count": comment_item.get("sub_comment_count", 0),  # Sub-comment count
        "pictures": ",".join(comment_pictures),  # Comment pictures
        "parent_comment_id": target_comment.get("id", ""),  # Parent comment ID
        "last_modify_ts": utils.get_current_timestamp(),  # Last modification timestamp (Generated by MediaCrawler, mainly used to record the latest update time of a record in DB storage)
        "like_count": comment_item.get("like_count", 0),
    }
    utils.logger.info(f"[store.xhs.update_xhs_note_comment] xhs note comment:{local_db_item}")
    await XhsStoreFactory.create_store().store_comment(local_db_item)


async def save_creator(user_id: str, creator: Dict):
    """
    Save Xiaohongshu creator
    Args:
        user_id:
        creator:

    Returns:

    """
    creator_item = _normalized_creator_item(user_id, creator)
    creator_item["author_profile_url"] = _creator_profile_url(str(creator_item.get("user_id") or user_id))
    utils.logger.info(f"[store.xhs.save_creator] xhs creator: {creator_item}")
    await XhsStoreFactory.create_store().store_creator(creator_item)


async def update_xhs_note_images(note_id: str, image_content_items: List[Dict]):
    """Atomically save all body images for one XHS note."""

    return await XiaoHongShuImage().store_post_images(note_id, image_content_items)


async def record_xhs_note_image_failure(note_id: str, image_content_item: Dict):
    """Write one failed manifest row without success file metadata."""

    return await XiaoHongShuImage().record_failure(note_id, image_content_item)


async def update_xhs_note_video(note_id, video_content, extension_file_name):
    """
    Update Xiaohongshu note video
    Args:
        note_id:
        video_content:
        extension_file_name:

    Returns:

    """

    await XiaoHongShuVideo().store_video({"notice_id": note_id, "video_content": video_content, "extension_file_name": extension_file_name})
