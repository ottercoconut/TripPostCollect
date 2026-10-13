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

"""正文图片：浮层内翻图触发前端加载，按 url_default > url > url_pre 认领窗口截获的字节并整帖暂存；不下载视频。

`get_notice_media` / `get_note_images` 的直连下载只剩 repair 过渡使用（提交 6 删）。
"""

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional

from trippostcollect.application.contracts import ImageStagingError
from trippostcollect.platforms.xhs.errors import XHSImageDownloadError
from trippostcollect.platforms.xhs.page_scripts import dom_overlay_state
from trippostcollect.platforms.xhs.parser import _xhs_image_assets
from trippostcollect.runtime.image_retry import (
    ImageDownloadFetchError,
    classified_http_image_error,
    fetch_image_bytes_with_retry,
)
from trippostcollect.runtime.page_capture import CapturedImage, ImageWindow

logger = logging.getLogger("MediaCrawler")

XHS_IMAGE_FLIP_SECONDS = (1.5, 3.0)
XHS_IMAGE_SETTLE_SECONDS = 15.0
# 约四分之一的翻图改按 → 键（R2 已证实可用），其余点右箭头；不点图片本身。
XHS_IMAGE_KEY_FLIP_SHARE = 0.25
XHS_IMAGE_FLIP_CONFIRM_SECONDS = 2.0
XHS_IMAGE_FLIP_POLL_SECONDS = 0.25
MEDIA_RANDOM = random.SystemRandom()


class XhsMediaMixin:
    """正文图片；作为 XiaoHongShuCrawler 的 mixin。"""

    async def _collect_note_images(
        self, note_id: str, window: ImageWindow, assets: List[Dict],
    ) -> Dict[str, Optional[tuple[CapturedImage, str]]]:
        """翻图触发剩余图片加载，最后一张之后再等剩余响应；窗口由 `_process_note_card` 在点击前打开。"""
        if len(assets) > 1:
            await self._flip_note_images(note_id, len(assets))
        return await window.collect(XHS_IMAGE_SETTLE_SECONDS)

    async def _flip_note_images(self, note_id: str, total: int) -> None:
        """每次翻图前停 1.5–3 秒（只写账本 note_image 停顿），按页码 k/N 核实前进；不前进时换一种方式再试一次。"""
        capture = self._require_capture()
        state = await dom_overlay_state(self.context_page)
        current = int(state.get("page_index") or 1)
        while current < total:
            start_seq = capture.pause_started()
            seconds = MEDIA_RANDOM.uniform(*XHS_IMAGE_FLIP_SECONDS)
            await self._scan_sleep(seconds)
            capture.pause_finished("note_image", start_seq, seconds)
            methods = ("key", "arrow") if MEDIA_RANDOM.random() < XHS_IMAGE_KEY_FLIP_SHARE else ("arrow", "key")
            advanced = False
            for method in methods:
                if method == "arrow" and state.get("right_arrow") is None:
                    continue
                capture.action(
                    "flip_image", page_role="primary", target={"note_id": note_id, "to_index": current + 1, "method": method},
                )
                if method == "arrow":
                    await self._click_point(state["right_arrow"])
                else:
                    await self.context_page.keyboard.press("ArrowRight")
                state = await self._wait_overlay(
                    lambda observed: int(observed.get("page_index") or 0) > current, XHS_IMAGE_FLIP_CONFIRM_SECONDS,
                )
                if int(state.get("page_index") or 0) > current:
                    advanced = True
                    break
            if not advanced:
                logger.warning(f"[XiaoHongShuCrawler.media] Image pager did not advance past {current}/{total}: {note_id}")
                return
            current = int(state["page_index"])

    async def _stage_note_images(
        self, note_id: str, assets: List[Dict], images: Dict[str, Optional[tuple[CapturedImage, str]]],
    ) -> List[str]:
        """把窗口截获的字节按资产顺序交给暂存；返回图片访问 id。未截获或状态码异常记失败行后抛出。"""
        fetched: List[Dict[str, Any]] = []
        visit_ids: List[str] = []
        for asset in assets:
            source_index = int(asset["source_index"])
            result = images.get(asset["url"])
            status = result[0].status if result is not None else None
            if result is None or status != 200:
                code = (
                    "image_response_not_observed"
                    if result is None
                    else classified_http_image_error(int(status), f"XHS image HTTP {status}").code
                )
                await self.record_xhs_note_image_failure(
                    note_id, {**asset, "attempts": 1, "http_status": status, "error_code": code},
                )
                raise XHSImageDownloadError(note_id, source_index, code, 1)
            image, visit_id = result
            fetched.append({**asset, "content": image.content, "attempts": 1, "http_status": 200})
            visit_ids.append(visit_id)
        try:
            await self.update_xhs_note_images(note_id, fetched)
        except ImageStagingError as exc:
            source_index = int(exc.source_index or 0)
            failed_asset = next(
                (asset for asset in fetched if int(asset["source_index"]) == source_index), fetched[0],
            )
            await self.record_xhs_note_image_failure(
                note_id, {**failed_asset, "attempts": 1, "http_status": 200, "error_code": exc.code},
            )
            raise XHSImageDownloadError(note_id, source_index, exc.code, 1) from exc
        return visit_ids

    async def get_notice_media(self, note_detail: Dict):
        """repair 过渡：经旧客户端直连下载（提交 6 改为截获）。"""
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
