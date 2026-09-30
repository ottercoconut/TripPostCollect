# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/douyin/__init__.py
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
# @Time    : 2024/1/14 18:46
# @Desc    :
from typing import Any, List

import config
from var import source_keyword_var
from tools.image_manifest import ImageStagingError, douyin_source_asset_key, normalize_image_url
from tools.user_hash import anonymize_user_id, mask_nickname

from ._store_impl import *
from .douyin_store_media import *


def _first_nonempty(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _nested_value(data: Dict, *paths):
    for path in paths:
        current = data
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current not in (None, ""):
            return current
    return None


def _creator_user_profile(creator_profile: Dict) -> Dict:
    user = (
        creator_profile.get("user")
        or creator_profile.get("user_info")
        or creator_profile.get("author")
        or creator_profile.get("profile")
        or creator_profile
        or {}
    )
    return user if isinstance(user, dict) else {}


def _author_metric(author: Dict, creator_profile: Dict, *keys: str):
    profile_user = _creator_user_profile(creator_profile)
    candidates = []
    for key in keys:
        value = _first_nonempty(author.get(key), profile_user.get(key), creator_profile.get(key))
        if value not in (None, "") and not isinstance(value, (dict, list)):
            candidates.append(value)
    expected = {key.lower() for key in keys}

    def walk(data):
        if isinstance(data, dict):
            for key, value in data.items():
                if str(key).lower() in expected and value not in (None, "") and not isinstance(value, (dict, list)):
                    candidates.append(value)
                elif isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(data, list):
            for item in data:
                walk(item)

    walk(creator_profile)
    for value in candidates:
        if str(value) != "0":
            return value
    return candidates[0] if candidates else None


def _normalized_author_stats(author: Dict, creator_profile: Dict) -> Dict:
    follower_keys = (
        "follower_count",
        "followers_count",
        "mplatform_followers_count",
        "fans_count",
        "fans",
    )
    followers_count = _author_metric(
        author,
        creator_profile,
        *follower_keys,
    )
    expected = {key.lower() for key in follower_keys}

    def observed(data: Any) -> bool:
        if isinstance(data, dict):
            for key, value in data.items():
                if str(key).lower() in expected and value not in (None, "") and not isinstance(value, (dict, list)):
                    return True
                if isinstance(value, (dict, list)) and observed(value):
                    return True
        elif isinstance(data, list):
            return any(observed(item) for item in data)
        return False

    profile_observed = observed(creator_profile)
    search_observed = observed(author)
    followers_observed = profile_observed or search_observed
    return {
        "followers_count": followers_count,
        "fans_count": followers_count,
        "following_count": _author_metric(author, creator_profile, "following_count", "follow_count", "follows"),
        "aweme_count": _author_metric(author, creator_profile, "aweme_count", "post_count", "posts_count", "video_count"),
        "author_liked_count": _author_metric(author, creator_profile, "total_favorited", "favorited_count", "liked_count"),
        "followers_observed": followers_observed,
        "author_followers_source": "creator_profile" if profile_observed else ("search_author" if search_observed else "missing"),
        "author_followers_zero_suspicious": str(followers_count) == "0" and not profile_observed,
    }


class DouyinStoreFactory:
    STORES = {
        "csv": DouyinCsvStoreImplement,
        "db": DouyinDbStoreImplement,
        "postgres": DouyinDbStoreImplement,
        "json": DouyinJsonStoreImplement,
        "jsonl": DouyinJsonlStoreImplement,
        "sqlite": DouyinSqliteStoreImplement,
        "mongodb": DouyinMongoStoreImplement,
        "excel": DouyinExcelStoreImplement,
    }

    @staticmethod
    def create_store() -> AbstractStore:
        store_class = DouyinStoreFactory.STORES.get(config.SAVE_DATA_OPTION)
        if not store_class:
            raise ValueError("[DouyinStoreFactory.create_store] Invalid save option only supported csv or db or json or sqlite or mongodb or excel ...")
        return store_class()


def _extract_note_image_list(aweme_detail: Dict) -> List[str]:
    """
    Extract note image list

    Args:
        aweme_detail (Dict): Douyin content details

    Returns:
        List[str]: Note image list
    """
    return [asset["url"] for asset in _extract_note_image_assets(aweme_detail)]


def _extract_note_image_assets(aweme_detail: Dict) -> List[Dict]:
    """Choose one fresh signed URL per note image and preserve its stable URI."""

    assets: List[Dict] = []
    seen = set()
    images: List[Dict] = aweme_detail.get("images", [])
    if not isinstance(images, list):
        return []
    for image in images:
        if not isinstance(image, dict):
            continue
        image_url_list = image.get("url_list") or []
        if not isinstance(image_url_list, list):
            continue
        url = next((str(value).strip() for value in reversed(image_url_list) if value), "")
        try:
            url = normalize_image_url(url)
        except ImageStagingError:
            continue
        uri = _first_nonempty(
            image.get("uri"),
            image.get("image_uri"),
            _nested_value(image, ("display_image", "uri"), ("origin_image", "uri")),
        )
        asset_key = douyin_source_asset_key(uri, url)
        if asset_key in seen:
            continue
        seen.add(asset_key)
        assets.append(
            {
                "uri": str(uri or ""),
                "url": url,
                "source_index": len(assets),
                "source_asset_key": asset_key,
            }
        )
    return assets


def _extract_comment_image_list(comment_item: Dict) -> List[str]:
    """
    Extract comment image list

    Args:
        comment_item (Dict): Douyin comment

    Returns:
        List[str]: Comment image list
    """
    images_res: List[str] = []
    image_list: List[Dict] = comment_item.get("image_list", [])

    if not image_list:
        return []

    for image in image_list:
        image_url_list = image.get("origin_url", {}).get("url_list", [])
        if image_url_list and len(image_url_list) > 1:
            images_res.append(image_url_list[1])

    return images_res


def _extract_content_cover_url(aweme_detail: Dict) -> str:
    """
    Extract video cover URL

    Args:
        aweme_detail (Dict): Douyin content details

    Returns:
        str: Video cover URL
    """
    res_cover_url = ""

    video_item = aweme_detail.get("video", {})
    raw_cover_url_list = (video_item.get("raw_cover", {}) or video_item.get("origin_cover", {})).get("url_list", [])
    if raw_cover_url_list and len(raw_cover_url_list) > 1:
        res_cover_url = raw_cover_url_list[1]

    return res_cover_url


def _extract_video_download_url(aweme_detail: Dict) -> str:
    """
    Extract video download URL

    Args:
        aweme_detail (Dict): Douyin video

    Returns:
        str: Video download URL
    """
    video_item = aweme_detail.get("video", {})
    url_h264_list = video_item.get("play_addr_h264", {}).get("url_list", [])
    url_256_list = video_item.get("play_addr_256", {}).get("url_list", [])
    url_list = video_item.get("play_addr", {}).get("url_list", [])
    actual_url_list = url_h264_list or url_256_list or url_list
    if not actual_url_list or len(actual_url_list) < 2:
        return ""
    return actual_url_list[-1]


def _extract_music_download_url(aweme_detail: Dict) -> str:
    """
    Extract music download URL

    Args:
        aweme_detail (Dict): Douyin video

    Returns:
        str: Music download URL
    """
    music_item = aweme_detail.get("music", {})
    play_url = music_item.get("play_url", {})
    music_url = play_url.get("uri", "")
    return music_url


async def update_douyin_aweme(aweme_item: Dict):
    aweme_id = aweme_item.get("aweme_id")
    user_info = aweme_item.get("author", {})
    creator_profile = aweme_item.get("creator_profile") or {}
    author_stats = _normalized_author_stats(user_info, creator_profile)
    interact_info = aweme_item.get("statistics", {})
    image_assets = _extract_note_image_assets(aweme_item)
    save_content_item = {
        "aweme_id": aweme_id,
        "aweme_type": str(aweme_item.get("aweme_type")),
        "title": aweme_item.get("desc", ""),
        "desc": aweme_item.get("desc", ""),
        "create_time": aweme_item.get("create_time"),
        "creator_hash": anonymize_user_id(user_info.get("uid")),  # 创作者匿名哈希(不存原始 uid)
        "nickname": mask_nickname(user_info.get("nickname")),  # 用户昵称(已脱敏)
        "followers_count": author_stats.get("followers_count"),
        "fans_count": author_stats.get("fans_count"),
        "followers_observed": author_stats.get("followers_observed"),
        "following_count": author_stats.get("following_count"),
        "aweme_count": author_stats.get("aweme_count"),
        "author_liked_count": author_stats.get("author_liked_count"),
        "author_followers_source": author_stats.get("author_followers_source"),
        "author_followers_zero_suspicious": author_stats.get("author_followers_zero_suspicious"),
        "liked_count": str(interact_info.get("digg_count")),
        "collected_count": str(interact_info.get("collect_count")),
        "comment_count": str(interact_info.get("comment_count")),
        "share_count": str(interact_info.get("share_count")),
        "last_modify_ts": utils.get_current_timestamp(),
        "aweme_url": f"https://www.douyin.com/video/{aweme_id}",
        "cover_url": _extract_content_cover_url(aweme_item),
        "video_download_url": _extract_video_download_url(aweme_item),
        "music_download_url": _extract_music_download_url(aweme_item),
        "note_download_url": ",".join(asset["url"] for asset in image_assets),
        "source_keyword": source_keyword_var.get(),
    }
    if config.SAVE_DATA_OPTION == "jsonl" and config.ENABLE_GET_MEIDAS:
        save_content_item["image_assets"] = image_assets
        save_content_item["image_list_source"] = "aweme.images"
    if config.SAVE_DATA_OPTION == "jsonl":
        save_content_item["content_detail_status"] = "detail_observed"
        save_content_item["content_detail_source"] = "aweme_detail"
    utils.logger.info(f"[store.douyin.update_douyin_aweme] douyin aweme id:{aweme_id}, title:{save_content_item.get('title')}")
    await DouyinStoreFactory.create_store().store_content(content_item=save_content_item)


async def batch_update_dy_aweme_comments(aweme_id: str, comments: List[Dict]):
    if not comments:
        return
    for comment_item in comments:
        await update_dy_aweme_comment(aweme_id, comment_item)


async def update_dy_aweme_comment(aweme_id: str, comment_item: Dict):
    comment_aweme_id = comment_item.get("aweme_id")
    if aweme_id != comment_aweme_id:
        utils.logger.error(f"[store.douyin.update_dy_aweme_comment] comment_aweme_id: {comment_aweme_id} != aweme_id: {aweme_id}")
        return
    user_info = comment_item.get("user", {})
    comment_id = comment_item.get("cid")
    parent_comment_id = comment_item.get("reply_id", "0")
    save_comment_item = {
        "comment_id": comment_id,
        "create_time": comment_item.get("create_time"),
        "aweme_id": aweme_id,
        "content": comment_item.get("text"),
        "creator_hash": anonymize_user_id(user_info.get("uid")),  # 创作者匿名哈希(不存原始 uid)
        "nickname": mask_nickname(user_info.get("nickname")),  # 用户昵称(已脱敏)
        "sub_comment_count": str(comment_item.get("reply_comment_total", 0)),
        "like_count": (comment_item.get("digg_count") if comment_item.get("digg_count") else 0),
        "last_modify_ts": utils.get_current_timestamp(),
        "parent_comment_id": parent_comment_id,
        "pictures": ",".join(_extract_comment_image_list(comment_item)),
    }
    utils.logger.info(f"[store.douyin.update_dy_aweme_comment] douyin aweme comment: {comment_id}, content: {save_comment_item.get('content')}")

    await DouyinStoreFactory.create_store().store_comment(comment_item=save_comment_item)


async def save_creator(user_id: str, creator: Dict):
    # 教学版：创作者个人资料(昵称/性别/头像/签名/IP/粉丝数等)不再落库，防骚扰。
    return


async def update_dy_aweme_images(aweme_id: str, image_content_items: List[Dict]):
    """Atomically save all body images for one Douyin note."""

    return await DouYinImage().store_post_images(aweme_id, image_content_items)


async def record_dy_aweme_image_failure(aweme_id: str, image_content_item: Dict):
    """Write one failed image manifest row without success metadata."""

    return await DouYinImage().record_failure(aweme_id, image_content_item)


async def update_dy_aweme_video(aweme_id, video_content, extension_file_name):
    """
    Update Douyin short video
    Args:
        aweme_id:
        video_content:
        extension_file_name:

    Returns:

    """

    await DouYinVideo().store_video({"aweme_id": aweme_id, "video_content": video_content, "extension_file_name": extension_file_name})
