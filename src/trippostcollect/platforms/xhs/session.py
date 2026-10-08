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

"""唯一浏览器会话：新页守卫、网络暂停恢复、中途登录恢复与 CDP 生命周期。"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional
from urllib.parse import quote

from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Error as PlaywrightError,
    Page,
    Playwright,
)

from trippostcollect.core import resources
from trippostcollect.platforms.xhs.behavior import (
    inspect_visible_page_state,
    run_required_continuity_behavior,
    run_required_request_pause,
)
from trippostcollect.platforms.xhs.client import (
    is_recoverable_xhs_transport_failure,
    unwrap_xhs_request_failure,
)
from trippostcollect.platforms.xhs.errors import (
    PlatformRuntimeError,
    XHSMainPageClosedUnexpected,
    XHSNetworkRecoveryTimeout,
    is_recoverable_xhs_navigation_failure,
)
from trippostcollect.platforms.xhs.login import XiaoHongShuLogin
from trippostcollect.platforms.xhs.manual_wait import XHSManualWaitBudget
from trippostcollect.runtime.behavior import project_browser_args

logger = logging.getLogger("MediaCrawler")


XHS_NEW_PAGE_MIN_HOLD_SECONDS = 30.0
_XHS_MANUAL_CHECKPOINT_TEXTS = (
    "扫码登录",
    "二维码",
    "打开小红书扫一扫",
    "确认登录",
    "登录确认",
    "手机号登录",
    "验证码",
    "请通过验证",
    "安全验证",
    "身份验证",
    "人机验证",
    "滑块验证",
    "拖动滑块",
    "security verification",
    "sms verification",
    "parameter error",
    "captcha",
    "geetest",
)
_XHS_MANUAL_CHECKPOINT_SELECTORS = (
    "img.qrcode-img",
    "input[placeholder*='验证码']",
    "[class*='captcha']",
    "[class*='geetest']",
    "iframe[src*='captcha']",
)


class XhsSessionMixin:
    """会话与浏览器生命周期；作为 XiaoHongShuCrawler 的 mixin，方法体逐字迁入。"""

    @staticmethod
    def _page_is_closed(page: Page) -> bool:
        try:
            return page.is_closed()
        except Exception:
            return False

    @staticmethod
    def _popup_monotonic() -> float:
        return time.monotonic()

    @staticmethod
    async def _popup_sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    def _get_manual_wait_budget(self) -> XHSManualWaitBudget:
        if self._manual_wait_budget is None:
            self._manual_wait_budget = XHSManualWaitBudget.from_environment(
                monotonic=lambda: self._popup_monotonic(),
            )
        return self._manual_wait_budget

    def _install_new_page_guard(self) -> None:
        """Protect every page appearing after browser launch for at least 30 seconds."""
        try:
            pages = list(self.browser_context.pages)
        except Exception:
            pages = []
        self.browser_context.on("page", self._on_browser_page)
        if pages:
            primary_page = pages[0]
            self._initial_pages[id(primary_page)] = primary_page
            for extra_page in pages[1:]:
                self._register_new_page(extra_page, source="preexisting_extra")

    def _on_browser_page(self, page: Page) -> None:
        source = "crawler_opened" if self._crawler_page_open_depth > 0 else "platform_opened"
        self._register_new_page(page, source=source)

    def _register_new_page(self, page: Page, *, source: str) -> None:
        page_id = id(page)
        if self._initial_pages.get(page_id) is page:
            return
        if page_id in self._new_pages:
            return
        opened_at = self._popup_monotonic()
        self._new_pages[page_id] = (page, opened_at, source)
        task = asyncio.create_task(self._hold_new_page(page, opened_at, source))
        self._new_page_guard_tasks[page_id] = task

    async def _hold_new_page(self, page: Page, opened_at: float, source: str) -> None:
        """Surface any new tab and keep it alive without trying to classify it."""
        try:
            await page.bring_to_front()
        except Exception as exc:
            logger.warning(
                "[XiaoHongShuCrawler] Could not bring new tab to front: "
                f"{type(exc).__name__}: {exc}"
            )
        remaining = max(
            0.0,
            XHS_NEW_PAGE_MIN_HOLD_SECONDS
            - (self._popup_monotonic() - opened_at),
        )
        logger.warning(
            "[XiaoHongShuCrawler] New browser tab opened; keeping it visible "
            f"for at least {XHS_NEW_PAGE_MIN_HOLD_SECONDS:.0f}s before any "
            f"crawler-initiated close: source={source}, url={getattr(page, 'url', '')}"
        )
        if remaining > 0:
            await self._popup_sleep(remaining)

    async def _new_guarded_page(self) -> Page:
        """Open a crawler-requested page while retaining the universal new-tab guard."""
        self._crawler_page_open_depth += 1
        try:
            page = await self.browser_context.new_page()
        finally:
            self._crawler_page_open_depth -= 1
        # Playwright normally emits ``page`` before ``new_page`` returns. Registering
        # explicitly as well keeps the guard correct if that event is delayed.
        self._register_new_page(page, source="crawler_opened")
        return page

    async def _wait_before_page_close(self, page: Page, *, reason: str) -> None:
        page_id = id(page)
        if page_id not in self._new_pages:
            return
        task = self._new_page_guard_tasks.get(page_id)
        if task and not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[XiaoHongShuCrawler] New-tab guard task failed; "
                    f"enforcing remaining hold directly: {type(exc).__name__}: {exc}"
                )
        _, opened_at, source = self._new_pages[page_id]
        remaining = max(
            0.0,
            XHS_NEW_PAGE_MIN_HOLD_SECONDS
            - (self._popup_monotonic() - opened_at),
        )
        if remaining > 0:
            await self._popup_sleep(remaining)
        logger.info(
            "[XiaoHongShuCrawler] New-tab minimum hold completed; "
            f"close may proceed: source={source}, reason={reason}, "
            f"url={getattr(page, 'url', '')}"
        )

    async def _wait_for_all_new_page_guards(self) -> None:
        """Drain all new-tab hold periods before Playwright or the context can close them."""
        while True:
            guarded_pages = [
                page
                for page, _, _ in self._new_pages.values()
                if not self._page_is_closed(page)
            ]
            pending = [
                page
                for page in guarded_pages
                if self._popup_monotonic() - self._new_pages[id(page)][1]
                < XHS_NEW_PAGE_MIN_HOLD_SECONDS
            ]
            if not pending:
                return
            await asyncio.gather(
                *(
                    self._wait_before_page_close(page, reason="browser_context_cleanup")
                    for page in pending
                )
            )

    async def _prepare_browser_shutdown(self) -> None:
        """Close pages through the guard before context or Playwright teardown."""
        while True:
            await self._wait_for_all_new_page_guards()
            try:
                pages = [
                    page
                    for page in self.browser_context.pages
                    if not self._page_is_closed(page)
                ]
            except Exception:
                return
            if not pages:
                return
            page_ids_before = {id(page) for page in pages}
            for page in pages:
                await self._close_page_with_deadline(
                    page,
                    reason="browser_shutdown",
                )
            await asyncio.sleep(0)
            try:
                remaining_ids = {
                    id(page)
                    for page in self.browser_context.pages
                    if not self._page_is_closed(page)
                }
            except Exception:
                return
            if not remaining_ids or remaining_ids == page_ids_before:
                return

    async def _guarded_pause(self, stage: str, minimum: float, maximum: float) -> float:
        event = await run_required_request_pause(stage, minimum, maximum, ports=self.ports.behavior)
        seconds = float(event["seconds"])
        logger.info(f"[XiaoHongShuCrawler] Guarded pause stage={stage} seconds={seconds:.3f}")
        if stage in {"search_results", "search_page"}:
            continuity = await run_required_continuity_behavior(
                self.context_page, stage, ports=self.ports.behavior,
            )
            logger.info(
                "[XiaoHongShuCrawler] Continuity behavior "
                f"stage={stage} status={continuity.get('status')}"
            )
        return seconds

    async def _close_page_with_deadline(
        self,
        page: Page,
        *,
        reason: str = "crawler_page_cleanup",
    ) -> None:
        await self._wait_before_page_close(page, reason=reason)
        if self._page_is_closed(page):
            return
        try:
            async with asyncio.timeout(10):
                await page.close()
        except Exception as exc:
            logger.warning(f"[XiaoHongShuCrawler] Page close did not finish cleanly: {exc}")

    async def _browser_identity_headers(self) -> Dict[str, str]:
        try:
            async with asyncio.timeout(10):
                identity = await self.context_page.evaluate(
                    """() => ({
                        webdriver: navigator.webdriver,
                        user_agent: navigator.userAgent || '',
                        language: navigator.language || '',
                        platform: navigator.userAgentData
                            ? navigator.userAgentData.platform
                            : (navigator.platform || ''),
                        mobile: navigator.userAgentData
                            ? navigator.userAgentData.mobile
                            : false,
                        brands: navigator.userAgentData
                            ? Array.from(navigator.userAgentData.brands || [])
                            : [],
                    })"""
                )
        except Exception as exc:
            raise RuntimeError(f"xhs_browser_identity_unavailable:{type(exc).__name__}") from exc

        user_agent = str((identity or {}).get("user_agent") or "").strip()
        language = str((identity or {}).get("language") or "").strip()
        platform = str((identity or {}).get("platform") or "").strip()
        brands = (identity or {}).get("brands") or []
        if (identity or {}).get("webdriver") is not None:
            raise RuntimeError("xhs_browser_identity_webdriver_exposed")
        if not user_agent or not language or not platform or not isinstance(brands, list) or not brands:
            raise RuntimeError("xhs_browser_identity_incomplete")

        ua_match = re.search(r"(?:Chrome|Chromium)/(\d+)", user_agent)
        ua_major = ua_match.group(1) if ua_match else ""
        normalized_brands: List[str] = []
        chromium_major = ""
        for item in brands:
            if not isinstance(item, dict):
                continue
            brand = str(item.get("brand") or "").replace("\\", "").replace('"', "").strip()
            version = str(item.get("version") or "").split(".", 1)[0].strip()
            if not brand or not version:
                continue
            normalized_brands.append(f'"{brand}";v="{version}"')
            if brand in {"Chromium", "Google Chrome"}:
                chromium_major = version
        if not normalized_brands or not ua_major or chromium_major != ua_major:
            raise RuntimeError(
                f"xhs_browser_identity_version_mismatch:ua={ua_major},client_hints={chromium_major}"
            )

        self.user_agent = user_agent
        return {
            "accept-language": language,
            "sec-ch-ua": ", ".join(normalized_brands),
            "sec-ch-ua-mobile": "?1" if bool((identity or {}).get("mobile")) else "?0",
            "sec-ch-ua-platform": f'"{platform}"',
            "user-agent": user_agent,
        }

    @staticmethod
    def _profile_dir() -> str:
        explicit_path = os.environ.get("TRIPPOSTCOLLECT_XHS_PROFILE_DIR", "").strip()
        if not explicit_path:
            raise RuntimeError("XHS requires TRIPPOSTCOLLECT_XHS_PROFILE_DIR from xhs_runner.py")
        return os.path.abspath(os.path.expanduser(explicit_path))

    def _assert_primary_page_alive(self, stage: str) -> None:
        """Keep the original business page; auxiliary tabs never become primary."""
        self._assert_cdp_lifecycle_alive(stage)
        if self._page_is_closed(self.context_page):
            raise XHSMainPageClosedUnexpected(stage=stage)

    @staticmethod
    def _is_target_closed_error(exc: BaseException) -> bool:
        text = str(exc).lower()
        return bool(
            exc.__class__.__name__ == "TargetClosedError"
            or "target page, context or browser has been closed" in text
            or "targetclosederror" in text
        )

    async def _run_human_behavior_on_primary_page(self, keyword: str) -> Dict:
        """Run behavior once; a closed primary page terminates this browser session."""
        try:
            return await self.ports.run_required_human_behavior(self.context_page, "xhs")
        except Exception as exc:
            if not self._is_target_closed_error(exc):
                raise
            self._assert_cdp_lifecycle_alive("behavior_primary_page_closed")
            raise XHSMainPageClosedUnexpected(stage="behavior") from exc

    async def _open_behavior_search_page_on_primary_page(self, keyword: str) -> None:
        """Navigate once; never adopt another tab after the primary page closes."""
        try:
            await self._open_behavior_search_page(keyword)
        except Exception as exc:
            if not self._is_target_closed_error(exc):
                raise
            self._assert_cdp_lifecycle_alive("search_navigation_primary_page_closed")
            raise XHSMainPageClosedUnexpected(stage="search_navigation") from exc

    def _assert_cdp_lifecycle_alive(self, stage: str) -> None:
        """Prefer a manager-observed context/browser lifecycle code when present."""
        manager = getattr(self, "cdp_manager", None)
        assert_alive = getattr(manager, "assert_alive", None)
        if callable(assert_alive):
            assert_alive(stage)

    @staticmethod
    def _classify_visible_terminal(
        *,
        text: str,
        url: str,
        markers: Dict[str, bool],
    ) -> tuple[str, str, List[str]]:
        """Classify visible XHS blockers without inventing a numeric code."""

        lowered = str(text or "").casefold()
        sms_verification_context = any(
            marker.casefold() in lowered
            for marker in XiaoHongShuLogin._VERIFICATION_CONTEXT_TEXTS
        )
        terminal_code, failure_type, matched_markers = (
            XiaoHongShuLogin.classify_terminal_state(
                text=str(text or ""),
                url=str(url or ""),
                sms_verification_context=sms_verification_context,
            )
        )
        if terminal_code:
            return terminal_code, failure_type, matched_markers
        if markers.get("platform_security_limit"):
            return (
                "xhs_platform_security_limit_unspecified",
                "platform_security_limit",
                ["platform_security_limit"],
            )
        if markers.get("rate_limited"):
            return (
                "xhs_rate_limited_terminal",
                "rate_limited",
                ["rate_limited"],
            )
        if markers.get("blocked"):
            return (
                "xhs_blocked_terminal",
                "blocked_or_forbidden",
                ["blocked"],
            )
        return "", "", []

    async def _popup_checkpoint_state(self, page: Page) -> Dict[str, object]:
        """Classify only visible login, verification, and terminal evidence."""
        if self._page_is_closed(page):
            return {
                "closed": True,
                "visible_text": "",
                "visible_markers": {},
                "manual_markers": [],
                "terminal": "",
            }

        visible_text = ""
        visible_markers: Dict[str, bool] = {}
        try:
            visible_text, visible_markers = await inspect_visible_page_state(page)
        except Exception:
            try:
                locator = getattr(page, "locator", None)
                if callable(locator):
                    visible_text = await locator("body").inner_text(timeout=2_000)
                else:
                    visible_text = await page.content()
            except Exception:
                visible_text = ""
            visible_text = " ".join(str(visible_text).split())[:2_000]

        selector_markers: List[str] = []
        locator = getattr(page, "locator", None)
        if callable(locator):
            for selector in _XHS_MANUAL_CHECKPOINT_SELECTORS:
                try:
                    candidate = locator(selector)
                    first = getattr(candidate, "first", candidate)
                    if await first.is_visible(timeout=250):
                        selector_markers.append(selector)
                except Exception:
                    continue

        normalized = str(visible_text or "")
        lowered = normalized.casefold()
        text_markers = sorted(
            {
                marker
                for marker in _XHS_MANUAL_CHECKPOINT_TEXTS
                if marker.casefold() in lowered
            }
        )
        manual_markers = sorted(set(text_markers) | set(selector_markers))
        if visible_markers.get("captcha_or_verify"):
            manual_markers.append("captcha_or_verify")
        if visible_markers.get("login_required"):
            manual_markers.append("login_required")
        manual_markers = sorted(set(manual_markers))

        page_url = str(getattr(page, "url", "") or "")
        terminal, terminal_failure_type, terminal_markers = (
            self._classify_visible_terminal(
                text=normalized,
                url=page_url,
                markers=visible_markers,
            )
        )

        return {
            "closed": False,
            "url": page_url,
            "visible_text": normalized[:360],
            "visible_markers": visible_markers,
            "manual_markers": manual_markers,
            "terminal": terminal,
            "terminal_failure_type": terminal_failure_type,
            "terminal_markers": terminal_markers,
        }

    @staticmethod
    def _raise_for_terminal_popup_state(
        state: Dict[str, object],
        *,
        reason: str,
    ) -> None:
        terminal = str(state.get("terminal") or "")
        if not terminal:
            return
        if terminal == "rate_limited":
            terminal = "xhs_rate_limited_terminal"
        elif terminal == "blocked":
            terminal = "xhs_blocked_terminal"
        raise PlatformRuntimeError(
            f"{terminal}:{reason}",
            code=terminal,
        )

    def _assert_network_recovery_session(self, state: Dict[str, object]) -> None:
        self._assert_cdp_lifecycle_alive(
            str(state.get("stage") or "network_recovery")
        )

        context = getattr(self, "browser_context", None)
        page = state.get("operation_page")
        if context is None:
            raise PlaywrightError("xhs_network_recovery_context_missing")
        if page is None:
            raise PlaywrightError("xhs_network_recovery_page_missing")
        if self._page_is_closed(page):
            raise PlaywrightError("xhs_network_recovery_page_closed")

        expected_context = state.get("browser_context")
        if expected_context is not context:
            raise PlaywrightError("xhs_network_recovery_context_replaced")
        if state.get("track_primary_page") and getattr(
            self,
            "context_page",
            None,
        ) is not page:
            raise PlaywrightError("xhs_network_recovery_page_replaced")

        page_context = getattr(page, "context", context)
        if page_context is not context:
            raise PlaywrightError("xhs_network_recovery_page_has_foreign_context")

        try:
            context_pages = list(context.pages)
        except PlaywrightError:
            raise
        except Exception as exc:
            raise PlaywrightError(
                "xhs_network_recovery_context_unavailable:"
                f"{type(exc).__name__}:{exc}"
            ) from exc
        if page not in context_pages:
            raise PlaywrightError("xhs_network_recovery_page_left_context")

    async def _record_network_recovery_event(
        self,
        *,
        stage: str,
        outcome: str,
        error: str = "",
        page: Optional[Page] = None,
    ) -> None:
        page = page or getattr(self, "context_page", None)
        if page is None:
            return
        try:
            await self._record_navigation_diagnostic(
                page,
                stage=stage,
                outcome=outcome,
                error=error,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[XiaoHongShuCrawler] Could not persist network recovery event: "
                f"{type(exc).__name__}: {exc}"
            )

    async def _pause_for_network_recovery(
        self,
        exc: BaseException,
        *,
        stage: str,
        state: Dict[str, object],
        page: Optional[Page] = None,
    ) -> bool:
        if not (
            is_recoverable_xhs_transport_failure(exc)
            or is_recoverable_xhs_navigation_failure(exc)
        ):
            return False

        wait_seconds = self.inputs.network_wait_seconds()
        if wait_seconds <= 0:
            return False
        minimum_delay = max(
            0.1,
            self.inputs.network_retry_min_seconds(),
        )
        maximum_delay = max(
            minimum_delay,
            self.inputs.network_retry_max_seconds(),
        )

        if not state:
            operation_page = page or getattr(self, "context_page", None)
            state.update(
                {
                    "stage": stage,
                    "started_at": self._popup_monotonic(),
                    "failures": 0,
                    "browser_context": getattr(self, "browser_context", None),
                    "operation_page": operation_page,
                    "track_primary_page": operation_page
                    is getattr(self, "context_page", None),
                }
            )
        elif state.get("stage") != stage:
            raise RuntimeError("xhs_network_recovery_state_stage_mismatch")
        self._assert_network_recovery_session(state)

        started_at = float(state["started_at"])
        elapsed = self._popup_monotonic() - started_at
        if elapsed >= wait_seconds:
            await self._record_network_recovery_event(
                stage=stage,
                outcome="network_recovery_timeout",
                error=f"{type(unwrap_xhs_request_failure(exc)).__name__}: {exc}",
                page=page,
            )
            return False

        failures = int(state.get("failures") or 0) + 1
        state["failures"] = failures
        delay = min(
            maximum_delay,
            minimum_delay * (2 ** min(failures - 1, 6)),
            wait_seconds - elapsed,
        )
        nested = unwrap_xhs_request_failure(exc)
        logger.warning(
            "[XiaoHongShuCrawler] Temporary network outage; preserving the "
            "current browser session and retrying the same operation: "
            f"stage={stage}, failure={type(nested).__name__}, "
            f"elapsed={elapsed:.1f}s, retry_in={delay:.1f}s"
        )
        await self._record_network_recovery_event(
            stage=stage,
            outcome="network_paused",
            error=f"{type(nested).__name__}: {nested}"[:500],
            page=page,
        )
        await self._popup_sleep(delay)
        self._assert_network_recovery_session(state)
        return True

    async def _finish_network_recovery(
        self,
        *,
        stage: str,
        state: Dict[str, object],
        page: Optional[Page] = None,
    ) -> None:
        if not state:
            return
        if state.get("stage") != stage:
            raise RuntimeError("xhs_network_recovery_state_stage_mismatch")
        operation_page = page or state.get("operation_page")
        started_at = float(state.get("started_at") or self._popup_monotonic())
        elapsed = max(0.0, self._popup_monotonic() - started_at)
        state.clear()
        logger.info(
            "[XiaoHongShuCrawler] Network recovered in the current browser "
            f"session: stage={stage}, elapsed={elapsed:.1f}s"
        )
        await self._record_network_recovery_event(
            stage=stage,
            outcome="network_recovered",
            page=operation_page,
        )

    async def _abort_network_recovery(
        self,
        *,
        stage: str,
        state: Dict[str, object],
        error: BaseException,
        page: Optional[Page] = None,
        outcome: str = "network_recovery_aborted",
    ) -> None:
        if not state:
            return
        if state.get("stage") != stage:
            raise RuntimeError("xhs_network_recovery_state_stage_mismatch")
        operation_page = page or state.get("operation_page")
        state.clear()
        logger.warning(
            "[XiaoHongShuCrawler] Network recovery aborted by a non-network "
            f"failure: stage={stage}, failure={type(error).__name__}"
        )
        await self._record_network_recovery_event(
            stage=stage,
            outcome=outcome,
            error=f"{type(error).__name__}: {error}"[:500],
            page=operation_page,
        )

    async def _run_with_network_recovery(self, operation, *, stage: str):
        state: Dict[str, object] = {}
        while True:
            try:
                result = await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not is_recoverable_xhs_transport_failure(exc):
                    await self._abort_network_recovery(
                        stage=stage,
                        state=state,
                        error=exc,
                    )
                    raise
                if await self._pause_for_network_recovery(
                    exc,
                    stage=stage,
                    state=state,
                ):
                    continue
                started_at = float(
                    state.get("started_at") or self._popup_monotonic()
                )
                raise XHSNetworkRecoveryTimeout(
                    stage,
                    self._popup_monotonic() - started_at,
                ) from unwrap_xhs_request_failure(exc)
            await self._finish_network_recovery(
                stage=stage,
                state=state,
            )
            return result

    async def _pong_with_network_recovery(self, *, stage: str) -> bool:
        return bool(
            await self._run_with_network_recovery(
                self.xhs_client.pong,
                stage=stage,
            )
        )

    async def _wait_for_midrun_login_recovery(self, keyword: Optional[str] = None) -> bool:
        """Wait on the original page's QR; never open or adopt a login tab."""
        self._assert_primary_page_alive("midrun_login_recovery")
        budget = self._get_manual_wait_budget()
        ticket = budget.start("midrun_login_recovery")
        recovered = False
        try:
            logger.warning(
                "[XiaoHongShuCrawler] Xiaohongshu login expired during search; "
                "waiting on the original page within the shared manual budget "
                f"({ticket.remaining_seconds:.1f}s left) so the operator can "
                "complete login/security verification."
            )
            try:
                await self.context_page.bring_to_front()
            except Exception:
                pass

            stage_started_elapsed = ticket.manual_elapsed_seconds
            last_print = 0.0
            last_pong = stage_started_elapsed - 10.0
            manual_latched = False
            clear_observations = 0
            while True:
                ticket.raise_if_exhausted()
                self._assert_primary_page_alive("midrun_login_recovery")
                try:
                    await self.context_page.bring_to_front()
                except Exception:
                    pass

                state = await self._popup_checkpoint_state(self.context_page)
                self._raise_for_terminal_popup_state(
                    state,
                    reason="midrun_login_recovery",
                )
                manual_markers = list(state.get("manual_markers") or [])
                visible_text = str(state.get("visible_text") or "").strip()
                if manual_markers:
                    manual_latched = True
                    clear_observations = 0
                elif manual_latched:
                    # A blank/loading DOM is not evidence that an SMS or CAPTCHA
                    # flow completed. Require two consecutive rendered, clear
                    # observations before probing the apparently signed-in shell.
                    clear_observations = (
                        clear_observations + 1 if visible_text else 0
                    )

                session_probe_allowed = (
                    not manual_latched or clear_observations >= 2
                )
                profile_ui = (
                    await self._profile_ui_visible()
                    if session_probe_allowed
                    else False
                )
                manual_elapsed = ticket.manual_elapsed_seconds
                stage_elapsed = manual_elapsed - stage_started_elapsed
                if profile_ui and manual_elapsed - last_pong >= 5.0:
                    ticket.raise_if_exhausted()
                    last_pong = manual_elapsed
                    with ticket.paused():
                        await self.xhs_client.update_cookies(
                            browser_context=self.browser_context,
                            urls=self.cookie_urls,
                        )
                        session_ready = await self._pong_with_network_recovery(
                            stage="midrun_login_confirmation",
                        )
                    ticket.raise_if_exhausted()
                    if session_ready:
                        confirmed_state = await self._popup_checkpoint_state(
                            self.context_page
                        )
                        self._raise_for_terminal_popup_state(
                            confirmed_state,
                            reason="midrun_login_recovery_confirmation",
                        )
                        if confirmed_state.get("manual_markers") or not str(
                            confirmed_state.get("visible_text") or ""
                        ).strip():
                            if confirmed_state.get("manual_markers"):
                                manual_latched = True
                            clear_observations = 0
                            remaining = ticket.remaining_seconds
                            if remaining <= 0:
                                ticket.raise_if_exhausted()
                            await self._popup_sleep(min(2.0, remaining))
                            continue
                        recovered = True
                        break

                if stage_elapsed - last_print >= 10.0:
                    logger.info(
                        "[XiaoHongShuCrawler] Waiting for mid-run Xiaohongshu login "
                        f"recovery: profile_ui={profile_ui}, "
                        f"manual_latched={manual_latched}, "
                        f"clear_observations={clear_observations}, "
                        f"visible={manual_markers}"
                    )
                    last_print = stage_elapsed
                remaining = ticket.remaining_seconds
                if remaining <= 0:
                    ticket.raise_if_exhausted()
                await self._popup_sleep(min(2.0, remaining))
        finally:
            ticket.close()

        if not recovered:
            raise RuntimeError("xhs_midrun_login_recovery_incomplete")
        if keyword is not None:
            search_url = f"{self.index_url}/search_result?keyword={quote(keyword)}"
            await self._goto_with_deadline(
                self.context_page,
                search_url,
                stage="midrun_login_recovered",
            )
        await self.xhs_client.update_cookies(
            browser_context=self.browser_context,
            urls=self.cookie_urls,
        )
        logger.info(
            "[XiaoHongShuCrawler] Mid-run login recovery confirmed; "
            "retrying the same search page."
        )
        return True

    async def _run_browser_session(
        self,
        playwright: Playwright,
        playwright_proxy_format: Optional[Dict],
        httpx_proxy_format: Optional[str],
    ) -> None:
        if self._browser_session_started:
            raise RuntimeError("xhs_browser_session_already_started")
        # Latch before the first await. A failed first launch must not make the
        # crawler instance reusable for a second Chrome/BrowserContext attempt.
        self._browser_session_started = True

        if self.settings.ENABLE_CDP_MODE:
            logger.info("[XiaoHongShuCrawler] Launching browser using CDP mode")
            self.browser_context = await self.launch_browser_with_cdp(
                playwright,
                playwright_proxy_format,
                self.user_agent,
                headless=self.settings.CDP_HEADLESS,
            )
            if self.cdp_manager:
                await self.cdp_manager.add_stealth_script()
        else:
            logger.info("[XiaoHongShuCrawler] Launching browser using standard mode")
            chromium = playwright.chromium
            self.browser_context = await self.launch_browser(
                chromium,
                playwright_proxy_format,
                self.user_agent,
                headless=self.settings.HEADLESS,
            )
            with resources.path("js/stealth.min.js") as stealth_path:
                await self.browser_context.add_init_script(path=str(stealth_path))

        self._install_new_page_guard()
        await self.ports.install_project_runtime_hints(self.browser_context)
        self.context_page = await self._single_page_for_login()
        await self._goto_with_deadline(
            self.context_page,
            self.explore_url,
            stage="initial_explore",
        )
        await self._wait_for_initial_page_settle()
        initial_shell_timeout = self.inputs.initial_shell_timeout_seconds()
        if not await self._wait_for_visible_page_shell(
            self.context_page,
            stage="initial_explore",
            timeout_seconds=initial_shell_timeout,
        ):
            await self._goto_with_deadline(
                self.context_page,
                self.explore_url,
                stage="initial_explore_retry",
            )
            if not await self._wait_for_visible_page_shell(
                self.context_page,
                stage="initial_explore_retry",
                timeout_seconds=initial_shell_timeout,
            ):
                raise RuntimeError("xhs_initial_explore_not_rendered")

        self.xhs_client = await self.create_xhs_client(httpx_proxy_format)
        if not await self._pong_with_network_recovery(stage="startup_login_probe"):
            await self._run_qrcode_login()
            await self.xhs_client.update_cookies(
                browser_context=self.browser_context,
                urls=self.cookie_urls,
            )
            if not await self._pong_with_network_recovery(
                stage="startup_post_login_probe",
            ):
                raise RuntimeError(
                    "[XiaoHongShuCrawler] Xiaohongshu login state not confirmed "
                    "after login flow"
                )
        behavior_keyword = next(
            (item.strip() for item in self.settings.KEYWORDS.split(",") if item.strip()),
            "",
        )
        if behavior_keyword:
            await self._open_behavior_search_page_on_primary_page(behavior_keyword)
        self._assert_primary_page_alive("initial_behavior")
        behavior_evidence = await self._run_human_behavior_on_primary_page(
            behavior_keyword
        )
        if behavior_evidence.get("status") != "completed":
            raise RuntimeError("XHS required human behavior stage did not complete")
        await self.xhs_client.update_cookies(
            browser_context=self.browser_context,
            urls=self.cookie_urls,
        )
        if not await self._pong_with_network_recovery(
            stage="post_behavior_login_probe",
        ):
            raise RuntimeError("xhs_session_not_confirmed_after_human_behavior")
        self.crawler_type = self.settings.CRAWLER_TYPE
        if self.settings.CRAWLER_TYPE == "search":
            await self.search()
        elif self.settings.CRAWLER_TYPE == "detail":
            await self.get_specified_notes()

        logger.info("[XiaoHongShuCrawler.start] Xhs Crawler finished ...")

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        logger.info("[XiaoHongShuCrawler.launch_browser] Begin create browser context ...")
        # This class is XHS-only: its profile is a per-run isolation boundary,
        # not optional cross-run login persistence controlled by
        # SAVE_LOGIN_STATE.
        user_data_dir = self._profile_dir()
        browser_context = await chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            accept_downloads=True,
            headless=headless,
            proxy=playwright_proxy,  # type: ignore
            viewport={
                "width": 1920,
                "height": 1080
            },
            user_agent=user_agent,
            args=project_browser_args(),
        )
        return browser_context

    async def launch_browser_with_cdp(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch one CDP browser or fail without a second launch path."""
        manager = self.ports.browser_manager_factory()
        self.cdp_manager = manager
        try:
            browser_context = await manager.launch_and_connect(
                playwright=playwright,
                playwright_proxy=playwright_proxy,
                user_agent=user_agent,
                headless=headless,
            )

            # Display browser information
            browser_info = await manager.get_browser_info()
            logger.info(f"[XiaoHongShuCrawler] CDP browser info: {browser_info}")

            return browser_context

        except Exception as exc:
            if self.cdp_manager is manager:
                self.cdp_manager = None
            detail = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(f"xhs_cdp_browser_launch_failed:{detail}") from exc

    async def close(self, *, force: bool = False):
        """Close browser context"""
        await self._prepare_browser_shutdown()
        try:
            async with asyncio.timeout(20):
                # Special handling if using CDP mode
                if self.cdp_manager:
                    cleanup_result = await self.cdp_manager.cleanup(force=force)
                    if cleanup_result.get("status") != "completed":
                        summary = {
                            "status": cleanup_result.get("status"),
                            "context": cleanup_result.get("context"),
                            "browser": cleanup_result.get("browser"),
                            "process": (
                                cleanup_result.get("process") or {}
                            ).get("status"),
                            "errors": cleanup_result.get("errors") or [],
                        }
                        raise RuntimeError(
                            "xhs_cdp_cleanup_incomplete:"
                            + json.dumps(
                                summary,
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        )
                    self.cdp_manager = None
                else:
                    await self.browser_context.close()
        except asyncio.CancelledError:
            logger.warning(
                "[XiaoHongShuCrawler.close] Browser cleanup was cancelled; "
                "retaining lifecycle handles for audit and retry"
            )
            raise
        except TimeoutError:
            logger.error(
                "[XiaoHongShuCrawler.close] Browser cleanup timed out; "
                "retaining lifecycle handles for audit and retry"
            )
            raise
        except Exception as exc:
            logger.error(
                "[XiaoHongShuCrawler.close] Browser cleanup failed; "
                f"retaining lifecycle handles for audit and retry: {exc}"
            )
            raise
        else:
            logger.info("[XiaoHongShuCrawler.close] Browser context closed ...")
