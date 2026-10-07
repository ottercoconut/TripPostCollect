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

"""笔记详情：API → HTML 回退、并发信号量与指定笔记（含历史修复显式分支）。"""

import asyncio
import logging
from typing import Dict, Optional

from tenacity import RetryError

from trippostcollect.platforms.xhs.errors import (
    DataFetchError,
    IPBlockError,
    NoteNotFoundError,
    XHSNoteDetailUnavailable,
)
from trippostcollect.platforms.xhs.models import NoteUrlInfo
from trippostcollect.platforms.xhs.parser import parse_note_info_from_note_url
from trippostcollect.platforms.xhs.repair import run_xhs_repair

logger = logging.getLogger("MediaCrawler")


class XhsDetailMixin:
    """笔记详情；作为 XiaoHongShuCrawler 的 mixin，方法体逐字迁入。"""

    async def get_specified_notes(self):
        """Get the information and comments of the specified post

        Note: Must specify note_id, xsec_source, xsec_token
        """
        if self.ports.repair:
            # 修复开关在 install_hooks 时点读取并经装配传入；显式分支取代旧桥对类方法的替换。
            return await run_xhs_repair(self)
        get_note_detail_task_list = []
        detail_semaphore = asyncio.Semaphore(self.settings.MAX_CONCURRENCY_NUM)
        for full_note_url in self.settings.XHS_SPECIFIED_NOTE_URL_LIST:
            note_url_info: NoteUrlInfo = parse_note_info_from_note_url(full_note_url)
            logger.info(f"[XiaoHongShuCrawler.get_specified_notes] Parse note url info: {note_url_info}")
            crawler_task = self.get_note_detail_async_task(
                note_id=note_url_info.note_id,
                xsec_source=note_url_info.xsec_source,
                xsec_token=note_url_info.xsec_token,
                semaphore=detail_semaphore,
            )
            get_note_detail_task_list.append(crawler_task)

        need_get_comment_note_ids = []
        xsec_tokens = []
        note_details = await asyncio.gather(*get_note_detail_task_list)
        for note_detail in note_details:
            if note_detail:
                if self.is_video_note(note_detail):
                    logger.info(
                        f"[XiaoHongShuCrawler.get_specified_notes] Skip video note, note_id: {note_detail.get('note_id')}"
                    )
                    continue
                need_get_comment_note_ids.append(note_detail.get("note_id", ""))
                xsec_tokens.append(note_detail.get("xsec_token", ""))
                await self.enrich_note_creator(note_detail)
                await self.update_xhs_note(note_detail)
                await self.get_notice_media(note_detail)

    async def get_note_detail_async_task(
        self,
        note_id: str,
        xsec_source: str,
        xsec_token: str,
        semaphore: asyncio.Semaphore,
    ) -> Optional[Dict]:
        """Get note detail

        Args:
            note_id:
            xsec_source:
            xsec_token:
            semaphore:

        Returns:
            Dict: note detail
        """
        note_detail = None
        attempts = 0
        logger.info(f"[get_note_detail_async_task] Begin get note detail, note_id: {note_id}")
        async with semaphore:
            try:
                try:
                    note_detail = await self._run_with_network_recovery(
                        lambda: self.xhs_client.get_note_by_id(
                            note_id,
                            xsec_source,
                            xsec_token,
                        ),
                        stage=f"note_detail_api:note={note_id}",
                    )
                    attempts += 1
                except RetryError as exc:
                    attempts += self._request_failure_attempts(exc)
                    request_failure = self._request_failure_exception(exc)
                    if isinstance(request_failure, IPBlockError):
                        raise request_failure

                if not note_detail:
                    try:
                        note_detail = await self._run_with_network_recovery(
                            lambda: self.xhs_client.get_note_by_id_from_html(
                                note_id,
                                xsec_source,
                                xsec_token,
                                enable_cookie=True,
                            ),
                            stage=f"note_detail_html:note={note_id}",
                        )
                        attempts += 1
                    except RetryError as exc:
                        attempts += self._request_failure_attempts(exc)
                        request_failure = self._request_failure_exception(exc)
                        if isinstance(request_failure, IPBlockError):
                            raise request_failure
                        note_detail = None
                    if not note_detail:
                        logger.warning(
                            "[XiaoHongShuCrawler.get_note_detail_async_task] "
                            f"Detail remained unavailable after API and HTML fallback: {note_id}"
                        )
                        raise XHSNoteDetailUnavailable(
                            note_id,
                            "api_and_html_empty",
                            attempts=max(1, attempts),
                        )

                note_detail.update({"xsec_token": xsec_token, "xsec_source": xsec_source})
                note_detail.update(
                    {
                        "content_detail_status": "detail_observed",
                        "content_detail_source": "note_detail",
                    }
                )

                await self._guarded_pause("note_detail", 4.0, 10.0)

                return note_detail

            except NoteNotFoundError as ex:
                logger.warning(f"[XiaoHongShuCrawler.get_note_detail_async_task] Note not found: {note_id}, {ex}")
                return None
            except DataFetchError as ex:
                logger.error(f"[XiaoHongShuCrawler.get_note_detail_async_task] Get note detail error: {ex}")
                raise
            except KeyError as ex:
                logger.error(f"[XiaoHongShuCrawler.get_note_detail_async_task] have not fund note detail note_id:{note_id}, err: {ex}")
                raise XHSNoteDetailUnavailable(
                    note_id,
                    "detail_parse_failed",
                    attempts=max(1, attempts),
                ) from ex
