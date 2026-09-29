# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/weibo/weibo_store_media.py
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
# @Author  : Erm
# @Time    : 2024/4/9 17:35
# @Desc    : Weibo media storage
from trippostcollect.core.paths import MEDIACRAWLER_DIR

from pathlib import Path
from typing import Dict, List

from base.base_crawler import AbstractStoreImage
from tools import utils
from tools.image_manifest import weibo_source_asset_key
from trippostcollect.artifacts.image_staging import PostImageStager
import config


class WeiboStoreImage(PostImageStager, AbstractStoreImage):
    def __init__(self):
        super().__init__(
            save_data_root=Path(config.SAVE_DATA_PATH) if config.SAVE_DATA_PATH else MEDIACRAWLER_DIR / "data",
            platform="weibo",
            source_key="image_list",
            source_asset_key=lambda item: weibo_source_asset_key(item.get("pid"), item["url"]),
            log_saved=lambda count, note_id: utils.logger.info(
                f"[WeiboImageStoreImplement.store_post_images] saved {count} "
                f"body images for note {note_id}"
            ),
        )

    async def store_post_images(self, note_id: str, image_content_items: List[Dict]):
        return await super().store_post_images(note_id, image_content_items)

    async def record_failure(self, note_id: str, image_content_item: Dict):
        return await super().record_failure(note_id, image_content_item)
