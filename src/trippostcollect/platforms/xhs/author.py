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

"""作者补全（§3.4 第 7 条）：停顿后在详情视图内点击作者名，经新标签页的页面状态投影读作者资料，作者页验证等待。

repair 直开的整页若点击后同页跳转到作者页（§9.2，待 V9），读完投影后返回笔记页。
"""

import asyncio
import logging
import random
import re
from typing import Any, Dict, List, NoReturn, Optional, Tuple
from urllib.parse import urlsplit

from playwright.async_api import (
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from trippostcollect.platforms.xhs.behavior import inspect_visible_page_state
from trippostcollect.platforms.xhs.errors import (
    PlatformRuntimeError,
    XHSCreatorProfileUnavailable,
    XhsNoteRedo,
)
from trippostcollect.platforms.xhs.manual_wait import XHSManualWaitTicket
from trippostcollect.platforms.xhs.page_scripts import dom_author_link_diagnostic
from trippostcollect.platforms.xhs.parser import (
    creator_runtime_followers_observed,
    read_creator_runtime_projection,
    xhs_creator_projection_spec,
)

logger = logging.getLogger("MediaCrawler")

# 作者页数据就绪的有界等待：domcontentloaded 只是前置条件，成功以粉丝观察成立为准。
XHS_CREATOR_RUNTIME_READY_SECONDS = 10.0
XHS_CREATOR_RUNTIME_POLL_SECONDS = 0.5
XHS_CREATOR_RUNTIME_EVALUATE_SECONDS = 5.0
# 重试也不会改变结论的投影原因：立即结束等待，候选失败不可重试。
XHS_CREATOR_RUNTIME_FINAL_REASONS = frozenset(
    {"creator_mismatch", "projection_avatar_check_incomplete"}
)
XHS_AUTHOR_TAB_OPEN_SECONDS = 10.0
XHS_AUTHOR_TAB_ATTEMPTS = 2
# 操作人 2026-10-14 要求全面提速（#79）：点击作者名之前的节奏停顿；作者标签页每次打开时抽一次保留时长，
# 从出现起计，读到投影之前不关，抽到的值记在 close_tab 动作的 target.hold_seconds。
XHS_CREATOR_PROFILE_PAUSE_SECONDS = (2.0, 5.0)
XHS_AUTHOR_TAB_HOLD_SECONDS = (5.0, 9.0)
XHS_PROFILE_PATH_RE = re.compile(r"^/user/profile/([^/]+)/?$")
# 诊断里的路径模板：连续 16 个及以上字母数字的片段遮为 <id>（同页面诊断的 class 遮蔽规则）。
XHS_PATH_ID_RE = re.compile(r"[A-Za-z0-9]{16,}")
AUTHOR_RANDOM = random.SystemRandom()


def profile_url_user_id(url: str) -> str:
    """作者标签页地址路径 `/user/profile/<id>` 中的作者 ID；不是作者页时为空串。"""
    match = XHS_PROFILE_PATH_RE.match(urlsplit(str(url or "")).path)
    return match.group(1) if match else ""


def author_click_point(links: Any) -> Optional[Dict]:
    """作者链接的点击点：作者 ID 一致的链接中，取可见且不在评论区的第一个有文字的作者名，没有时取头像链接。

    旧位置隐藏的作者名、评论者链接与作者本人的评论都不点（Mac 人工取证 2026-10-14）；都没有时为 None。
    """
    usable = [
        link for link in (links if isinstance(links, list) else [])
        if isinstance(link, dict) and isinstance(link.get("point"), dict) and link.get("comment") is False
    ]
    chosen = next((link for link in usable if link.get("named") is True), usable[0] if usable else None)
    return chosen["point"] if chosen is not None else None


def path_template(url: str) -> str:
    """地址路径的模板（如 `/user/profile/<id>`），不含查询串。"""
    return XHS_PATH_ID_RE.sub("<id>", urlsplit(str(url or "")).path)


class XhsAuthorMixin:
    """作者补全；作为 XiaoHongShuCrawler 的 mixin。"""

    async def _enrich_creator_via_author_tab(self, note: Any) -> Tuple[Dict, str]:
        """返回（作者资料, 作者访问 id）；同一作者本轮只点一次，之后直接引用首次访问。

        取不到时抛 `XHSCreatorProfileUnavailable`，reason 为单一路径的原因类别。
        """
        user_id = note.user_id
        cached = self.creator_profile_cache.get(user_id)
        if cached is not None:
            return cached
        await self._guarded_pause("creator_profile", *XHS_CREATOR_PROFILE_PAUSE_SECONDS)
        self._creator_browser_reason = ""
        self._creator_visit_id = None
        self._creator_attempts = 0
        creator_info = await self._get_creator_info_from_browser(user_id, note=note)
        if creator_info and self._creator_visit_id:
            entry = (creator_info, self._creator_visit_id)
            self.creator_profile_cache[user_id] = entry
            return entry
        reason = self._creator_browser_reason or "unknown"
        logger.warning(
            f"[XiaoHongShuCrawler.author] creator profile unavailable: {user_id}, reason={reason}"
        )
        raise XHSCreatorProfileUnavailable(user_id, self._creator_attempts or 1, reason=reason)

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

    async def _get_creator_info_from_browser(self, user_id: str, *, note: Any) -> Optional[Dict]:
        """在浮层内点击作者名打开前台新标签页，读取投影后按打开时抽到的保留时长关闭并置前主页面。"""
        self._assert_primary_page_alive("creator_profile_browser")
        primary_state = await self._popup_checkpoint_state(self.context_page)
        self._raise_for_terminal_popup_state(primary_state, reason="creator_profile_browser")
        if primary_state.get("manual_markers"):
            return await self._recover_creator_login_on_primary_page(user_id)
        page = await self._open_author_tab(note)
        if page is None:
            self._creator_browser_reason = "author_tab_not_opened"
            return None
        if page is self.context_page:
            try:
                return await self._read_author_tab(page, user_id)
            finally:
                await self._return_from_author_page(note)
        self._require_capture().attach(page, "author_tab")
        try:
            return await self._read_author_tab(page, user_id)
        except PlaywrightError:
            if self._page_is_closed(page) and not self._page_is_closed(self.context_page):
                self._creator_browser_reason = "author_tab_closed"
                return None
            raise
        finally:
            await self._close_author_tab(page, note)

    async def _open_author_tab(self, note: Any) -> Optional[Page]:
        """写 click_author（from_visit＝本篇详情访问）后点击作者名，等待新标签页；两次都没打开时写诊断并返回 None。

        repair 整页中点击后主页面同页跳转到作者页时，返回主页面本身，不再点第二次。
        """
        capture = self._require_capture()
        attempts: List[str] = []
        clicked_path = ""
        for attempt in range(1, XHS_AUTHOR_TAB_ATTEMPTS + 1):
            self._creator_attempts = attempt
            state = await self._note_view_state(author_id=note.user_id)
            point = author_click_point(state.get("author_links"))
            if point is None:
                attempts.append("link_missing")
                await self._scan_sleep(AUTHOR_RANDOM.uniform(1.0, 2.0))
                continue
            self._author_click_id = capture.action(
                "click_author", page_role="primary", target={"note_id": note.note_id}, from_visit=note.detail_visit,
            )
            self._crawler_page_open_depth += 1
            try:
                async with self.browser_context.expect_page(timeout=int(XHS_AUTHOR_TAB_OPEN_SECONDS * 1000)) as opened:
                    await self._click_point(point)
                page = await opened.value
            except (PlaywrightTimeoutError, TimeoutError):
                if self.ports.repair and profile_url_user_id(getattr(self.context_page, "url", "")):
                    return self.context_page
                attempts.append("tab_timeout")
                clicked_path = path_template(getattr(self.context_page, "url", ""))
                continue
            finally:
                self._crawler_page_open_depth -= 1
            self._register_new_page(page, source="crawler_opened")
            self._extend_new_page_hold(page, AUTHOR_RANDOM.uniform(*XHS_AUTHOR_TAB_HOLD_SECONDS))
            return page
        await self._record_author_tab_failure(note, attempts, clicked_path)
        return None

    async def _record_author_tab_failure(self, note: Any, attempts: List[str], clicked_path: str) -> None:
        """作者标签页没打开：按最后一次尝试写 author_link_missing / author_tab_timeout 导航诊断，并在日志留一行。

        诊断含详情视图内作者名链接、作者主页链接与指向本篇作者的链接的个数、可见个数，前 4 个主页链接的 target 与
        祖先链；点击过时另记点击后主页面路径的模板。不含 token、地址查询串或正文。
        """
        page = self.context_page
        outcome = "author_tab_timeout" if attempts and attempts[-1] == "tab_timeout" else "author_link_missing"
        error = ""
        try:
            links = await dom_author_link_diagnostic(page, self._note_view_roots, note.user_id)
        except Exception as exc:
            links, error = {}, f"author_link_diagnostic: {type(exc).__name__}"
        extra: Dict[str, Any] = {"attempts": attempts, **links}
        if clicked_path:
            extra["clicked_path"] = clicked_path
        await self._record_navigation_diagnostic(page, stage="creator_profile", outcome=outcome, error=error, extra=extra)
        fields = [
            f"outcome={outcome}",
            f"attempts={attempts}",
            *(
                f"{key}={links[key]}"
                for key in (
                    "view_visible", "author_names", "author_names_visible", "profile_links", "profile_links_visible",
                    "author_id_links", "author_id_links_visible",
                )
                if key in links
            ),
            f"clicked_path={clicked_path or None}",
        ]
        logger.warning("[XiaoHongShuCrawler.author] Author tab not opened: " + ", ".join(fields))

    async def _close_author_tab(self, page: Page, note: Any) -> None:
        """打开时抽到的保留期满后写 close_tab（带抽到的保留秒数）并关闭，主页面置前；不额外停留。"""
        await self._wait_before_page_close(page, reason="creator_profile_cleanup")
        self._require_capture().action(
            "close_tab", page_role="author_tab",
            target={"note_id": note.note_id, "hold_seconds": round(self._new_page_hold_seconds(page), 3)},
        )
        await self._close_page_with_deadline(page, reason="creator_profile_cleanup")
        try:
            await self.context_page.bring_to_front()
        except PlaywrightError as exc:
            if self._is_target_closed_error(exc):
                raise
            logger.warning(f"[XiaoHongShuCrawler.author] Could not bring the main page to front: {exc}")

    async def _return_from_author_page(self, note: Any) -> None:
        """同页跳转的作者页读完后写 go_back 并返回笔记页（repair，待 V9）。"""
        self._require_capture().action("go_back", page_role="primary", target={"note_id": note.note_id})
        async with asyncio.timeout(self.inputs.navigation_deadline_seconds() + 2.0):
            await self.context_page.go_back(wait_until="commit")

    async def _read_author_tab(self, page: Page, user_id: str) -> Optional[Dict]:
        """作者页到达检查与小幅滚动后读取投影；登录界面转主页面恢复，验证界面进入作者页验证等待。"""
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
        creator_info, self._creator_browser_reason = (
            await self._read_creator_profile_from_page(page, str(user_id))
        )
        return creator_info

    async def _read_creator_profile_from_page(
        self,
        page: Page,
        user_id: str,
    ) -> Tuple[Optional[Dict], str]:
        """作者页取数：有界等待页面状态投影出现粉丝观察（经通道 read_state，接受时写作者访问）。

        每次读取投影后、接受或重试之前，都先做生命周期、登录、验证、阻断与封禁检查；出现验证
        标记时进入既有验证等待（其内部只做“检查 + 读取一次”，不会回到这里）。取消、页面与浏览器
        关闭照常向上传播。返回作者资料与只含类别的原因，并写一次 creator_profile_parse 导航诊断。
        """
        reasons: List[str] = []
        deadline = self._popup_monotonic() + XHS_CREATOR_RUNTIME_READY_SECONDS
        try:
            async with asyncio.timeout(
                XHS_CREATOR_RUNTIME_READY_SECONDS + XHS_CREATOR_RUNTIME_EVALUATE_SECONDS
            ):
                await page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=int(XHS_CREATOR_RUNTIME_READY_SECONDS * 1000),
                )
        except (PlaywrightTimeoutError, TimeoutError):
            reasons.append("page_not_ready")

        creator_info: Optional[Dict] = None
        outcome = ""
        while True:
            projected, projection_reason = await self._creator_runtime_projection(
                page,
                user_id,
            )
            self._assert_primary_page_alive("creator_profile_runtime_wait")
            text_sample, markers = await inspect_visible_page_state(page)
            await self._raise_for_creator_page_terminal(
                page,
                user_id=str(user_id),
                stage="runtime_wait",
                visible_text=text_sample,
                visible_markers=markers,
            )
            if markers.get("login_required"):
                creator_info = await self._recover_creator_login_on_primary_page(user_id)
                await self._record_navigation_diagnostic(
                    page,
                    stage="creator_profile_parse",
                    outcome=(
                        "ok_login_recovery" if creator_info else "login_recovery_empty"
                    ),
                )
                return creator_info, ("" if creator_info else "login_recovery_empty")
            if markers.get("captcha_or_verify"):
                self._creator_browser_reason = ""
                creator_info = await self._wait_for_creator_profile_verification(
                    page,
                    user_id,
                )
                return creator_info, self._creator_browser_reason
            if projected is not None:
                creator_info = projected
                outcome = "ok"
                break
            remaining = deadline - self._popup_monotonic()
            if projection_reason in XHS_CREATOR_RUNTIME_FINAL_REASONS or remaining <= 0:
                reasons.append(projection_reason)
                break
            await self._popup_sleep(min(XHS_CREATOR_RUNTIME_POLL_SECONDS, remaining))

        reason = ",".join(reasons)
        await self._record_navigation_diagnostic(
            page,
            stage="creator_profile_parse",
            outcome=outcome or reason,
        )
        return creator_info, reason

    async def _read_creator_profile_snapshot(
        self,
        page: Page,
        user_id: str,
    ) -> Tuple[Optional[Dict], str]:
        """读取一次投影；不做可见状态检查，不进入验证等待，也不写诊断。

        成功时原因为 ok；作者不一致时为 creator_mismatch。
        """
        projected, projection_reason = await self._creator_runtime_projection(page, user_id)
        return projected, ("ok" if projected is not None else projection_reason)

    async def _read_creator_profile_after_verification(
        self,
        page: Page,
        user_id: str,
        ticket: XHSManualWaitTicket,
    ) -> Tuple[str, Optional[Dict], str]:
        """验证标记消失后读取一次，并在接受前复查；返回 (动作, 作者资料, 原因)。

        动作：accept（复查通过）、login（转登录恢复）、wait（未取到或验证重现，在同一 ticket 内继续等）、
        stop（作者不一致）。接受前依次复查生命周期、可见状态与阻断、人工流程标记和 ticket 预算，全部
        通过后才写 creator_profile_parse 诊断；超时后才拿到的数据不接受。
        """
        creator_info, read_reason = await self._read_creator_profile_snapshot(page, user_id)
        if read_reason == "creator_mismatch":
            await self._record_navigation_diagnostic(
                page,
                stage="creator_profile_parse",
                outcome=read_reason,
            )
            return "stop", None, read_reason
        if not creator_info:
            return "wait", None, read_reason
        self._assert_primary_page_alive("creator_profile_verification_accept")
        if self._page_is_closed(page):
            raise PlaywrightError("Target page, context or browser has been closed")
        text_sample, markers = await inspect_visible_page_state(page)
        await self._raise_for_creator_page_terminal(
            page,
            user_id=str(user_id),
            stage="verification_accept",
            visible_text=text_sample,
            visible_markers=markers,
        )
        if markers.get("login_required"):
            return "login", None, ""
        if markers.get("captcha_or_verify"):
            return "wait", None, "verification_reappeared"
        ticket.raise_if_exhausted()
        await self._record_navigation_diagnostic(
            page,
            stage="creator_profile_parse",
            outcome=read_reason,
        )
        return "accept", creator_info, read_reason

    async def _creator_runtime_projection(
        self,
        page: Page,
        user_id: str,
    ) -> Tuple[Optional[Dict], str]:
        """经通道读取一次白名单投影；只在粉丝观察成立时接受并写作者访问，否则返回原因类别。

        作者 ID 一致性比对标签页地址路径 `/user/profile/<id>` 与 feed 的作者 ID。
        """
        url_user_id = profile_url_user_id(getattr(page, "url", ""))
        if url_user_id != user_id:
            return None, "creator_mismatch"
        outcome: Dict[str, Any] = {}

        def accept(raw: Any) -> bool:
            projected, reason = read_creator_runtime_projection(raw)
            if reason == "ok" and not creator_runtime_followers_observed(user_id, projected):
                reason = "followers_unobserved"
            outcome["value"] = (projected if reason == "ok" else None, reason)
            return reason == "ok"

        try:
            _raw, visit_id = await self._require_capture().read_state(
                page,
                "creator_page_data",
                xhs_creator_projection_spec(),
                stage="creator_profile",
                subject_id=user_id,
                trigger=getattr(self, "_author_click_id", None),
                accept=accept,
                timeout=XHS_CREATOR_RUNTIME_EVALUATE_SECONDS,
                url_user_id=url_user_id,
            )
        except TimeoutError:
            return None, "runtime_projection_timeout"
        except PlaywrightError as exc:
            if self._is_target_closed_error(exc) or self._page_is_closed(page):
                raise
            return None, "runtime_projection_error"
        if visit_id is not None:
            self._creator_visit_id = visit_id
        return outcome["value"]

    async def _recover_creator_login_on_primary_page(self, user_id: str) -> NoReturn:
        """在原主页面完成轮中登录恢复后整篇重做（§3.8.4）；不再请求接口。"""
        if not await self._wait_for_midrun_login_recovery():
            raise PlatformRuntimeError("XHS creator profile login expired", code="login_required")
        raise XhsNoteRedo("creator_login_recovered")

    async def _wait_for_creator_profile_verification(
        self,
        page: Page,
        user_id: str,
    ) -> Optional[Dict]:
        """Keep a creator page open until manual login or security verification completes.

        验证标记消失后每轮做“检查 + 读取一次 + 接受前复查”（_read_creator_profile_after_verification），
        不会重入就绪等待或本函数；作者不一致时直接结束，其余未取到或验证重现的情况在同一 ticket 的
        共享人工预算内继续等待。
        """
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
                    action, creator_info, read_reason = (
                        await self._read_creator_profile_after_verification(
                            page,
                            str(user_id),
                            ticket,
                        )
                    )
                    if action == "login":
                        ticket.close()
                        return await self._recover_creator_login_on_primary_page(user_id)
                    self._creator_browser_reason = (
                        "" if action == "accept" else f"verification_wait:{read_reason}"
                    )
                    if action == "accept":
                        logger.info(
                            "[XiaoHongShuCrawler] Manual creator-profile "
                            f"verification completed: {user_id}"
                        )
                        return creator_info
                    if action == "stop":
                        return None

                remaining = ticket.remaining_seconds
                if remaining <= 0:
                    ticket.raise_if_exhausted()
                await self._popup_sleep(min(poll_seconds, remaining))
        finally:
            ticket.close()
