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

import asyncio
import logging
import os
import random
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from playwright.async_api import (
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from trippostcollect.platforms.xhs.behavior import inspect_visible_page_state
from trippostcollect.platforms.xhs.errors import (
    IPBlockError,
    PlatformRuntimeError,
    XHSCreatorProfileUnavailable,
    XHSNetworkRecoveryTimeout,
)
from trippostcollect.platforms.xhs.parser import (
    creator_runtime_followers_observed,
    creator_profile_user_ids,
    read_creator_runtime_projection,
    xhs_creator_projection_spec,
)

logger = logging.getLogger("MediaCrawler")

# 作者页数据就绪的有界等待：domcontentloaded 只是前置条件，成功以粉丝观察成立为准。
XHS_CREATOR_RUNTIME_READY_SECONDS = 10.0
XHS_CREATOR_RUNTIME_POLL_SECONDS = 0.5
XHS_CREATOR_RUNTIME_EVALUATE_SECONDS = 5.0
# 重试也不会改变结论的投影原因：立即结束等待（作者不一致不再做静态回退）。
XHS_CREATOR_RUNTIME_FINAL_REASONS = frozenset(
    {"creator_mismatch", "projection_avatar_check_incomplete"}
)

# 固定的页面投影脚本（page.evaluate 原样使用这一份）：只读取页面已执行的
# window.__INITIAL_STATE__.user.userPageData。先在该作者记录内有界收集头像证据 URL（键与路径来自
# records.sanitization），再按白名单只复制标量字段，删除与证据 URL 完全相同的字符串；证据集合不返回。
# 只有自有数据属性 __v_isRef === true 的节点才按 Vue ref 解包，其余对象的全部字段都参与证据扫描。
# 计数类字段不投影布尔值。只读自有数据属性（不触发 getter 或 toJSON），检查做不完时拒绝整个投影。
# 不序列化整份状态，不返回 HTML。
XHS_CREATOR_RUNTIME_PROJECTION_SCRIPT = """(spec) => {
    const INCOMPLETE = 'projection_avatar_check_incomplete';
    const avatarKeys = new Set(spec.avatar_keys);
    const avatarPaths = new Set(spec.avatar_paths.map((path) => JSON.stringify(path)));
    const isObject = (value) => value !== null && typeof value === 'object';
    const ownValue = (node, key) => {
        const descriptor = Object.getOwnPropertyDescriptor(node, key);
        if (descriptor === undefined) return undefined;
        if (!('value' in descriptor)) throw new Error(INCOMPLETE);
        return descriptor.value;
    };
    const numericFields = new Set(spec.numeric_fields);
    const stripChars = new Set(Array.from(spec.strip_chars));
    const pyStrip = (text) => {
        let start = 0;
        let end = text.length;
        while (start < end && stripChars.has(text[start])) start += 1;
        while (end > start && stripChars.has(text[end - 1])) end -= 1;
        return text.slice(start, end);
    };
    const isRef = (value) => {
        const marker = Object.getOwnPropertyDescriptor(value, '__v_isRef');
        return marker !== undefined && 'value' in marker && marker.value === true;
    };
    const unref = (value) => {
        let current = value;
        for (let depth = 0; depth < 4; depth += 1) {
            if (!isObject(current) || !isRef(current)) return current;
            const wrapped = Object.getOwnPropertyDescriptor(current, '_rawValue')
                || Object.getOwnPropertyDescriptor(current, '_value');
            if (wrapped === undefined || !('value' in wrapped)) throw new Error(INCOMPLETE);
            current = wrapped.value;
        }
        return current;
    };
    const isPlain = (value) => {
        if (!isObject(value) || Array.isArray(value)) return false;
        const prototype = Object.getPrototypeOf(value);
        return prototype === Object.prototype || prototype === null;
    };
    const isAvatarKey = (key) => avatarKeys.has(key.toLowerCase())
        || avatarKeys.has(key.toUpperCase().toLowerCase());
    const evidencedUrl = (value) => {
        if (typeof value !== 'string') return null;
        const candidate = pyStrip(value);
        if (!candidate || (candidate.length > 8192 && Array.from(candidate).length > 8192)) return null;
        const lowered = candidate.toLowerCase();
        let rest = null;
        if (lowered.startsWith('http://')) rest = candidate.slice(7);
        else if (lowered.startsWith('https://')) rest = candidate.slice(8);
        else if (lowered.startsWith('//')) rest = candidate.slice(2);
        if (rest === null || !rest.split(/[\\/?#]/, 1)[0]) return null;
        return candidate;
    };
    const evidence = new Set();
    const ancestors = [];
    let remaining = spec.max_nodes;
    const visit = (value, path, inAvatar, depth) => {
        remaining -= 1;
        if (remaining < 0 || depth > spec.max_depth) throw new Error(INCOMPLETE);
        const node = unref(value);
        if (inAvatar) {
            const url = evidencedUrl(node);
            if (url !== null) {
                evidence.add(url);
                return;
            }
        }
        if (node === null || node === undefined) return;
        const kind = typeof node;
        if (kind === 'string' || kind === 'number' || kind === 'boolean') return;
        if (kind !== 'object') throw new Error(INCOMPLETE);
        if (ancestors.includes(node)) throw new Error(INCOMPLETE);
        ancestors.push(node);
        try {
            if (Array.isArray(node)) {
                const length = ownValue(node, 'length');
                for (let index = 0; index < length; index += 1) {
                    visit(ownValue(node, String(index)), path, inAvatar, depth + 1);
                }
            } else if (node instanceof Set) {
                for (const item of node) visit(item, path, inAvatar, depth + 1);
            } else if (node instanceof Map) {
                for (const [key, item] of node) {
                    visit(key, path, inAvatar, depth + 1);
                    visit(item, path, inAvatar, depth + 1);
                }
            } else if (!(node instanceof Date)) {
                if (!isPlain(node)) throw new Error(INCOMPLETE);
                for (const key of Object.keys(node)) {
                    const nestedPath = path.concat([key]);
                    const nestedAvatar = inAvatar || isAvatarKey(key)
                        || avatarPaths.has(JSON.stringify(nestedPath));
                    visit(ownValue(node, key), nestedPath, nestedAvatar, depth + 1);
                }
            }
        } finally {
            ancestors.pop();
        }
    };
    const scalarFields = (source, fields) => {
        const result = {};
        for (const field of fields) {
            const value = ownValue(source, field);
            if (typeof value === 'string') {
                if (!evidence.has(pyStrip(value))) result[field] = value;
            } else if (typeof value === 'number' && Number.isFinite(value)) {
                result[field] = value;
            } else if (typeof value === 'boolean' && !numericFields.has(field)) {
                result[field] = value;
            }
        }
        return result;
    };
    try {
        const state = unref(ownValue(window, '__INITIAL_STATE__'));
        const user = isPlain(state) ? unref(ownValue(state, 'user')) : undefined;
        const data = isPlain(user) ? unref(ownValue(user, 'userPageData')) : undefined;
        if (!isPlain(data)) return { status: 'missing' };
        visit(data, [], false, 0);
        const creator = scalarFields(data, spec.top_fields);
        for (const [key, fields] of spec.object_fields) {
            const container = unref(ownValue(data, key));
            if (isPlain(container)) creator[key] = scalarFields(container, fields);
        }
        for (const [key, fields] of spec.list_fields) {
            const items = unref(ownValue(data, key));
            if (!Array.isArray(items)) continue;
            const rebuilt = [];
            const length = Math.min(ownValue(items, 'length'), spec.max_items);
            for (let index = 0; index < length; index += 1) {
                const item = unref(ownValue(items, String(index)));
                if (isPlain(item)) rebuilt.push(scalarFields(item, fields));
            }
            creator[key] = rebuilt;
        }
        return { status: 'ok', creator };
    } catch (error) {
        return { status: 'rejected', reason: INCOMPLETE };
    }
}"""


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
        api_reason = ""
        try:
            creator_info = await self._run_with_network_recovery(
                lambda: self.xhs_client.get_creator_info(user_id=user_id),
                stage=f"creator_profile_api:user={user_id}",
            )
            attempts += 1
            api_reason = self._creator_client_parse_reason()
        except XHSNetworkRecoveryTimeout:
            raise
        except Exception as exc:
            attempts += self._request_failure_attempts(exc)
            request_failure = self._request_failure_exception(exc)
            api_reason = f"api_request_failed:{type(request_failure).__name__}"
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

        browser_reason = ""
        if not creator_info:
            await self._guarded_pause("creator_profile_browser_fallback", 12.0, 30.0)
            attempts += 1
            self._creator_browser_reason = ""
            creator_info = await self._get_creator_info_from_browser(str(user_id))
            browser_reason = (
                self._creator_browser_reason or self._creator_client_parse_reason()
            )
        if creator_info:
            self.creator_profile_cache[str(user_id)] = creator_info
            note_detail["creator_profile"] = creator_info
            await self._guarded_pause("creator_profile", 8.0, 18.0)
        else:
            reason = f"api={api_reason or 'unknown'};browser={browser_reason or 'unknown'}"
            logger.warning(
                f"[XiaoHongShuCrawler.enrich_note_creator] creator profile empty after browser fallback: {user_id}, reason={reason}"
            )
            raise XHSCreatorProfileUnavailable(str(user_id), attempts, reason=reason)

    def _creator_client_parse_reason(self) -> str:
        """客户端最近一次作者页静态解析的原因类别；没有记录时返回空串。"""
        return str(getattr(self.xhs_client, "last_creator_parse_reason", "") or "")

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
            creator_info, self._creator_browser_reason = (
                await self._read_creator_profile_from_page(page, str(user_id))
            )
            return creator_info
        finally:
            await self._close_page_with_deadline(
                page,
                reason="creator_profile_cleanup",
            )

    async def _read_creator_profile_from_page(
        self,
        page: Page,
        user_id: str,
    ) -> Tuple[Optional[Dict], str]:
        """作者页取数：有界等待运行时投影出现粉丝观察，取不到再回退 page.content() 严格静态解析。

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

        if creator_info is None and "creator_mismatch" not in reasons:
            html_content = await page.content()
            creator_info = self.xhs_client.extract_creator_info_from_html(html_content)
            static_reason = self._creator_client_parse_reason() or "unknown"
            if creator_info:
                outcome = "ok_static_state"
            else:
                reasons.append(f"static_{static_reason}")
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
        """读取一次：先运行时投影，取不到再严格静态解析；不做可见状态检查，也不进入验证等待。

        供人工验证完成后的轮询使用；只在得到结论（成功或作者不一致）时写 creator_profile_parse 诊断。
        """
        projected, projection_reason = await self._creator_runtime_projection(page, user_id)
        if projected is not None:
            await self._record_navigation_diagnostic(
                page,
                stage="creator_profile_parse",
                outcome="ok",
            )
            return projected, "ok"
        if projection_reason == "creator_mismatch":
            await self._record_navigation_diagnostic(
                page,
                stage="creator_profile_parse",
                outcome=projection_reason,
            )
            return None, projection_reason
        html_content = await page.content()
        creator_info = self.xhs_client.extract_creator_info_from_html(html_content)
        if creator_info:
            await self._record_navigation_diagnostic(
                page,
                stage="creator_profile_parse",
                outcome="ok_static_state",
            )
            return creator_info, "ok_static_state"
        static_reason = self._creator_client_parse_reason() or "unknown"
        return None, f"{projection_reason},static_{static_reason}"

    async def _creator_runtime_projection(
        self,
        page: Page,
        user_id: str,
    ) -> Tuple[Optional[Dict], str]:
        """执行固定投影脚本一次；只在粉丝观察成立时返回作者资料，否则返回原因类别。"""
        try:
            async with asyncio.timeout(XHS_CREATOR_RUNTIME_EVALUATE_SECONDS):
                raw = await page.evaluate(
                    XHS_CREATOR_RUNTIME_PROJECTION_SCRIPT,
                    xhs_creator_projection_spec(),
                )
        except TimeoutError:
            return None, "runtime_projection_timeout"
        except PlaywrightError as exc:
            if self._is_target_closed_error(exc) or self._page_is_closed(page):
                raise
            return None, "runtime_projection_error"
        projected, reason = read_creator_runtime_projection(raw)
        if reason != "ok":
            return None, reason
        if any(page_user_id != user_id for page_user_id in creator_profile_user_ids(projected)):
            return None, "creator_mismatch"
        if not creator_runtime_followers_observed(user_id, projected):
            return None, "followers_unobserved"
        return projected, "ok"

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
        """Keep a creator page open until manual login or security verification completes.

        验证标记消失后每轮只做“检查 + 读取一次”（_read_creator_profile_snapshot），不会重入就绪
        等待或本函数；作者不一致时直接结束，其余未取到的情况在共享人工预算内继续等待。
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
                    creator_info, read_reason = await self._read_creator_profile_snapshot(
                        page,
                        str(user_id),
                    )
                    self._creator_browser_reason = (
                        "" if creator_info else f"verification_wait:{read_reason}"
                    )
                    if creator_info:
                        logger.info(
                            "[XiaoHongShuCrawler] Manual creator-profile "
                            f"verification completed: {user_id}"
                        )
                        return creator_info
                    if read_reason == "creator_mismatch":
                        return None

                remaining = ticket.remaining_seconds
                if remaining <= 0:
                    ticket.raise_if_exhausted()
                await self._popup_sleep(min(poll_seconds, remaining))
        finally:
            ticket.close()
