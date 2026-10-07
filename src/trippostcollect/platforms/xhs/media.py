# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/core.py
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

# TripPostCollect：T09 迁入根平台；来源 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303，许可见 resources/licenses/MediaCrawler-LICENSE。

"""正文图片：按 url_default > url > url_pre 取一张，带重试下载并整帖暂存；不下载视频。"""

import asyncio
import logging
import random
from typing import Dict, List

from trippostcollect.application.contracts import ImageStagingError
from trippostcollect.platforms.xhs.errors import XHSImageDownloadError
from trippostcollect.platforms.xhs.parser import _xhs_image_assets
from trippostcollect.runtime.image_retry import ImageDownloadFetchError, fetch_image_bytes_with_retry

logger = logging.getLogger("MediaCrawler")


class XhsMediaMixin:
    """正文图片；作为 XiaoHongShuCrawler 的 mixin，方法体逐字迁入。"""

    async def get_notice_media(self, note_detail: Dict):
        if not self.settings.ENABLE_GET_MEIDAS:
            logger.info("[XiaoHongShuCrawler.get_notice_media] Crawling image mode is not enabled")
            return
        await self.get_note_images(note_detail)
        logger.info("[XiaoHongShuCrawler.get_notice_media] Video media crawling is disabled by TripPostCollect policy")

    async def get_note_images(self, note_item: Dict):
        """Get note images. Please use get_notice_media

        Args:
            note_item: Note item dictionary
        """
        if not self.settings.ENABLE_GET_MEIDAS:
            return
        note_id = str(note_item.get("note_id") or "")
        image_assets = _xhs_image_assets(note_item)
        if not image_assets:
            return
        fetched_assets: List[Dict] = []
        for asset in image_assets:
            source_index = int(asset["source_index"])
            try:
                content, attempts = await fetch_image_bytes_with_retry(
                    lambda: self.xhs_client.get_note_media(asset["url"]),
                    logger=logger,
                    label=(
                        f"platform=xhs note_id={note_id} "
                        f"source_index={source_index}"
                    ),
                )
            except ImageDownloadFetchError as exc:
                await self.record_xhs_note_image_failure(
                    note_id,
                    {
                        **asset,
                        "attempts": exc.attempts,
                        "http_status": exc.http_status,
                        "error_code": exc.code,
                    },
                )
                raise XHSImageDownloadError(
                    note_id, source_index, exc.code, exc.attempts
                ) from exc
            await asyncio.sleep(random.random())
            if content is None:
                await self.record_xhs_note_image_failure(
                    note_id,
                    {
                        **asset,
                        "attempts": attempts,
                        "http_status": None,
                        "error_code": "image_download_retryable",
                    },
                )
                raise XHSImageDownloadError(
                    note_id,
                    source_index,
                    "image_download_retryable",
                    attempts,
                )
            fetched_assets.append(
                {**asset, "content": content, "attempts": attempts, "http_status": 200}
            )
        try:
            await self.update_xhs_note_images(note_id, fetched_assets)
        except ImageStagingError as exc:
            source_index = int(exc.source_index or 0)
            failed_asset = next(
                (
                    asset
                    for asset in fetched_assets
                    if int(asset["source_index"]) == source_index
                ),
                image_assets[0],
            )
            await self.record_xhs_note_image_failure(
                note_id,
                {
                    **failed_asset,
                    "attempts": int(failed_asset.get("attempts") or 1),
                    "http_status": 200,
                    "error_code": exc.code,
                },
            )
            raise XHSImageDownloadError(
                note_id,
                source_index,
                exc.code,
                int(failed_asset.get("attempts") or 1),
            ) from exc
