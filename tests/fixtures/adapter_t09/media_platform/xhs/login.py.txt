# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/login.py
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


import asyncio
import os
import re
import time
from typing import Awaitable, Callable, Optional

from playwright.async_api import BrowserContext, Page

from base.base_crawler import AbstractLogin
from tools import utils

from .exception import PlatformRuntimeError
from .manual_wait import XHSManualWaitBudget


class XiaoHongShuLogin(AbstractLogin):

    _MIN_QR_REFRESH_SECONDS = 180
    _DEFAULT_LOGIN_POLL_SECONDS = 1.0
    _DEFAULT_STABLE_LOGIN_SECONDS = 5.0
    _QR_COMPONENT_REFRESH_RETRY_SECONDS = 5.0
    _SMS_PARAMETER_ERROR_MARKERS = frozenset({
        "Parameter error",
        "参数错误",
    })
    _SMS_DAILY_LIMIT_MARKERS = frozenset({
        "今日次数已达上限",
        "今日获取验证码次数已达上限",
        "今日验证码获取次数已达上限",
        "今日发送验证码次数已达上限",
        "今日短信验证码次数已达上限",
        "获取验证码次数已达上限",
        "验证码获取次数已达上限",
    })
    _SMS_RATE_LIMIT_MARKERS = frozenset({
        "操作频繁",
        "请求频繁",
        "Requests too frequent",
    })
    _GENERIC_SECURITY_MARKERS = frozenset({"安全限制"})
    _ACCOUNT_EXCEPTION_MARKERS = frozenset({
        "账号异常",
        "Account exception",
    })
    _VERIFICATION_CONTEXT_TEXTS = (
        "SMS Verification",
        "短信验证",
        "短信验证码",
    )
    _LOGIN_OR_QR_TEXTS = (
        "扫码登录",
        "二维码",
        "打开小红书扫一扫",
        "手机号登录",
    )
    _STRONG_MANUAL_PROGRESS_TEXTS = (
        "已扫码",
        "请在手机上确认",
        "请在小红书App确认",
        "请在小红书 APP 确认",
        "手机确认",
        "等待确认",
        "请通过验证",
        "安全验证",
        "身份验证",
        "滑块验证",
        "拖动滑块",
        "SMS Verification",
    )
    _CONDITIONAL_MANUAL_PROGRESS_TEXTS = (
        "确认登录",
        "登录确认",
        "验证码",
    )
    _QR_EXPIRED_TEXTS = (
        "二维码已失效",
        "二维码已过期",
        "点击刷新",
        "重新获取二维码",
    )
    _CONDITIONAL_MANUAL_PROGRESS_SELECTORS = (
        "input[autocomplete='one-time-code']",
        "input[placeholder*='验证码']",
    )
    _STRONG_MANUAL_PROGRESS_SELECTORS = (
        "input[placeholder*='安全验证']",
        "[class*='captcha'] input",
        "[class*='verify'] input",
        "[class*='verification'] input",
        "[class*='slider']",
        "[class*='captcha'] canvas",
    )
    _QR_COMPONENT_REFRESH_SELECTORS = (
        "xpath=//*[normalize-space()='点击刷新']",
        "xpath=//*[normalize-space()='重新获取二维码']",
    )
    _PROFILE_SELECTORS = (
        "xpath=//a[contains(@href, '/user/profile/')]"
        "[.//*[normalize-space()='我'] or normalize-space()='我']",
        "xpath=//*[self::a or self::button]"
        "[.//*[normalize-space()='我'] or normalize-space()='我']",
    )
    _QRCODE_SELECTOR = (
        "xpath=//img[contains(concat(' ', normalize-space(@class), ' '), "
        "' qrcode-img ')]"
    )

    def __init__(self,
                 login_type: str,
                 browser_context: BrowserContext,
                 context_page: Page,
                 close_page: Optional[Callable[..., Awaitable[None]]] = None,
                 new_page: Optional[Callable[[], Awaitable[Page]]] = None,
                 manual_wait_budget: Optional[XHSManualWaitBudget] = None,
                 ):
        if login_type != "qrcode":
            raise ValueError("xhs_login_type_must_be_qrcode")
        self.browser_context = browser_context
        self.context_page = context_page
        self.close_page = close_page
        self.new_page = new_page
        self._manual_wait_budget = manual_wait_budget
        self._last_login_observation: dict[str, object] = {}
        self._manual_progress_observed = False
        self._last_checkpoint_kind = "initial_qrcode_login"

    def _get_manual_wait_budget(self) -> XHSManualWaitBudget:
        if self._manual_wait_budget is None:
            self._manual_wait_budget = XHSManualWaitBudget.from_environment(
                monotonic=lambda: time.monotonic(),
            )
        return self._manual_wait_budget

    async def _new_login_page(self) -> Page:
        if self.new_page:
            return await self.new_page()
        return await self.browser_context.new_page()

    async def _close_extra_login_page(self, page: Page) -> None:
        if self.close_page:
            await self.close_page(page, reason="login_tab_normalization")
            return
        try:
            await page.bring_to_front()
        except Exception:
            pass
        utils.logger.warning(
            "[XiaoHongShuLogin] Unexpected login tab detected; keeping it visible "
            "for at least 30s before close."
        )
        await asyncio.sleep(30)
        if not page.is_closed():
            await page.close()

    async def _single_login_page(self) -> Page:
        """Return the active tab without closing any login/verification page."""
        try:
            pages = [page for page in self.browser_context.pages if not page.is_closed()]
        except Exception as exc:
            raise RuntimeError("xhs_login_browser_context_unavailable") from exc

        if not pages:
            raise RuntimeError("xhs_login_browser_pages_closed")

        page = (
            self.context_page
            if self.context_page in pages
            else pages[-1]
        )
        if len(pages) > 1:
            utils.logger.info(
                "[XiaoHongShuLogin] Preserving all login/verification tabs "
                f"until login completes: {len(pages)} open tab(s)."
            )
        self.context_page = page
        return page

    async def _login_pages(self) -> list[Page]:
        try:
            pages = [
                page
                for page in self.browser_context.pages
                if not page.is_closed()
            ]
        except Exception as exc:
            raise RuntimeError("xhs_login_browser_context_unavailable") from exc
        if not pages:
            raise RuntimeError("xhs_login_browser_pages_closed")
        return pages

    @staticmethod
    async def _selector_is_visible(page: Page, selector: str) -> bool:
        frames = getattr(page, "frames", None)
        targets = list(frames) if frames else [page]
        for target in targets:
            try:
                if await target.locator(selector).is_visible(timeout=500):
                    return True
            except Exception:
                continue
        try:
            return bool(await page.is_visible(selector, timeout=500))
        except Exception:
            return False

    @classmethod
    async def _any_selector_is_visible(
        cls,
        page: Page,
        selectors: tuple[str, ...],
    ) -> bool:
        for selector in selectors:
            if await cls._selector_is_visible(page, selector):
                return True
        return False

    @staticmethod
    async def _visible_page_text(page: Page) -> str:
        """Read rendered text so hidden fallback login DOM is not evidence."""
        parts: list[str] = []
        frames = getattr(page, "frames", None)
        targets = list(frames) if frames else [page]
        locator_supported = False
        for target in targets:
            locator = getattr(target, "locator", None)
            if not callable(locator):
                continue
            locator_supported = True
            try:
                body = locator("body")
                if await body.count() > 0:
                    parts.append(await body.inner_text(timeout=750))
            except Exception:
                continue
        if locator_supported:
            return "\n".join(parts)

        # Test doubles and older Page shims may lack Locator. Real Playwright
        # pages always use rendered text above.
        try:
            return await page.content()
        except Exception:
            return ""

    @staticmethod
    def _matching_markers(text: str, markers: frozenset[str]) -> set[str]:
        lowered = text.casefold()
        return {
            marker
            for marker in markers
            if marker.casefold() in lowered
        }

    @classmethod
    def classify_terminal_state(
        cls,
        *,
        text: str,
        url: str,
        sms_verification_context: bool,
    ) -> tuple[str, str, list[str]]:
        """Return one mutually exclusive terminal code and its failure family."""

        terminal_source = f"{text}\n{url}"
        if re.search(r"(?<!\d)300012(?!\d)", terminal_source):
            return "ip_blocked_300012", "ip_blocked", ["300012"]
        if re.search(r"(?<!\d)300011(?!\d)", terminal_source):
            return (
                "platform_security_limit_300011",
                "platform_security_limit",
                ["300011"],
            )

        if sms_verification_context:
            parameter_markers = cls._matching_markers(
                text,
                cls._SMS_PARAMETER_ERROR_MARKERS,
            )
            if parameter_markers:
                return (
                    "xhs_sms_verification_parameter_error",
                    "sms_verification_terminal",
                    sorted(parameter_markers),
                )
            daily_markers = cls._matching_markers(
                text,
                cls._SMS_DAILY_LIMIT_MARKERS,
            )
            if daily_markers:
                return (
                    "xhs_sms_verification_daily_limit",
                    "sms_verification_terminal",
                    sorted(daily_markers),
                )
            rate_markers = cls._matching_markers(
                text,
                cls._SMS_RATE_LIMIT_MARKERS,
            )
            if rate_markers:
                return (
                    "xhs_sms_verification_rate_limited",
                    "sms_verification_terminal",
                    sorted(rate_markers),
                )

        account_markers = cls._matching_markers(
            text,
            cls._ACCOUNT_EXCEPTION_MARKERS,
        )
        if account_markers:
            return (
                "xhs_account_exception",
                "platform_security_limit",
                sorted(account_markers),
            )
        if "/website-login/error" in url:
            return (
                "xhs_login_error_page",
                "platform_security_limit",
                ["website-login/error"],
            )
        security_markers = cls._matching_markers(
            text,
            cls._GENERIC_SECURITY_MARKERS,
        )
        if security_markers:
            return (
                "xhs_platform_security_limit_unspecified",
                "platform_security_limit",
                sorted(security_markers),
            )
        return "", "", []

    @staticmethod
    def _checkpoint_kind(observation: dict[str, object]) -> str:
        terminal_code = str(observation.get("terminal_code") or "")
        if terminal_code.startswith("xhs_sms_verification_"):
            return "sms_verification"
        if terminal_code == "ip_blocked_300012":
            return "ip_block"
        if terminal_code:
            return "security_verification"

        pages = [
            item
            for item in observation.get("pages") or []
            if isinstance(item, dict)
        ]
        progress = {
            str(marker).casefold()
            for item in pages
            for marker in item.get("manual_progress") or []
        }
        page_urls = {
            str(item.get("url") or "").casefold()
            for item in pages
        }
        if any(
            "sms verification" in marker or "短信" in marker
            for marker in progress
        ):
            return "sms_verification"
        if (
            any(
                token in marker
                for marker in progress
                for token in ("安全验证", "身份验证", "滑块", "captcha")
            )
            or any("/website-login/captcha" in url for url in page_urls)
            or any(item.get("strong_control_visible") for item in pages)
        ):
            return "captcha"
        if (
            any("验证码" in marker for marker in progress)
            or any(item.get("conditional_control_visible") for item in pages)
        ):
            return "sms_verification"
        if any(
            token in marker
            for marker in progress
            for token in ("已扫码", "手机", "确认登录", "登录确认", "等待确认")
        ):
            return "mobile_confirmation"
        if observation.get("qr_visible") or observation.get("qr_expired"):
            return "qrcode_waiting"
        if observation.get("visible_checkpoint"):
            return "login_required"
        return "unknown"

    def _remember_observation(self, observation: dict[str, object]) -> None:
        checkpoint_kind = self._checkpoint_kind(observation)
        manual_in_progress = bool(observation.get("manual_in_progress"))
        if manual_in_progress:
            self._manual_progress_observed = True
        if (
            observation.get("terminal_code")
            or manual_in_progress
            or not self._manual_progress_observed
        ):
            self._last_checkpoint_kind = checkpoint_kind

    def terminal_context(self) -> dict[str, object]:
        """Return a serialization-safe description of the last login checkpoint."""

        observation = self._last_login_observation
        matched_markers = {
            str(marker)
            for marker in observation.get("terminal_markers") or []
        }
        if not matched_markers:
            for page in observation.get("pages") or []:
                if not isinstance(page, dict):
                    continue
                for key in ("manual_progress", "login_or_qr", "qr_expired"):
                    matched_markers.update(
                        str(marker) for marker in page.get(key) or []
                    )
        return {
            "checkpoint_kind": self._last_checkpoint_kind,
            "manual_progress_observed": self._manual_progress_observed,
            "matched_markers": sorted(matched_markers),
        }

    def terminal_failure_type(self) -> str:
        """Return the failure family attached to the last visible terminal."""

        return str(
            self._last_login_observation.get("terminal_failure_type") or ""
        )

    async def _page_login_observation(self, page: Page) -> dict[str, object]:
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        text = await self._visible_page_text(page)
        qr_visible = await self._selector_is_visible(
            page,
            self._QRCODE_SELECTOR,
        )
        conditional_control = await self._any_selector_is_visible(
            page,
            self._CONDITIONAL_MANUAL_PROGRESS_SELECTORS,
        )
        strong_control = await self._any_selector_is_visible(
            page,
            self._STRONG_MANUAL_PROGRESS_SELECTORS,
        )
        strong_progress = {
            marker
            for marker in self._STRONG_MANUAL_PROGRESS_TEXTS
            if marker.casefold() in text.casefold()
        }
        conditional_progress = {
            marker
            for marker in self._CONDITIONAL_MANUAL_PROGRESS_TEXTS
            if marker in text
        }
        qr_expired = {
            marker for marker in self._QR_EXPIRED_TEXTS if marker in text
        }
        # Generic code UI may be the untouched page's alternative login method.
        # It becomes progress only after the QR has left the visible UI. CAPTCHA
        # and SMS-specific evidence remains strong proof.
        manual_in_progress = bool(
            strong_progress
            or strong_control
            or (
                (conditional_progress or conditional_control)
                and not qr_visible
                and not qr_expired
            )
        )
        login_or_qr = {
            marker for marker in self._LOGIN_OR_QR_TEXTS if marker in text
        }
        sms_verification_context = bool(
            conditional_control
            or any(
                marker.casefold() in text.casefold()
                for marker in self._VERIFICATION_CONTEXT_TEXTS
            )
        )
        terminal_code, terminal_failure_type, terminal_markers = (
            self.classify_terminal_state(
                text=text,
                url=url,
                sms_verification_context=sms_verification_context,
            )
        )
        profile_visible = await self._any_selector_is_visible(
            page,
            self._PROFILE_SELECTORS,
        )
        return {
            "page": page,
            "url": url,
            "terminal_code": terminal_code,
            "terminal_failure_type": terminal_failure_type,
            "terminal_markers": terminal_markers,
            "manual_progress": sorted(strong_progress | conditional_progress),
            "conditional_control_visible": conditional_control,
            "strong_control_visible": strong_control,
            "manual_control_visible": bool(conditional_control or strong_control),
            "manual_in_progress": manual_in_progress,
            "login_or_qr": sorted(login_or_qr),
            "qr_visible": qr_visible,
            "qr_expired": sorted(qr_expired),
            "profile_visible": profile_visible,
        }

    async def _login_observation(self) -> dict[str, object]:
        page_observations = [
            await self._page_login_observation(page)
            for page in await self._login_pages()
        ]
        terminal_pages = [
            item for item in page_observations if item["terminal_code"]
        ]
        progress_pages = [
            item for item in page_observations if item["manual_in_progress"]
        ]
        profile_pages = [
            item for item in page_observations if item["profile_visible"]
        ]
        initial_checkpoint_pages = [
            item
            for item in page_observations
            if item["login_or_qr"] or item["qr_visible"]
        ]
        # A stale QR-only tab may coexist with the newly signed-in primary tab.
        # Manual progress on any tab wins; stale QR on another tab does not
        # block a profile UI that is otherwise clear.
        profile_page_checkpoint = any(
            bool(item["login_or_qr"] or item["qr_visible"])
            for item in profile_pages
        )
        visible_checkpoint = bool(
            progress_pages
            or profile_page_checkpoint
            or (not profile_pages and initial_checkpoint_pages)
        )
        active = (progress_pages or profile_pages or page_observations)[-1]
        active_page = active["page"]
        if progress_pages and active_page is not self.context_page:
            try:
                await active_page.bring_to_front()
            except Exception:
                pass
            self.context_page = active_page
        return {
            "terminal_code": str(
                terminal_pages[0]["terminal_code"] if terminal_pages else ""
            ),
            "terminal_failure_type": str(
                terminal_pages[0]["terminal_failure_type"]
                if terminal_pages
                else ""
            ),
            "terminal_markers": list(
                terminal_pages[0]["terminal_markers"] if terminal_pages else []
            ),
            "manual_in_progress": bool(progress_pages),
            "visible_checkpoint": visible_checkpoint,
            "profile_visible": bool(profile_pages),
            "qr_visible": any(
                bool(item["qr_visible"]) for item in page_observations
            ),
            "qr_expired": any(
                bool(item["qr_expired"]) for item in page_observations
            ),
            "pages": page_observations,
        }

    @staticmethod
    def _is_pure_qr_observation(observation: dict[str, object]) -> bool:
        if (
            observation.get("terminal_code")
            or observation.get("manual_in_progress")
            or observation.get("profile_visible")
        ):
            return False
        pages = observation.get("pages") or []
        return bool(
            observation.get("qr_visible")
            or any(item.get("login_or_qr") for item in pages)
        )

    @classmethod
    def _is_pure_expired_qr_observation(
        cls,
        observation: dict[str, object],
    ) -> bool:
        if not cls._is_pure_qr_observation(observation):
            return False
        return bool(
            observation.get("qr_expired")
            or any(
                item.get("qr_expired")
                for item in observation.get("pages") or []
            )
        )

    async def _click_expired_qr_component_refresh(
        self,
        observation: dict[str, object],
    ) -> bool:
        """Click only an explicit refresh control on an expired pure QR."""
        expired_pages = [
            item["page"]
            for item in observation.get("pages") or []
            if item.get("qr_expired")
        ]
        for page in reversed(expired_pages):
            frames = getattr(page, "frames", None)
            targets = list(frames) if frames else [page]
            for target in targets:
                locator_factory = getattr(target, "locator", None)
                if not callable(locator_factory):
                    continue
                for selector in self._QR_COMPONENT_REFRESH_SELECTORS:
                    try:
                        candidate = locator_factory(selector)
                        locator = getattr(candidate, "first", candidate)
                        if not await locator.is_visible(timeout=500):
                            continue
                        await locator.click(timeout=5_000)
                        self.context_page = page
                        utils.logger.info(
                            "[XiaoHongShuLogin.login_by_qrcode] Expired pure QR "
                            "confirmed twice; clicked its component refresh control."
                        )
                        return True
                    except Exception:
                        continue
        return False

    async def _check_login_state_once(self, no_logged_in_session: str) -> bool:
        """Confirm login only from visible UI across the one BrowserContext."""
        observation = await self._login_observation()
        self._last_login_observation = observation
        self._remember_observation(observation)
        terminal_code = str(observation["terminal_code"] or "")
        if terminal_code:
            raise PlatformRuntimeError(terminal_code, code=terminal_code)

        # A stale profile shell can remain behind an active checkpoint. Every
        # visible verification state wins over every visible profile entry.
        if observation["visible_checkpoint"]:
            page_summaries = [
                {
                    "url": item["url"],
                    "manual_progress": item["manual_progress"],
                    "manual_control_visible": item["manual_control_visible"],
                    "login_or_qr": item["login_or_qr"],
                    "qr_visible": item["qr_visible"],
                }
                for item in observation["pages"]
            ]
            utils.logger.info(
                "[XiaoHongShuLogin.check_login_state] Visible login/security "
                f"checkpoint, please verify manually: {page_summaries}"
            )
        elif observation["profile_visible"]:
            utils.logger.info(
                "[XiaoHongShuLogin.check_login_state] Login status confirmed by "
                "visible profile UI without a login/security checkpoint."
            )
            return True

        # Cookie mutation is evidence only; it cannot end the login wait or
        # trigger browser shutdown before stable signed-in UI appears.
        current_cookie = await self.browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(current_cookie)
        current_web_session = cookie_dict.get("web_session")
        if (
            no_logged_in_session
            and current_web_session
            and current_web_session != no_logged_in_session
        ):
            utils.logger.info(
                "[XiaoHongShuLogin.check_login_state] web_session changed, "
                "waiting for logged-in UI before confirming login."
            )
        return False

    async def begin(self):
        """Start the only supported XHS login flow."""
        utils.logger.info("[XiaoHongShuLogin.begin] Begin login xiaohongshu ...")
        await self._single_login_page()
        await self.login_by_qrcode()

    async def login_by_qrcode(self):
        """login xiaohongshu website and keep webdriver login state"""
        utils.logger.info("[XiaoHongShuLogin.login_by_qrcode] Begin login xiaohongshu by qrcode ...")
        requested_refresh_seconds = int(
            os.environ.get(
                "TRIPPOSTCOLLECT_XHS_QR_REFRESH_SECONDS",
                str(self._MIN_QR_REFRESH_SECONDS),
            )
        )
        refresh_seconds = max(
            self._MIN_QR_REFRESH_SECONDS,
            requested_refresh_seconds,
        )
        if refresh_seconds != requested_refresh_seconds:
            utils.logger.warning(
                "[XiaoHongShuLogin.login_by_qrcode] QR refresh interval "
                f"{requested_refresh_seconds}s is unsafe; clamped to "
                f"{refresh_seconds}s."
            )
        poll_seconds = max(
            0.2,
            float(
                os.environ.get(
                    "TRIPPOSTCOLLECT_XHS_LOGIN_POLL_SECONDS",
                    str(self._DEFAULT_LOGIN_POLL_SECONDS),
                )
            ),
        )
        stable_seconds = max(
            poll_seconds,
            float(
                os.environ.get(
                    "TRIPPOSTCOLLECT_XHS_STABLE_LOGIN_SECONDS",
                    str(self._DEFAULT_STABLE_LOGIN_SECONDS),
                )
            ),
        )
        budget = self._get_manual_wait_budget()
        ticket = budget.start("initial_qrcode_login")
        try:
            current_cookie = await self.browser_context.cookies()
            _, cookie_dict = utils.convert_cookies(current_cookie)
            no_logged_in_session = cookie_dict.get("web_session")

            next_refresh_at: Optional[float] = None
            progress_latched = False
            qrcode_displayed = False
            refresh_count = 0
            stable_started_at: Optional[float] = None
            component_refresh_pending = False
            next_component_refresh_retry_at: Optional[float] = None

            utils.logger.info(
                "[XiaoHongShuLogin.login_by_qrcode] Waiting for operator login: "
                f"manual_remaining={ticket.remaining_seconds:.1f}s, "
                f"qr_refresh_interval={refresh_seconds}s."
            )
            while True:
                ticket.raise_if_exhausted()
                logged_in = await self._check_login_state_once(
                    no_logged_in_session
                )
                observation = self._last_login_observation
                ticket.raise_if_exhausted()
                if observation.get("manual_in_progress") and not progress_latched:
                    progress_latched = True
                    utils.logger.info(
                        "[XiaoHongShuLogin.login_by_qrcode] Manual login/verification "
                        "progress observed; disabling QR reload for the remainder "
                        "of this bounded login window."
                    )
                if logged_in and not progress_latched:
                    progress_latched = True
                    utils.logger.info(
                        "[XiaoHongShuLogin.login_by_qrcode] Signed-in UI observed; "
                        "disabling every QR refresh path while stability is confirmed."
                    )

                now = budget.now()
                if logged_in:
                    if stable_started_at is None:
                        stable_started_at = now
                        utils.logger.info(
                            "[XiaoHongShuLogin.login_by_qrcode] Signed-in UI "
                            f"observed; requiring {stable_seconds:.1f}s stable "
                            "confirmation."
                        )
                    elif now - stable_started_at >= stable_seconds:
                        utils.logger.info(
                            "[XiaoHongShuLogin.login_by_qrcode] Login confirmed "
                            f"stable for {now - stable_started_at:.1f}s."
                        )
                        return
                else:
                    stable_started_at = None

                if (
                    component_refresh_pending
                    and self._is_pure_qr_observation(observation)
                    and not self._is_pure_expired_qr_observation(observation)
                ):
                    # A component refresh produced a fresh QR. Its first ready
                    # observation starts a new 180-second full-page reload floor.
                    component_refresh_pending = False
                    next_component_refresh_retry_at = None
                    qrcode_displayed = False
                    next_refresh_at = None

                if not qrcode_displayed and not progress_latched and not logged_in:
                    base64_qrcode_img = await utils.find_login_qrcode(
                        self.context_page,
                        selector=self._QRCODE_SELECTOR,
                    )
                    ticket.raise_if_exhausted()
                    if (
                        not base64_qrcode_img
                        and not observation.get("qr_expired")
                    ):
                        utils.logger.info(
                            "[XiaoHongShuLogin.login_by_qrcode] QR code not found, "
                            "trying to open login dialog ..."
                        )
                        ticket.raise_if_exhausted()
                        try:
                            login_button_ele = self.context_page.locator(
                                "xpath=//*[@id='app']/div[1]/div[2]/div[1]/ul/"
                                "div[1]/button"
                            )
                            await login_button_ele.click(timeout=5_000)
                            remaining = ticket.remaining_seconds
                            if remaining > 0:
                                await asyncio.sleep(min(0.5, remaining))
                        except Exception as exc:
                            utils.logger.warning(
                                "[XiaoHongShuLogin.login_by_qrcode] open login "
                                f"dialog failed: {exc}"
                            )
                        ticket.raise_if_exhausted()
                        base64_qrcode_img = await utils.find_login_qrcode(
                            self.context_page,
                            selector=self._QRCODE_SELECTOR,
                        )
                        ticket.raise_if_exhausted()
                    if base64_qrcode_img and not observation.get("qr_expired"):
                        qrcode_displayed = True
                        next_refresh_at = budget.now() + refresh_seconds
                        utils.logger.info(
                            "[XiaoHongShuLogin.login_by_qrcode] QR code ready in browser; "
                            "automatic page reload is not allowed before "
                            f"{refresh_seconds}s."
                        )

                now = budget.now()
                if (
                    not progress_latched
                    and next_refresh_at is None
                    and observation.get("qr_visible")
                    and not observation.get("qr_expired")
                ):
                    # Visible QR is enough to start its lifetime even when byte
                    # extraction for a headless preview fails.
                    next_refresh_at = now + refresh_seconds

                if (
                    not progress_latched
                    and not logged_in
                    and self._is_pure_expired_qr_observation(observation)
                    and (
                        not component_refresh_pending
                        or next_component_refresh_retry_at is None
                        or now >= next_component_refresh_retry_at
                    )
                ):
                    # An expired pure QR may use only its own explicit refresh
                    # control, and only after a second observation closes the
                    # scan/confirm transition race.
                    confirmed_logged_in = await self._check_login_state_once(
                        no_logged_in_session
                    )
                    confirmed_observation = self._last_login_observation
                    observation = confirmed_observation
                    if confirmed_observation.get("manual_in_progress"):
                        progress_latched = True
                        utils.logger.info(
                            "[XiaoHongShuLogin.login_by_qrcode] Manual progress "
                            "appeared while confirming QR expiry; component "
                            "refresh cancelled and permanently disabled."
                        )
                    elif confirmed_logged_in:
                        progress_latched = True
                        utils.logger.info(
                            "[XiaoHongShuLogin.login_by_qrcode] Signed-in UI "
                            "appeared while confirming QR expiry; component "
                            "refresh cancelled."
                        )
                    elif self._is_pure_expired_qr_observation(
                        confirmed_observation
                    ):
                        ticket.raise_if_exhausted()
                        component_clicked = (
                            await self._click_expired_qr_component_refresh(
                                confirmed_observation
                            )
                        )
                        if component_clicked:
                            component_refresh_pending = True
                            next_component_refresh_retry_at = (
                                budget.now()
                                + self._QR_COMPONENT_REFRESH_RETRY_SECONDS
                            )
                            qrcode_displayed = False

                if (
                    not progress_latched
                    and not component_refresh_pending
                    and next_refresh_at is not None
                    and now >= next_refresh_at
                ):
                    # Both observations at the boundary must remain pure QR. Any
                    # scan/SMS/security transition permanently disables reload.
                    refresh_allowed = self._is_pure_qr_observation(observation)
                    if refresh_allowed:
                        confirmed_logged_in = await self._check_login_state_once(
                            no_logged_in_session
                        )
                        confirmed_observation = self._last_login_observation
                        if confirmed_observation.get("manual_in_progress"):
                            progress_latched = True
                            utils.logger.info(
                                "[XiaoHongShuLogin.login_by_qrcode] Manual progress "
                                "appeared at the QR refresh boundary; reload cancelled."
                            )
                        refresh_allowed = bool(
                            not confirmed_logged_in
                            and not progress_latched
                            and self._is_pure_qr_observation(
                                confirmed_observation
                            )
                        )
                    if refresh_allowed:
                        ticket.raise_if_exhausted()
                        refresh_count += 1
                        utils.logger.info(
                            "[XiaoHongShuLogin.login_by_qrcode] QR code not "
                            f"confirmed; refreshing after at least {refresh_seconds}s "
                            f"(refresh {refresh_count})."
                        )
                        try:
                            await self.context_page.reload(
                                wait_until="domcontentloaded",
                                timeout=30_000,
                            )
                        except Exception as exc:
                            utils.logger.warning(
                                "[XiaoHongShuLogin.login_by_qrcode] page reload "
                                f"failed: {exc}"
                            )
                        qrcode_displayed = False
                        next_refresh_at = None
                        component_refresh_pending = False
                        next_component_refresh_retry_at = None
                    elif not progress_latched:
                        next_refresh_at = budget.now() + poll_seconds
                        utils.logger.info(
                            "[XiaoHongShuLogin.login_by_qrcode] QR refresh deferred: "
                            "two consecutive pure-QR observations were unavailable."
                        )

                remaining = ticket.remaining_seconds
                if remaining <= 0:
                    ticket.raise_if_exhausted()
                await asyncio.sleep(min(poll_seconds, remaining))
        finally:
            ticket.close()
