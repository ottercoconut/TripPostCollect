# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/zhihu/__init__.py
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
from typing import List
from urllib.parse import urlsplit

import config
from base.base_crawler import AbstractStore
from model.m_zhihu import ZhihuComment, ZhihuContent, ZhihuCreator
from ._store_impl import (ZhihuCsvStoreImplement,
                                          ZhihuDbStoreImplement,
                                          ZhihuJsonStoreImplement,
                                          ZhihuJsonlStoreImplement,
                                          ZhihuSqliteStoreImplement,
                                          ZhihuMongoStoreImplement,
                                          ZhihuExcelStoreImplement)
from tools import utils
from tools.image_manifest import ImageStagingError, normalize_image_url, zhihu_source_asset_key
from var import source_keyword_var
from .zhihu_store_media import ZhihuStoreImage


def zhihu_content_image_assets(content_item: ZhihuContent) -> List[dict]:
    """Project only answer/article body images from an observed content body."""

    if content_item.content_type not in {"answer", "article"}:
        return []
    if content_item.content_detail_status != "detail_observed":
        return []
    excluded_urls = set()
    for value in (content_item.avatar_url, content_item.author_profile_url):
        try:
            excluded_urls.add(normalize_image_url(value))
        except ImageStagingError:
            continue
    assets: List[dict] = []
    seen = set()
    for value in content_item.image_list:
        try:
            url = normalize_image_url(value)
        except ImageStagingError:
            continue
        path = urlsplit(url).path.lower().rstrip("/")
        if url in excluded_urls or path.endswith("/equation") or "/equation/" in f"{path}/":
            continue
        asset_key = zhihu_source_asset_key(url)
        if asset_key in seen:
            continue
        seen.add(asset_key)
        assets.append(
            {"url": url, "source_index": len(assets), "source_asset_key": asset_key}
        )
    return assets


class ZhihuStoreFactory:
    STORES = {
        "csv": ZhihuCsvStoreImplement,
        "db": ZhihuDbStoreImplement,
        "postgres": ZhihuDbStoreImplement,
        "json": ZhihuJsonStoreImplement,
        "jsonl": ZhihuJsonlStoreImplement,
        "sqlite": ZhihuSqliteStoreImplement,
        "mongodb": ZhihuMongoStoreImplement,
        "excel": ZhihuExcelStoreImplement,
    }

    @staticmethod
    def create_store() -> AbstractStore:
        store_class = ZhihuStoreFactory.STORES.get(config.SAVE_DATA_OPTION)
        if not store_class:
            raise ValueError("[ZhihuStoreFactory.create_store] Invalid save option only supported csv or db or json or sqlite or mongodb or excel ...")
        return store_class()

async def batch_update_zhihu_contents(contents: List[ZhihuContent]):
    """
    Batch update Zhihu contents
    Args:
        contents:

    Returns:

    """
    if not contents:
        return

    for content_item in contents:
        await update_zhihu_content(content_item)

async def update_zhihu_content(content_item: ZhihuContent):
    """
    Update Zhihu content
    Args:
        content_item:

    Returns:

    """
    content_item.source_keyword = source_keyword_var.get()
    content_item.image_assets = zhihu_content_image_assets(content_item)
    content_item.image_list = [asset["url"] for asset in content_item.image_assets]
    content_item.image_count = len(content_item.image_list)
    if content_item.image_assets:
        content_item.image_list_source = "content_html"
    local_db_item = content_item.model_dump()
    local_db_item.update({"last_modify_ts": utils.get_current_timestamp()})
    utils.logger.info(f"[store.zhihu.update_zhihu_content] zhihu content: {local_db_item}")
    await ZhihuStoreFactory.create_store().store_content(local_db_item)


async def update_zhihu_content_images(content_id: str, image_content_items: List[dict]):
    """Atomically save all body images for one Zhihu answer/article."""

    return await ZhihuStoreImage().store_post_images(content_id, image_content_items)


async def record_zhihu_content_image_failure(content_id: str, image_content_item: dict):
    """Write one failed image manifest row without success metadata."""

    return await ZhihuStoreImage().record_failure(content_id, image_content_item)



async def batch_update_zhihu_note_comments(comments: List[ZhihuComment]):
    """
    Batch update Zhihu content comments
    Args:
        comments:

    Returns:

    """
    if not comments:
        return

    for comment_item in comments:
        await update_zhihu_content_comment(comment_item)


async def update_zhihu_content_comment(comment_item: ZhihuComment):
    """
    Update Zhihu content comment
    Args:
        comment_item:

    Returns:

    """
    local_db_item = comment_item.model_dump()
    local_db_item.update({"last_modify_ts": utils.get_current_timestamp()})
    utils.logger.info(f"[store.zhihu.update_zhihu_note_comment] zhihu content comment:{local_db_item}")
    await ZhihuStoreFactory.create_store().store_comment(local_db_item)


async def save_creator(creator: ZhihuCreator):
    """
    Save Zhihu creator information
    Args:
        creator:

    Returns:

    """
    if not creator:
        return
    local_db_item = creator.model_dump()
    local_db_item.update({"last_modify_ts": utils.get_current_timestamp()})
    await ZhihuStoreFactory.create_store().store_creator(local_db_item)
