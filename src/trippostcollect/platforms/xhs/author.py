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

"""作者补全：无 token API → 暂停 → 原页检查 → 作者页回退与作者页验证等待。"""

import logging
import os
import random
from typing import Dict, Optional
from urllib.parse import quote

from playwright.async_api import Page

from trippostcollect.platforms.xhs.behavior import inspect_visible_page_state
from trippostcollect.platforms.xhs.errors import (
    IPBlockError,
    PlatformRuntimeError,
    XHSCreatorProfileUnavailable,
    XHSNetworkRecoveryTimeout,
)

logger = logging.getLogger("MediaCrawler")


class XhsAuthorMixin:
    """作者补全；作为 XiaoHongShuCrawler 的 mixin，方法体逐字迁入。"""

    async def enrich_note_creator(self, note_detail: Dict) -> None:
        """Attach creator homepage metrics when TripPostCollect requests author enrichment."""
        if os.environ.get("TRIPPOSTCOLLECT_XHS_ENRICH_CREATORS") != "1":
            return
        user_info = note_detail.get("user") or {}
        user_id = user_info.get("user_id")
        if not user_id:
            return
        cached_creator = self.creator_profile_cache.get(str(user_id))
        if cached_creator:
            note_detail["creator_profile"] = cached_creator
            return
        creator_info = None
        attempts = 0
        try:
            creator_info = await self._run_with_network_recovery(
                lambda: self.xhs_client.get_creator_info(user_id=user_id),
                stage=f"creator_profile_api:user={user_id}",
            )
            attempts += 1
        except XHSNetworkRecoveryTimeout:
            raise
        except Exception as exc:
            attempts += self._request_failure_attempts(exc)
            request_failure = self._request_failure_exception(exc)
            if isinstance(request_failure, IPBlockError):
                raise request_failure
            if isinstance(request_failure, PlatformRuntimeError):
                raise request_failure
            if self._is_login_expired_failure(exc):
                raise PlatformRuntimeError(
                    "XHS creator profile login expired",
                    code="login_required",
                ) from request_failure
            logger.warning(
                "[XiaoHongShuCrawler.enrich_note_creator] "
                f"session profile request failed, using browser fallback: {user_id}, {exc}"
            )

        if not creator_info:
            await self._guarded_pause("creator_profile_browser_fallback", 12.0, 30.0)
            attempts += 1
            creator_info = await self._get_creator_info_from_browser(str(user_id))
        if creator_info:
            self.creator_profile_cache[str(user_id)] = creator_info
            note_detail["creator_profile"] = creator_info
            await self._guarded_pause("creator_profile", 8.0, 18.0)
        else:
            logger.warning(
                f"[XiaoHongShuCrawler.enrich_note_creator] creator profile empty after browser fallback: {user_id}"
            )
            raise XHSCreatorProfileUnavailable(str(user_id), attempts)

    async def _raise_for_creator_page_terminal(
        self,
        page: Page,
        *,
        user_id: str,
        stage: str,
        visible_text: str,
        visible_markers: Dict[str, bool],
    ) -> None:
        page_url = str(getattr(page, "url", "") or "")
        terminal_code, failure_type, _matched_markers = (
            self._classify_visible_terminal(
                text=visible_text,
                url=page_url,
                markers=visible_markers,
            )
        )
        if not terminal_code:
            return
        if failure_type == "platform_security_limit":
            await self.ports.record_platform_security_limit(
                page,
                stage=f"creator_profile:{user_id}:{stage}",
                visible_text_sample=visible_text,
                visible_markers=visible_markers,
            )
        self._raise_for_terminal_popup_state(
            {"terminal": terminal_code},
            reason=f"creator_profile:{user_id}:{stage}",
        )

    async def _get_creator_info_from_browser(self, user_id: str) -> Optional[Dict]:
        """Load an author homepage in the signed-in context when the direct request is empty."""
        self._assert_primary_page_alive("creator_profile_browser")
        primary_state = await self._popup_checkpoint_state(self.context_page)
        self._raise_for_terminal_popup_state(primary_state, reason="creator_profile_browser")
        if primary_state.get("manual_markers"):
            return await self._recover_creator_login_on_primary_page(user_id)
        page = await self._new_guarded_page()
        try:
            await self._goto_with_deadline(
                page,
                f"{self.index_url}/user/profile/{quote(user_id)}",
                stage="creator_profile_browser",
            )
            await page.wait_for_timeout(random.randint(1_200, 3_000))

            text_sample, markers = await inspect_visible_page_state(page)
            await self._raise_for_creator_page_terminal(
                page,
                user_id=str(user_id),
                stage="arrival",
                visible_text=text_sample,
                visible_markers=markers,
            )
            if markers.get("login_required"):
                return await self._recover_creator_login_on_primary_page(user_id)
            if markers.get("captcha_or_verify"):
                return await self._wait_for_creator_profile_verification(page, user_id)
            viewport = page.viewport_size or {"width": 1280, "height": 800}
            await page.mouse.move(
                random.randint(80, max(81, viewport["width"] - 80)),
                random.randint(80, max(81, viewport["height"] - 80)),
                steps=random.randint(6, 14),
            )
            await page.mouse.wheel(0, random.randint(180, 460))
            await page.wait_for_timeout(random.randint(500, 1_500))

            text_sample, markers = await inspect_visible_page_state(page)
            await self._raise_for_creator_page_terminal(
                page,
                user_id=str(user_id),
                stage="post_scroll",
                visible_text=text_sample,
                visible_markers=markers,
            )
            if markers.get("login_required"):
                return await self._recover_creator_login_on_primary_page(user_id)
            if markers.get("captcha_or_verify"):
                return await self._wait_for_creator_profile_verification(page, user_id)
            html_content = await page.content()
            return self.xhs_client.extract_creator_info_from_html(html_content)
        finally:
            await self._close_page_with_deadline(
                page,
                reason="creator_profile_cleanup",
            )

    async def _recover_creator_login_on_primary_page(self, user_id: str) -> Optional[Dict]:
        """Restore the shared session on the existing main page, then retry the author."""
        await self._wait_for_midrun_login_recovery()
        return await self._run_with_network_recovery(
            lambda: self.xhs_client.get_creator_info(user_id=user_id),
            stage=f"creator_profile_api:user={user_id}",
        )

    async def _wait_for_creator_profile_verification(
        self,
        page: Page,
        user_id: str,
    ) -> Optional[Dict]:
        """Keep a creator page open until manual login or security verification completes."""
        poll_seconds = max(
            1.0,
            self.inputs.creator_verify_poll_seconds(),
        )
        budget = self._get_manual_wait_budget()
        ticket = budget.start(f"creator_profile_verification:{user_id}")
        try:
            await page.bring_to_front()
            logger.warning(
                "[XiaoHongShuCrawler] Manual login or security verification "
                "required for creator profile; keeping the page open within "
                f"the shared manual budget ({ticket.remaining_seconds:.1f}s left): "
                f"{user_id}"
            )

            while True:
                ticket.raise_if_exhausted()
                text_sample, markers = await inspect_visible_page_state(page)
                await self._raise_for_creator_page_terminal(
                    page,
                    user_id=str(user_id),
                    stage="verification_wait",
                    visible_text=text_sample,
                    visible_markers=markers,
                )
                if markers.get("login_required"):
                    ticket.close()
                    return await self._recover_creator_login_on_primary_page(user_id)
                if not markers.get("captcha_or_verify"):
                    ticket.raise_if_exhausted()
                    html_content = await page.content()
                    creator_info = self.xhs_client.extract_creator_info_from_html(
                        html_content
                    )
                    if creator_info:
                        logger.info(
                            "[XiaoHongShuCrawler] Manual creator-profile "
                            f"verification completed: {user_id}"
                        )
                        return creator_info

                remaining = ticket.remaining_seconds
                if remaining <= 0:
                    ticket.raise_if_exhausted()
                await self._popup_sleep(min(poll_seconds, remaining))
        finally:
            ticket.close()
