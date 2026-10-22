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

"""正文图片：详情视图内翻图触发前端加载，按 url_default > url > url_pre 认领窗口截获的字节并整帖暂存；不下载视频。"""

import logging
import random
from typing import Any, Dict, List, Optional

from trippostcollect.application.contracts import ImageStagingError
from trippostcollect.platforms.xhs.errors import XHSImageDownloadError
from trippostcollect.runtime.image_retry import classified_http_image_error
from trippostcollect.runtime.page_capture import CapturedImage, ImageWindow, image_url_form

logger = logging.getLogger("MediaCrawler")

# 每次翻到下一张之前的停顿（操作人 2026-10-14 要求全面提速，#79）。
XHS_IMAGE_FLIP_SECONDS = (0.8, 1.8)
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
        """翻图触发剩余图片加载，最后一张之后再等剩余响应；窗口由调用方在点击（repair 为直开）之前打开。"""
        if len(assets) > 1:
            await self._flip_note_images(note_id, len(assets))
        return await window.collect(XHS_IMAGE_SETTLE_SECONDS)

    async def _flip_note_images(self, note_id: str, total: int) -> None:
        """每次翻图前按 XHS_IMAGE_FLIP_SECONDS 停顿（只写账本 note_image 停顿），按页码 k/N 核实前进；不前进时换一种方式再试一次。"""
        capture = self._require_capture()
        state = await self._note_view_state()
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
                plan = await self._plan_click(state["right_arrow"]) if method == "arrow" else None
                # 点箭头：光标到达并静止后在光标处做命中检测（#102），不中不按下、不写动作，换另一种方式。
                arrow = {"kind": "right_arrow", "roots": list(self._note_view_roots)}
                if plan is not None and not await self._approach_checked(plan, arrow):
                    continue
                capture.action(
                    "flip_image", page_role="primary", target={"note_id": note_id, "to_index": current + 1, "method": method},
                    pointer=plan.evidence if plan is not None else None,
                )
                if plan is not None:
                    await self._press(plan)
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
        self, note_id: str, window: ImageWindow, assets: List[Dict], images: Dict[str, Optional[tuple[CapturedImage, str]]],
    ) -> List[str]:
        """把窗口截获的字节按资产顺序交给暂存；返回图片访问 id。未截获或状态码异常记失败行后抛出。

        有未截获的图片时，抛出前先写一条 `note_images` 导航诊断（全部缺失序号与窗口内见到的响应形态）。
        """
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
                missing = [item for item in assets if images.get(item["url"]) is None]
                if missing:
                    await self._record_images_not_observed(note_id, window, assets, missing)
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

    async def _record_images_not_observed(
        self, note_id: str, window: ImageWindow, assets: List[Dict], missing: List[Dict],
    ) -> None:
        """未截获诊断与一行日志：只有计数、序号、协议、主机、样式与布尔值；失败请求的地址同样只留形态。"""
        report = window.miss_report([asset["url"] for asset in missing])
        entries = [
            {"source_index": int(asset["source_index"]), **entry} for asset, entry in zip(missing, report.pop("missing"))
        ]
        indexes = [entry["source_index"] for entry in entries]
        request_failures = [
            {
                "at": failure.get("at"),
                "resource_type": failure.get("resource_type"),
                **image_url_form(str(failure.get("url") or "")),
                "failure": failure.get("failure"),
            }
            for failure in self._navigation_request_failures[-8:]
        ]
        await self._record_navigation_diagnostic(
            self.context_page,
            stage="note_images",
            outcome="image_response_not_observed",
            extra={
                "note_id": note_id,
                "expected_count": len(assets),
                "captured_count": len(assets) - len(missing),
                "missing_source_indexes": indexes,
                **report,
                "missing": entries,
                "request_failures": request_failures,
            },
        )
        forms = ", ".join(
            f"{form['scheme']} {form['host']} !{form['style']}×{form['count']}" for form in report["unmatched_forms"][:3]
        )
        logger.warning(
            f"[XiaoHongShuCrawler.media] Note images not captured: {note_id} expected={len(assets)}, "
            f"captured={len(assets) - len(missing)}, missing={indexes}, "
            f"unmatched={report['unmatched_responses']} ({forms or 'none'}), "
            f"same_file_id={sum(entry['same_file_id_seen'] for entry in entries)}/{len(entries)}, "
            f"read_errors={report['read_errors']}, dropped={report['dropped_responses']}"
        )
