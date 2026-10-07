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

# TripPostCollect：资源与 profile 基目录不再依赖进程 cwd。
from trippostcollect.core import resources
from trippostcollect.core.paths import MEDIACRAWLER_DIR

import asyncio
import json
import os
import random
import re
import time
from asyncio import Task
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qsl, quote, urlparse

from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from tenacity import RetryError

import config
from base.base_crawler import AbstractCrawler
from model.m_xiaohongshu import NoteUrlInfo, CreatorUrlInfo
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import xhs as xhs_store
from tools import utils
from tools.image_download_retry import (
    ImageDownloadFetchError,
    fetch_image_bytes_with_retry,
    is_runtime_blocking_image_error,
)
from tools.image_manifest import ImageStagingError
from tools.trippostcollect_behavior import (
    inspect_visible_page_state,
    install_project_runtime_hints,
    project_browser_args,
    record_platform_security_limit,
    run_required_continuity_behavior,
    run_required_human_behavior,
    run_required_request_pause,
    run_requested_post_interaction,
)
from tools.trippostcollect_adaptive import (
    AdaptiveAccumulator,
    append_execution_event,
    env_int,
)
from tools.cdp_browser import CDPBrowserLifecycleError, CDPBrowserManager
from trippostcollect.records.topic_relevance import (
    topic_relevant_for_web_post,
)
from var import crawler_type_var, source_keyword_var

from .client import (
    XiaoHongShuClient,
    is_recoverable_xhs_transport_failure,
    unwrap_xhs_request_failure,
)
from .exception import (
    DataFetchError,
    IPBlockError,
    NoteNotFoundError,
    PlatformRuntimeError,
)
from .field import SearchSortType
from .help import parse_note_info_from_note_url, parse_creator_info_from_url, get_search_id
from .login import XiaoHongShuLogin
from .manual_wait import XHSManualWaitBudget, XHSManualWaitBudgetExhausted


XHS_NEW_PAGE_MIN_HOLD_SECONDS = 30.0
_XHS_REMOVED_LOGIN_ENV_VARS = (
    "TRIPPOSTCOLLECT_XHS_RUN_SCOPED_LOGIN",
    "TRIPPOSTCOLLECT_XHS_STORAGE_STATE_PATH",
    "TRIPPOSTCOLLECT_COOKIES",
)
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
_XHS_RECOVERABLE_NAVIGATION_ERROR_MARKERS = (
    "net::err_internet_disconnected",
    "net::err_network_changed",
    "net::err_name_not_resolved",
    "net::err_connection_reset",
    "net::err_connection_closed",
    "net::err_connection_refused",
    "net::err_timed_out",
    "net::err_address_unreachable",
    "net::err_proxy_connection_failed",
    "net::err_tunnel_connection_failed",
)
_XHS_CDP_LIFECYCLE_STOP_DETAILS = {
    "xhs_browser_process_exited": "browser_process_exited",
    "xhs_cdp_disconnected_unexpected": "cdp_disconnected",
    "xhs_browser_context_closed_unexpected": "browser_context_closed",
}


def xhs_cdp_lifecycle_stop_detail(exc: CDPBrowserLifecycleError) -> str:
    return _XHS_CDP_LIFECYCLE_STOP_DETAILS.get(
        str(exc.event.get("code") or ""),
        "browser_runtime_failed",
    )


def is_recoverable_xhs_navigation_failure(exc: BaseException) -> bool:
    detail = str(exc).casefold()
    if exc.__class__.__name__ == "TargetClosedError" or any(
        marker in detail
        for marker in (
            "target page, context or browser has been closed",
            "context or browser has been closed",
            "browser has been closed",
            "browser has disconnected",
            "browser disconnected",
            "playwright connection closed",
            "connection closed while reading from the driver",
        )
    ):
        return False
    if isinstance(exc, (PlaywrightTimeoutError, TimeoutError)):
        return True
    if not isinstance(exc, PlaywrightError):
        return False
    return any(
        marker in detail for marker in _XHS_RECOVERABLE_NAVIGATION_ERROR_MARKERS
    )


class XHSImageDownloadError(RuntimeError):
    """An XHS post image exhausted its applicable fetch attempts."""

    def __init__(self, note_id: str, source_index: int, code: str, attempts: int = 1):
        super().__init__(
            f"XHS image download failed: note_id={note_id}, "
            f"source_index={source_index}, code={code}"
        )
        self.note_id = note_id
        self.source_index = source_index
        self.code = code
        self.attempts = max(1, int(attempts))


class XHSNoteDetailUnavailable(RuntimeError):
    """An XHS note detail remained unavailable after bounded attempts."""

    def __init__(self, note_id: str, code: str, attempts: int = 1):
        super().__init__(
            f"XHS note detail unavailable: note_id={note_id or '<missing>'}, code={code}"
        )
        self.note_id = note_id
        self.code = code
        self.attempts = max(1, int(attempts))


class XHSCreatorProfileUnavailable(RuntimeError):
    """An XHS creator profile remained unavailable after observed attempts."""

    def __init__(self, user_id: str, attempts: int):
        super().__init__(
            "creator_profile_unavailable_after_retry:"
            f"user_id={user_id or '<missing>'}:attempts={attempts}"
        )
        self.user_id = user_id
        self.attempts = max(1, int(attempts))


class XHSNetworkRecoveryTimeout(RuntimeError):
    """A recoverable transport outage outlived the configured same-run wait."""

    def __init__(self, stage: str, elapsed_seconds: float):
        super().__init__(
            "xhs_network_recovery_timeout:"
            f"stage={stage}:elapsed={max(0.0, elapsed_seconds):.1f}s"
        )
        self.stage = stage
        self.elapsed_seconds = max(0.0, elapsed_seconds)


class XiaoHongShuCrawler(AbstractCrawler):
    context_page: Page
    xhs_client: XiaoHongShuClient
    browser_context: BrowserContext
    cdp_manager: Optional[CDPBrowserManager]

    def __init__(self) -> None:
        self.index_url = "https://www.rednote.com" if config.XHS_INTERNATIONAL else "https://www.xiaohongshu.com"
        self.explore_url = f"{self.index_url}/explore"
        self.cookie_urls = [self.index_url]
        self.user_agent: Optional[str] = None
        self.cdp_manager = None
        self._browser_session_started = False
        self.ip_proxy_pool = None  # Proxy IP pool for automatic proxy refresh
        self.post_interaction_mode = os.environ.get("TRIPPOSTCOLLECT_XHS_POST_INTERACTION", "none").strip()
        self.post_interaction_attempted = False
        self.creator_profile_cache: Dict[str, Dict] = {}
        self._crawler_page_open_depth = 0
        self._initial_pages: Dict[int, Page] = {}
        self._new_pages: Dict[int, tuple[Page, float, str]] = {}
        self._new_page_guard_tasks: Dict[int, Task[None]] = {}
        self._navigation_diagnostics: List[Dict] = []
        self._navigation_observed_pages: set[int] = set()
        self._navigation_page_errors: List[Dict] = []
        self._navigation_request_failures: List[Dict] = []
        self._manual_wait_budget: Optional[XHSManualWaitBudget] = None

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        try:
            return max(0.0, float(os.environ.get(name, str(default))))
        except ValueError:
            return default

    @staticmethod
    def _validate_login_contract() -> None:
        for env_name in _XHS_REMOVED_LOGIN_ENV_VARS:
            if env_name in os.environ:
                raise RuntimeError(f"xhs_deprecated_login_input:{env_name}")
        if config.LOGIN_TYPE != "qrcode":
            raise RuntimeError("xhs_login_type_must_be_qrcode")
        if str(config.COOKIES or "").strip():
            raise RuntimeError("xhs_cookie_login_input_removed")

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
            utils.logger.warning(
                "[XiaoHongShuCrawler] Could not bring new tab to front: "
                f"{type(exc).__name__}: {exc}"
            )
        remaining = max(
            0.0,
            XHS_NEW_PAGE_MIN_HOLD_SECONDS
            - (self._popup_monotonic() - opened_at),
        )
        utils.logger.warning(
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
                utils.logger.warning(
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
        utils.logger.info(
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
        event = await run_required_request_pause(stage, minimum, maximum)
        seconds = float(event["seconds"])
        utils.logger.info(f"[XiaoHongShuCrawler] Guarded pause stage={stage} seconds={seconds:.3f}")
        if stage in {"search_results", "search_page"}:
            continuity = await run_required_continuity_behavior(self.context_page, stage)
            utils.logger.info(
                "[XiaoHongShuCrawler] Continuity behavior "
                f"stage={stage} status={continuity.get('status')}"
            )
        return seconds

    @staticmethod
    def _navigation_target_matches(target_url: str, current_url: str) -> bool:
        def normalized(url: str) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
            parsed = urlparse(str(url or ""))
            return (
                parsed.scheme.casefold(),
                parsed.netloc.casefold(),
                parsed.path.rstrip("/") or "/",
                tuple(sorted(parse_qsl(parsed.query, keep_blank_values=True))),
            )

        return bool(current_url) and normalized(target_url) == normalized(current_url)

    async def _goto_with_deadline(
        self,
        page: Page,
        url: str,
        *,
        stage: str,
        wait_until: str = "commit",
    ) -> None:
        self._install_navigation_observers(page)
        timeout_seconds = self._env_float("TRIPPOSTCOLLECT_XHS_NAVIGATION_DEADLINE_SECONDS", 60.0)
        timeout_seconds = max(5.0, timeout_seconds)
        recovery_stage = f"navigation:{stage}"
        recovery_state: Dict[str, object] = {}
        while True:
            response_status: Optional[int] = None
            try:
                async with asyncio.timeout(timeout_seconds + 2.0):
                    response = await page.goto(
                        url,
                        wait_until=wait_until,
                        timeout=int(timeout_seconds * 1000),
                    )
                    response_status = (
                        response.status if response is not None else None
                    )
            except (PlaywrightTimeoutError, TimeoutError) as exc:
                try:
                    current_url = page.url or ""
                except Exception:
                    current_url = ""
                if (
                    "/search_result" in url
                    and self._navigation_target_matches(url, current_url)
                ):
                    await self._abort_network_recovery(
                        stage=recovery_stage,
                        state=recovery_state,
                        error=exc,
                        page=page,
                        outcome="network_recovery_deferred_to_visible_readiness",
                    )
                    await self._record_navigation_diagnostic(
                        page,
                        stage=stage,
                        outcome="commit_timeout_deferred",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    utils.logger.warning(
                        "[XiaoHongShuCrawler] Navigation event timed out after URL "
                        "commit; continuing with visible readiness gate: "
                        f"stage={stage}, url={current_url}"
                    )
                    return
                await self._record_navigation_diagnostic(
                    page,
                    stage=stage,
                    outcome="navigation_network_error",
                    error=f"{type(exc).__name__}: {exc}",
                )
                if await self._pause_for_network_recovery(
                    exc,
                    stage=recovery_stage,
                    state=recovery_state,
                    page=page,
                ):
                    continue
                started_at = float(
                    recovery_state.get("started_at") or self._popup_monotonic()
                )
                raise XHSNetworkRecoveryTimeout(
                    recovery_stage,
                    self._popup_monotonic() - started_at,
                ) from exc
            except PlaywrightError as exc:
                if not is_recoverable_xhs_navigation_failure(exc):
                    await self._abort_network_recovery(
                        stage=recovery_stage,
                        state=recovery_state,
                        error=exc,
                        page=page,
                    )
                    raise
                await self._record_navigation_diagnostic(
                    page,
                    stage=stage,
                    outcome="navigation_network_error",
                    error=f"{type(exc).__name__}: {exc}",
                )
                if await self._pause_for_network_recovery(
                    exc,
                    stage=recovery_stage,
                    state=recovery_state,
                    page=page,
                ):
                    continue
                started_at = float(
                    recovery_state.get("started_at") or self._popup_monotonic()
                )
                raise XHSNetworkRecoveryTimeout(
                    recovery_stage,
                    self._popup_monotonic() - started_at,
                ) from exc

            await self._finish_network_recovery(
                stage=recovery_stage,
                state=recovery_state,
                page=page,
            )
            await self._record_navigation_diagnostic(
                page,
                stage=stage,
                outcome="navigation_committed",
                response_status=response_status,
            )
            return

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _install_navigation_observers(self, page: Page) -> None:
        page_id = id(page)
        if page_id in self._navigation_observed_pages:
            return
        on = getattr(page, "on", None)
        if not callable(on):
            return
        self._navigation_observed_pages.add(page_id)

        def record_page_error(error: object) -> None:
            self._navigation_page_errors.append(
                {
                    "at": self._utc_now(),
                    "message": str(error)[:500],
                }
            )
            self._navigation_page_errors = self._navigation_page_errors[-8:]

        def record_request_failure(request: object) -> None:
            failure = getattr(request, "failure", None)
            request_url = str(getattr(request, "url", "") or "")
            if request_url.startswith("chrome-extension://invalid"):
                return
            self._navigation_request_failures.append(
                {
                    "at": self._utc_now(),
                    "resource_type": str(getattr(request, "resource_type", "") or "")[:80],
                    "url": request_url[:500],
                    "failure": str(failure or "")[:300],
                }
            )
            self._navigation_request_failures = self._navigation_request_failures[-12:]

        on("pageerror", record_page_error)
        on("requestfailed", record_request_failure)

    def _navigation_diagnostics_path(self) -> Optional[Path]:
        evidence_path = os.environ.get(
            "TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE",
            "",
        ).strip()
        if not evidence_path:
            return None
        path = Path(evidence_path).expanduser()
        return path.with_name(f"{path.stem}.navigation.json")

    def _write_navigation_diagnostics(self) -> None:
        path = self._navigation_diagnostics_path()
        if path is None:
            return
        payload = {
            "schema_version": 1,
            "platform": "xhs",
            "updated_at": self._utc_now(),
            "events": self._navigation_diagnostics[-30:],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError as exc:
            utils.logger.warning(
                "[XiaoHongShuCrawler] Could not persist navigation diagnostics: "
                f"{type(exc).__name__}: {exc}"
            )

    async def _page_render_diagnostic(self, page: Page) -> Dict:
        diagnostic: Dict = {
            "url": str(getattr(page, "url", "") or ""),
            "page_errors": list(self._navigation_page_errors[-5:]),
            "request_failures": list(self._navigation_request_failures[-8:]),
        }
        try:
            async with asyncio.timeout(5.0):
                observed = await page.evaluate(
                    """() => ({
                        ready_state: document.readyState || '',
                        title: (document.title || '').slice(0, 160),
                        dom_length: document.documentElement
                            ? document.documentElement.outerHTML.length
                            : 0,
                        body_text_length: document.body
                            ? (document.body.innerText || '').trim().length
                            : 0,
                        body_child_element_count: document.body
                            ? document.body.childElementCount
                            : 0,
                        visibility_state: document.visibilityState || '',
                    })"""
                )
            if isinstance(observed, dict):
                diagnostic.update(observed)
        except Exception as exc:
            diagnostic["inspection_error"] = f"{type(exc).__name__}: {exc}"
        return diagnostic

    async def _record_navigation_diagnostic(
        self,
        page: Page,
        *,
        stage: str,
        outcome: str,
        response_status: Optional[int] = None,
        error: str = "",
    ) -> Dict:
        diagnostic = await self._page_render_diagnostic(page)
        diagnostic.update(
            {
                "at": self._utc_now(),
                "stage": stage,
                "outcome": outcome,
                "response_status": response_status,
                "error": error[:500],
            }
        )
        self._navigation_diagnostics.append(diagnostic)
        self._navigation_diagnostics = self._navigation_diagnostics[-30:]
        self._write_navigation_diagnostics()
        return diagnostic

    async def _wait_for_visible_page_shell(
        self,
        page: Page,
        *,
        stage: str,
        timeout_seconds: float,
    ) -> bool:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        last_diagnostic: Dict = {}
        while True:
            last_diagnostic = await self._page_render_diagnostic(page)
            body_text_length = int(last_diagnostic.get("body_text_length") or 0)
            body_child_count = int(last_diagnostic.get("body_child_element_count") or 0)
            ready_state = str(last_diagnostic.get("ready_state") or "")
            if (
                ready_state in {"interactive", "complete"}
                and body_text_length > 0
                and body_child_count > 0
            ):
                await self._record_navigation_diagnostic(
                    page,
                    stage=stage,
                    outcome="visible_shell_ready",
                )
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                diagnostic = await self._record_navigation_diagnostic(
                    page,
                    stage=stage,
                    outcome="visible_shell_timeout",
                    error="visible_page_shell_not_rendered",
                )
                utils.logger.warning(
                    "[XiaoHongShuCrawler] Visible page shell did not render: "
                    f"stage={stage}, ready_state={diagnostic.get('ready_state')}, "
                    f"body_text_length={diagnostic.get('body_text_length')}, "
                    f"body_child_element_count={diagnostic.get('body_child_element_count')}, "
                    f"url={diagnostic.get('url')}"
                )
                return False
            await asyncio.sleep(min(2.0, remaining))

    async def _open_behavior_search_page(self, keyword: str) -> None:
        search_url = f"{self.index_url}/search_result?keyword={quote(keyword)}"
        await self._goto_with_deadline(
            self.context_page,
            search_url,
            stage="behavior_search",
        )
        search_shell_timeout = self._env_float(
            "TRIPPOSTCOLLECT_XHS_SEARCH_SHELL_TIMEOUT_SECONDS",
            30.0,
        )
        if await self._wait_for_visible_page_shell(
            self.context_page,
            stage="behavior_search",
            timeout_seconds=search_shell_timeout,
        ):
            return

        await self._record_navigation_diagnostic(
            self.context_page,
            stage="behavior_search_recovery",
            outcome="explore_fallback_started",
        )
        await self._goto_with_deadline(
            self.context_page,
            self.explore_url,
            stage="behavior_search_recovery_explore",
        )
        recovery_timeout = self._env_float(
            "TRIPPOSTCOLLECT_XHS_RECOVERY_SHELL_TIMEOUT_SECONDS",
            60.0,
        )
        if not await self._wait_for_visible_page_shell(
            self.context_page,
            stage="behavior_search_recovery_explore",
            timeout_seconds=recovery_timeout,
        ):
            raise RuntimeError("xhs_navigation_recovery_failed:explore_not_rendered")
        await self._goto_with_deadline(
            self.context_page,
            search_url,
            stage="behavior_search_recovery_search",
        )
        if not await self._wait_for_visible_page_shell(
            self.context_page,
            stage="behavior_search_recovery_search",
            timeout_seconds=recovery_timeout,
        ):
            raise RuntimeError("xhs_navigation_recovery_failed:search_not_rendered")
        await self._record_navigation_diagnostic(
            self.context_page,
            stage="behavior_search_recovery",
            outcome="explore_fallback_completed",
        )

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
            utils.logger.warning(f"[XiaoHongShuCrawler] Page close did not finish cleanly: {exc}")

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

    async def _maybe_run_post_interaction(self, note_detail: Dict) -> None:
        if self.post_interaction_mode == "none" or self.post_interaction_attempted:
            return
        self.post_interaction_attempted = True
        note_id = str(note_detail.get("note_id") or "").strip()
        if not note_id:
            utils.logger.warning("[XiaoHongShuCrawler] Skip requested post interaction: missing note_id")
            return
        query = (
            f"xsec_token={quote(str(note_detail.get('xsec_token') or ''))}"
            f"&xsec_source={quote(str(note_detail.get('xsec_source') or 'pc_search'))}"
        )
        interaction_url = f"{self.index_url}/explore/{quote(note_id)}?{query}"
        page = await self._new_guarded_page()
        try:
            await self._goto_with_deadline(
                page,
                interaction_url,
                stage="post_interaction",
            )
            interaction = await run_requested_post_interaction(
                page,
                platform_key="xhs",
                requested_mode=self.post_interaction_mode,
                note_id=note_id,
            )
            utils.logger.info(
                "[XiaoHongShuCrawler] Post interaction "
                f"mode={self.post_interaction_mode} note_id={note_id} status={interaction.get('status')}"
            )
            blocked_markers = {
                key
                for markers_key in ("initial_visible_markers", "visible_markers")
                for key, present in (interaction.get(markers_key) or {}).items()
                if present
            }
            if blocked_markers:
                raise RuntimeError(f"xhs_post_interaction_visible_block:{','.join(sorted(blocked_markers))}")
        except RuntimeError as exc:
            if str(exc).startswith("xhs_post_interaction_visible_block:"):
                raise
            utils.logger.warning(
                f"[XiaoHongShuCrawler] Requested post interaction failed without stopping crawl: {type(exc).__name__}: {exc}"
            )
        except Exception as exc:
            utils.logger.warning(
                f"[XiaoHongShuCrawler] Requested post interaction failed without stopping crawl: {type(exc).__name__}: {exc}"
            )
        finally:
            await self._close_page_with_deadline(
                page,
                reason="post_interaction_cleanup",
            )

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
            raise RuntimeError(f"xhs_main_page_closed_unexpected:stage={stage}")

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
            return await run_required_human_behavior(self.context_page, "xhs")
        except Exception as exc:
            if not self._is_target_closed_error(exc):
                raise
            self._assert_cdp_lifecycle_alive("behavior_primary_page_closed")
            raise RuntimeError(
                "xhs_main_page_closed_unexpected:stage=behavior"
            ) from exc

    async def _open_behavior_search_page_on_primary_page(self, keyword: str) -> None:
        """Navigate once; never adopt another tab after the primary page closes."""
        try:
            await self._open_behavior_search_page(keyword)
        except Exception as exc:
            if not self._is_target_closed_error(exc):
                raise
            self._assert_cdp_lifecycle_alive("search_navigation_primary_page_closed")
            raise RuntimeError(
                "xhs_main_page_closed_unexpected:stage=search_navigation"
            ) from exc

    def _assert_cdp_lifecycle_alive(self, stage: str) -> None:
        """Prefer a manager-observed context/browser lifecycle code when present."""
        manager = getattr(self, "cdp_manager", None)
        assert_alive = getattr(manager, "assert_alive", None)
        if callable(assert_alive):
            assert_alive(stage)

    async def _run_qrcode_login(self) -> None:
        """Run the one XHS login state machine in the current browser session."""
        login_obj = XiaoHongShuLogin(
            login_type=config.LOGIN_TYPE,
            browser_context=self.browser_context,
            context_page=self.context_page,
            close_page=self._close_page_with_deadline,
            new_page=self._new_guarded_page,
            manual_wait_budget=self._get_manual_wait_budget(),
        )
        try:
            await login_obj.begin()
        except PlatformRuntimeError as exc:
            stop_detail = str(exc.code or "xhs_login_runtime_failed")
            observed_failure_type = getattr(
                login_obj,
                "terminal_failure_type",
                lambda: "",
            )()
            failure_type = str(observed_failure_type or "")
            if not failure_type:
                failure_type = (
                    "manual_checkpoint_timeout"
                    if stop_detail == "xhs_manual_checkpoint_budget_exhausted"
                    else "login_runtime_error"
                )
            append_execution_event(
                "xhs_runtime_terminal",
                {
                    "phase": "login",
                    "failure_type": failure_type,
                    "stop_reason": "runtime_failed",
                    "stop_detail": stop_detail,
                    **login_obj.terminal_context(),
                    "retryable": False,
                },
            )
            raise
        except Exception as exc:
            stop_detail = str(exc).strip() or (
                f"xhs_login_{type(exc).__name__.lower()}"
            )
            browser_lifecycle_codes = {
                "xhs_login_browser_context_unavailable",
                "xhs_login_browser_pages_closed",
            }
            failure_type = (
                "browser_target_closed"
                if stop_detail in browser_lifecycle_codes
                or self._is_target_closed_error(exc)
                else "login_runtime_error"
            )
            append_execution_event(
                "xhs_runtime_terminal",
                {
                    "phase": "login",
                    "failure_type": failure_type,
                    "stop_reason": "runtime_failed",
                    "stop_detail": stop_detail,
                    **login_obj.terminal_context(),
                    "retryable": False,
                },
            )
            raise
        self.context_page = login_obj.context_page
        self.xhs_client.playwright_page = self.context_page

    async def _single_page_for_login(self) -> Page:
        """Select a login tab without closing any verification companion tab."""
        try:
            pages = [page for page in self.browser_context.pages if not page.is_closed()]
        except Exception as exc:
            raise RuntimeError("xhs_login_browser_context_unavailable") from exc

        if not pages:
            raise RuntimeError("xhs_login_browser_pages_closed")

        current_page = getattr(self, "context_page", None)
        page = (
            current_page
            if current_page in pages
            else pages[-1]
        )
        if len(pages) > 1:
            utils.logger.info(
                "[XiaoHongShuCrawler] Login stage is preserving all current "
                f"BrowserContext tabs: {len(pages)} open tab(s)."
            )
        self.context_page = page
        return page

    async def _profile_ui_visible(self) -> bool:
        """Recognize the signed-in profile entry across XHS sidebar DOM variants."""

        self._assert_primary_page_alive("profile_ui")
        try:
            selectors = (
                # The original sidebar layout exposes a profile URL.
                "xpath=//a[contains(@href, '/user/profile/')]"
                "[.//*[normalize-space()='我'] or normalize-space()='我']",
                # Current XHS can render the signed-in sidebar entry as a
                # button rather than a profile anchor before navigation.
                "xpath=//*[self::a or self::button]"
                "[.//*[normalize-space()='我'] or normalize-space()='我']",
                # Some current sidebar builds use a non-semantic container.
                # The logged-out dialog has “我已阅读”, not an exact “我”.
                "xpath=//*[normalize-space()='我']",
            )
            for selector in selectors:
                if await self.context_page.locator(selector).count() > 0:
                    return True
            return False
        except Exception:
            return False

    async def _wait_for_initial_page_settle(self) -> None:
        settle_seconds = self._env_float("TRIPPOSTCOLLECT_XHS_INITIAL_SETTLE_SECONDS", 12.0)
        try:
            await self.context_page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:
            pass
        try:
            await self.context_page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        if settle_seconds > 0:
            utils.logger.info(
                f"[XiaoHongShuCrawler] Waiting {settle_seconds:.1f}s for Xiaohongshu web startup settle ..."
            )
            await asyncio.sleep(settle_seconds)
        self._assert_primary_page_alive("initial_page_settle")

    @staticmethod
    def _request_failure_exception(exc: BaseException) -> BaseException:
        """Return the request exception hidden by tenacity, when available."""
        if not isinstance(exc, RetryError):
            return exc
        try:
            nested = exc.last_attempt.exception()
        except Exception:
            nested = None
        return nested if isinstance(nested, BaseException) else exc

    @staticmethod
    def _request_failure_attempts(exc: BaseException) -> int:
        """Return the observed tenacity attempt count without inventing retries."""

        if not isinstance(exc, RetryError):
            return 1
        return max(1, int(getattr(exc.last_attempt, "attempt_number", 1) or 1))

    @classmethod
    def _is_login_expired_failure(cls, exc: BaseException) -> bool:
        nested = cls._request_failure_exception(exc)
        text = str(nested).lower()
        return isinstance(nested, DataFetchError) and any(
            marker in text
            for marker in (
                "登录已过期",
                "登录状态已失效",
                "登录失效",
                "login expired",
                "login has expired",
                "session expired",
            )
        )

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
            utils.logger.warning(
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

        wait_seconds = self._env_float(
            "TRIPPOSTCOLLECT_XHS_NETWORK_WAIT_SECONDS",
            600.0,
        )
        if wait_seconds <= 0:
            return False
        minimum_delay = max(
            0.1,
            self._env_float(
                "TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MIN_SECONDS",
                2.0,
            ),
        )
        maximum_delay = max(
            minimum_delay,
            self._env_float(
                "TRIPPOSTCOLLECT_XHS_NETWORK_RETRY_MAX_SECONDS",
                30.0,
            ),
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
        utils.logger.warning(
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
        utils.logger.info(
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
        utils.logger.warning(
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
            utils.logger.warning(
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
                    utils.logger.info(
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
        utils.logger.info(
            "[XiaoHongShuCrawler] Mid-run login recovery confirmed; "
            "retrying the same search page."
        )
        return True

    async def start(self) -> None:
        self._validate_login_contract()
        playwright_proxy_format, httpx_proxy_format = None, None
        if config.ENABLE_IP_PROXY:
            self.ip_proxy_pool = await create_ip_pool(config.IP_PROXY_POOL_COUNT, enable_validate_ip=True)
            ip_proxy_info: IpInfoModel = await self.ip_proxy_pool.get_proxy()
            playwright_proxy_format, httpx_proxy_format = utils.format_proxy_info(ip_proxy_info)

        async with async_playwright() as playwright:
            try:
                await self._run_browser_session(
                    playwright,
                    playwright_proxy_format,
                    httpx_proxy_format,
                )
            except BaseException as primary_error:
                try:
                    await self._prepare_browser_shutdown()
                except BaseException as cleanup_error:
                    cleanup_detail = (
                        "XHS browser shutdown also failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
                    utils.logger.error(
                        "[XiaoHongShuCrawler.start] " + cleanup_detail
                    )
                    primary_error.add_note(cleanup_detail)
                raise
            else:
                await self._prepare_browser_shutdown()
            finally:
                # Stopping Playwright tears down the CDP transport before
                # app_runner invokes the later resource cleanup. Mark that
                # disconnect as planned without closing the browser here.
                manager = getattr(self, "cdp_manager", None)
                if manager is not None:
                    manager.mark_planned_cleanup(
                        "playwright_context_exit"
                    )

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

        if config.ENABLE_CDP_MODE:
            utils.logger.info("[XiaoHongShuCrawler] Launching browser using CDP mode")
            self.browser_context = await self.launch_browser_with_cdp(
                playwright,
                playwright_proxy_format,
                self.user_agent,
                headless=config.CDP_HEADLESS,
            )
            if self.cdp_manager:
                await self.cdp_manager.add_stealth_script()
        else:
            utils.logger.info("[XiaoHongShuCrawler] Launching browser using standard mode")
            chromium = playwright.chromium
            self.browser_context = await self.launch_browser(
                chromium,
                playwright_proxy_format,
                self.user_agent,
                headless=config.HEADLESS,
            )
            with resources.path("js/stealth.min.js") as stealth_path:
                await self.browser_context.add_init_script(path=str(stealth_path))

        self._install_new_page_guard()
        await install_project_runtime_hints(self.browser_context)
        self.context_page = await self._single_page_for_login()
        await self._goto_with_deadline(
            self.context_page,
            self.explore_url,
            stage="initial_explore",
        )
        await self._wait_for_initial_page_settle()
        initial_shell_timeout = self._env_float(
            "TRIPPOSTCOLLECT_XHS_INITIAL_SHELL_TIMEOUT_SECONDS",
            30.0,
        )
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
            (item.strip() for item in config.KEYWORDS.split(",") if item.strip()),
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
        crawler_type_var.set(config.CRAWLER_TYPE)
        if config.CRAWLER_TYPE == "search":
            await self.search()
        elif config.CRAWLER_TYPE == "detail":
            await self.get_specified_notes()
        elif config.CRAWLER_TYPE == "creator":
            await self.get_creators_and_notes()

        utils.logger.info("[XiaoHongShuCrawler.start] Xhs Crawler finished ...")

    async def search(self) -> None:
        """Search for notes and retrieve their comment information."""
        utils.logger.info("[XiaoHongShuCrawler.search] Begin search Xiaohongshu keywords")
        accumulator = AdaptiveAccumulator.from_environment("xhs")
        start_page = config.START_PAGE
        for keyword in config.KEYWORDS.split(","):
            source_keyword_var.set(keyword)
            utils.logger.info(f"[XiaoHongShuCrawler.search] Current search keyword: {keyword}")
            refresh_max_pages = env_int(
                "TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES",
                0,
            )
            source_exhausted = (
                os.environ.get("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED") == "1"
            )
            frontier_search_id = os.environ.get(
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR",
                "",
            ) or get_search_id()
            phases: list[tuple[str, int, int | None, str]] = []
            if refresh_max_pages > 0:
                phases.append(("refresh", 1, refresh_max_pages, get_search_id()))
            if not source_exhausted:
                phases.append(("frontier", start_page, None, frontier_search_id))

            for discovery_phase, phase_start, phase_limit, search_id in phases:
                page = phase_start
                phase_batches = 0
                while (
                    accumulator.can_continue
                    and (phase_limit is None or phase_batches < phase_limit)
                ):
                    requested_page = page
                    try:
                        utils.logger.info(
                            "[XiaoHongShuCrawler.search] search Xiaohongshu "
                            f"keyword: {keyword}, page: {requested_page}, phase: {discovery_phase}"
                        )
                        notes_res = await self._run_with_network_recovery(
                            lambda: self.xhs_client.get_note_by_keyword(
                                keyword=keyword,
                                search_id=search_id,
                                page=requested_page,
                                sort=(
                                    SearchSortType(config.SORT_TYPE)
                                    if config.SORT_TYPE != ""
                                    else SearchSortType.GENERAL
                                ),
                            ),
                            stage=(
                                f"search:{discovery_phase}:"
                                f"page={requested_page}:cursor={search_id}"
                            ),
                        )
                        if not notes_res:
                            utils.logger.info("[XiaoHongShuCrawler.search] No more content!")
                            if discovery_phase == "frontier":
                                accumulator.mark_source_exhausted(
                                    "empty_response",
                                    source_page=requested_page,
                                    source_cursor=search_id,
                                    source_has_more=False,
                                    raw_batch_count=0,
                                    resume_page=requested_page,
                                    resume_cursor=search_id,
                                    discovery_phase=discovery_phase,
                                )
                            break

                        raw_items = list(notes_res.get("items") or [])
                        source_has_more = (
                            notes_res.get("has_more")
                            if "has_more" in notes_res
                            else None
                        )
                        pending_ids: set[str] = set()
                        unknown_items: List[Dict] = []
                        for post_item in raw_items:
                            if post_item.get("model_type") in ("rec_query", "hot_query"):
                                continue
                            identity = str(post_item.get("id") or "")
                            if accumulator.is_known(identity) or (
                                identity and identity in pending_ids
                            ):
                                continue
                            if identity:
                                pending_ids.add(identity)
                            unknown_items.append(post_item)

                        accumulator.begin_batch()
                        selected_items = unknown_items
                        if not selected_items:
                            resume_page = requested_page + 1
                            if accumulator.finish_batch(
                                source_page=requested_page,
                                source_cursor=search_id,
                                source_has_more=source_has_more,
                                raw_batch_count=len(raw_items),
                                resume_page=resume_page,
                                resume_cursor=search_id,
                                batch_complete=True,
                                discovery_phase=discovery_phase,
                                count_stagnation=discovery_phase == "frontier",
                            ):
                                break
                            if source_has_more in (False, 0):
                                if discovery_phase == "frontier":
                                    accumulator.mark_source_exhausted(
                                        "has_more_false",
                                        source_page=requested_page,
                                        source_cursor=search_id,
                                        source_has_more=False,
                                        raw_batch_count=len(raw_items),
                                        resume_page=resume_page,
                                        resume_cursor=search_id,
                                        batch_complete=True,
                                        discovery_phase=discovery_phase,
                                    )
                                break
                            page = resume_page
                            phase_batches += 1
                            await self._guarded_pause("search_page", 12.0, 30.0)
                            continue

                        await self._guarded_pause("search_results", 6.0, 14.0)
                        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
                        task_list = [
                            self.get_note_detail_async_task(
                                note_id=post_item.get("id"),
                                xsec_source=post_item.get("xsec_source"),
                                xsec_token=post_item.get("xsec_token"),
                                semaphore=semaphore,
                            )
                            for post_item in selected_items
                        ]
                        note_details = await asyncio.gather(
                            *task_list,
                            return_exceptions=True,
                        )
                        note_ids: List[str] = []
                        xsec_tokens: List[str] = []
                        processed_count = 0
                        for post_item, note_detail in zip(selected_items, note_details):
                            identity = str(
                                (
                                    (note_detail or {}).get("note_id")
                                    if isinstance(note_detail, dict)
                                    else ""
                                )
                                or post_item.get("id")
                                or ""
                            )
                            processed_count += 1
                            if isinstance(note_detail, BaseException):
                                request_failure = self._request_failure_exception(
                                    note_detail
                                )
                                detail_text = str(request_failure).lower()
                                if (
                                    isinstance(
                                        request_failure,
                                        (
                                            IPBlockError,
                                            PlatformRuntimeError,
                                            XHSNetworkRecoveryTimeout,
                                            CDPBrowserLifecycleError,
                                        ),
                                    )
                                    or isinstance(note_detail, PlaywrightError)
                                    or self._is_login_expired_failure(note_detail)
                                    or any(
                                        marker in detail_text
                                        for marker in (
                                            "platform_security_limit",
                                            "captcha",
                                            "rate_limit",
                                            "login_required",
                                        )
                                    )
                                ):
                                    raise request_failure
                                error_code = (
                                    note_detail.code
                                    if isinstance(note_detail, XHSNoteDetailUnavailable)
                                    else "detail_request_failed"
                                )
                                should_stop = accumulator.skip_candidate_failure(
                                    identity,
                                    failure_scope="post",
                                    detail="note_detail_unavailable",
                                    error_code=error_code,
                                    attempts=(
                                        note_detail.attempts
                                        if isinstance(
                                            note_detail, XHSNoteDetailUnavailable
                                        )
                                        else self._request_failure_attempts(note_detail)
                                    ),
                                    retryable=True,
                                    source_page=requested_page,
                                    source_cursor=search_id,
                                    discovery_phase=discovery_phase,
                                )
                                if should_stop:
                                    break
                                continue
                            if note_detail:
                                if self.is_video_note(note_detail):
                                    utils.logger.info(
                                        "[XiaoHongShuCrawler.search] Skip video note, "
                                        f"note_id: {note_detail.get('note_id')}"
                                    )
                                    if accumulator.consider(identity, valid=False):
                                        break
                                    continue
                                await self._maybe_run_post_interaction(note_detail)
                                try:
                                    await self.enrich_note_creator(note_detail)
                                except XHSCreatorProfileUnavailable as exc:
                                    should_stop = accumulator.skip_candidate_failure(
                                        identity,
                                        failure_scope="post",
                                        detail="creator_profile_failed",
                                        error_code="creator_profile_unavailable",
                                        attempts=exc.attempts,
                                        retryable=True,
                                        source_page=requested_page,
                                        source_cursor=search_id,
                                        discovery_phase=discovery_phase,
                                    )
                                    if should_stop:
                                        break
                                    continue
                                except RuntimeError as exc:
                                    if isinstance(
                                        exc,
                                        (
                                            XHSNetworkRecoveryTimeout,
                                            CDPBrowserLifecycleError,
                                        ),
                                    ):
                                        raise
                                    detail_text = str(exc).lower()
                                    if any(
                                        marker in detail_text
                                        for marker in (
                                            "platform_security_limit",
                                            "captcha",
                                            "rate_limit",
                                            "login_required",
                                        )
                                    ):
                                        raise
                                    should_stop = accumulator.skip_candidate_failure(
                                        identity,
                                        failure_scope="post",
                                        detail="creator_profile_failed",
                                        error_code=(
                                            "creator_profile_unavailable"
                                            if "creator_profile_unavailable_after_retry"
                                            in detail_text
                                            else type(exc).__name__
                                        ),
                                        attempts=1,
                                        retryable=(
                                            "creator_profile_unavailable_after_retry"
                                            in detail_text
                                        ),
                                        source_page=requested_page,
                                        source_cursor=search_id,
                                        discovery_phase=discovery_phase,
                                    )
                                    if should_stop:
                                        break
                                    continue
                                creator_profile = note_detail.get("creator_profile") or {}
                                creator_item = (
                                    xhs_store._normalized_creator_item(
                                        (note_detail.get("user") or {}).get("user_id", ""),
                                        creator_profile,
                                    )
                                    if creator_profile
                                    else {}
                                )
                                followers_observed = any(
                                    creator_item.get(key) not in (None, "")
                                    for key in ("fans_count", "fans")
                                )
                                interact_info = note_detail.get("interact_info") or {}
                                valid = bool(
                                    identity
                                    and note_detail.get("desc")
                                    and note_detail.get("content_detail_status") == "detail_observed"
                                    and note_detail.get("content_detail_source") == "note_detail"
                                    and note_detail.get("time")
                                    and (note_detail.get("user") or {}).get("user_id")
                                    and (note_detail.get("user") or {}).get("nickname")
                                    and note_detail.get("image_list")
                                    and followers_observed
                                    and all(
                                        interact_info.get(key) not in (None, "")
                                        for key in (
                                            "liked_count",
                                            "collected_count",
                                            "comment_count",
                                            "share_count",
                                        )
                                    )
                                )
                                if valid:
                                    try:
                                        await self.get_notice_media(note_detail)
                                    except XHSImageDownloadError as exc:
                                        if is_runtime_blocking_image_error(exc.code):
                                            accumulator.mark_runtime_failed(
                                                exc.code,
                                                source_page=requested_page,
                                                source_cursor=search_id,
                                                resume_page=requested_page,
                                                resume_cursor=search_id,
                                                discovery_phase=discovery_phase,
                                            )
                                            break
                                        should_stop = accumulator.skip_candidate_failure(
                                            identity,
                                            failure_scope="image",
                                            detail="image_download_failed",
                                            error_code=exc.code,
                                            attempts=exc.attempts,
                                            source_index=exc.source_index,
                                            source_page=requested_page,
                                            source_cursor=search_id,
                                            discovery_phase=discovery_phase,
                                        )
                                        if should_stop:
                                            break
                                        continue
                                await xhs_store.update_xhs_note(note_detail)
                                note_ids.append(note_detail.get("note_id"))
                                xsec_tokens.append(note_detail.get("xsec_token"))
                                target_valid = valid and topic_relevant_for_web_post(
                                    "xhs",
                                    note_detail,
                                    fallback_keyword=keyword,
                                )
                                should_stop = accumulator.consider(
                                    identity,
                                    valid=target_valid,
                                )
                                if should_stop:
                                    break
                            elif accumulator.consider(identity, valid=False):
                                break

                        utils.logger.info(
                            "[XiaoHongShuCrawler.search] Note detail summaries: "
                            f"{self.note_detail_summaries([item for item in note_details if isinstance(item, dict)])}"
                        )
                        await self.batch_get_note_comments(note_ids, xsec_tokens)
                        batch_complete = processed_count >= len(unknown_items)
                        resume_page = (
                            requested_page + 1 if batch_complete else requested_page
                        )
                        if accumulator.finish_batch(
                            source_page=requested_page,
                            source_cursor=search_id,
                            source_has_more=source_has_more,
                            raw_batch_count=len(raw_items),
                            resume_page=resume_page,
                            resume_cursor=search_id,
                            batch_complete=batch_complete,
                            discovery_phase=discovery_phase,
                            count_stagnation=discovery_phase == "frontier",
                        ):
                            break
                        if source_has_more in (False, 0):
                            if discovery_phase == "frontier":
                                accumulator.mark_source_exhausted(
                                    "has_more_false",
                                    source_page=requested_page,
                                    source_cursor=search_id,
                                    source_has_more=False,
                                    raw_batch_count=len(raw_items),
                                    resume_page=resume_page,
                                    resume_cursor=search_id,
                                    batch_complete=batch_complete,
                                    discovery_phase=discovery_phase,
                                )
                            break
                        page = requested_page + 1
                        phase_batches += 1
                        await self._guarded_pause("search_page", 12.0, 30.0)
                    except XHSNetworkRecoveryTimeout as exc:
                        utils.logger.error(
                            "[XiaoHongShuCrawler.search] Network recovery budget "
                            f"expired on page {requested_page}: {exc}"
                        )
                        accumulator.mark_runtime_failed(
                            "network_recovery_timeout",
                            source_page=requested_page,
                            source_cursor=search_id,
                            resume_page=requested_page,
                            resume_cursor=search_id,
                            discovery_phase=discovery_phase,
                        )
                        break
                    except XHSNoteDetailUnavailable as exc:
                        utils.logger.error(
                            "[XiaoHongShuCrawler.search] Note detail remained unavailable "
                            f"on page {requested_page}: {exc!r}"
                        )
                        accumulator.mark_runtime_failed(
                            "note_detail_unavailable",
                            source_page=requested_page,
                            source_cursor=search_id,
                            resume_page=requested_page,
                            resume_cursor=search_id,
                            discovery_phase=discovery_phase,
                        )
                        break
                    except IPBlockError as exc:
                        utils.logger.error(
                            "[XiaoHongShuCrawler.search] Platform IP block: "
                            f"{exc!r}"
                        )
                        accumulator.mark_runtime_failed(
                            "ip_blocked_300012",
                            source_page=requested_page,
                            source_cursor=search_id,
                            resume_page=requested_page,
                            resume_cursor=search_id,
                            discovery_phase=discovery_phase,
                        )
                        break
                    except PlatformRuntimeError as exc:
                        accumulator.mark_runtime_failed(
                            exc.code,
                            source_page=requested_page,
                            source_cursor=search_id,
                            resume_page=requested_page,
                            resume_cursor=search_id,
                            discovery_phase=discovery_phase,
                        )
                        break
                    except (DataFetchError, RetryError) as exc:
                        request_failure = self._request_failure_exception(exc)
                        if isinstance(request_failure, IPBlockError):
                            accumulator.mark_runtime_failed(
                                "ip_blocked_300012",
                                source_page=requested_page,
                                source_cursor=search_id,
                                resume_page=requested_page,
                                resume_cursor=search_id,
                                discovery_phase=discovery_phase,
                            )
                            break
                        if isinstance(request_failure, PlatformRuntimeError):
                            accumulator.mark_runtime_failed(
                                request_failure.code,
                                source_page=requested_page,
                                source_cursor=search_id,
                                resume_page=requested_page,
                                resume_cursor=search_id,
                                discovery_phase=discovery_phase,
                            )
                            break
                        if self._is_login_expired_failure(exc):
                            try:
                                recovered = (
                                    await self._wait_for_midrun_login_recovery(
                                        keyword
                                    )
                                )
                            except XHSManualWaitBudgetExhausted as budget_exc:
                                accumulator.mark_runtime_failed(
                                    budget_exc.code,
                                    source_page=requested_page,
                                    source_cursor=search_id,
                                    resume_page=requested_page,
                                    resume_cursor=search_id,
                                    discovery_phase=discovery_phase,
                                )
                                break
                            if recovered:
                                continue
                            utils.logger.error(
                                "[XiaoHongShuCrawler.search] Login remained expired "
                                f"on page {requested_page}."
                            )
                            accumulator.mark_runtime_failed(
                                "login_required",
                                source_page=requested_page,
                                source_cursor=search_id,
                                resume_page=requested_page,
                                resume_cursor=search_id,
                                discovery_phase=discovery_phase,
                            )
                            break
                        utils.logger.error(
                            "[XiaoHongShuCrawler.search] Search or note detail "
                            f"request failed: {self._request_failure_exception(exc)!r}"
                        )
                        accumulator.mark_runtime_failed(
                            "search_or_detail_request_failed",
                            source_page=requested_page,
                            source_cursor=search_id,
                            resume_page=requested_page,
                            resume_cursor=search_id,
                            discovery_phase=discovery_phase,
                        )
                        break
                    except CDPBrowserLifecycleError as exc:
                        detail = xhs_cdp_lifecycle_stop_detail(exc)
                        utils.logger.error(
                            "[XiaoHongShuCrawler.search] CDP lifecycle ended on "
                            f"page {requested_page}: {exc!r}"
                        )
                        accumulator.mark_runtime_failed(
                            detail,
                            source_page=requested_page,
                            source_cursor=search_id,
                            resume_page=requested_page,
                            resume_cursor=search_id,
                            discovery_phase=discovery_phase,
                        )
                        break
                    except PlaywrightError as exc:
                        detail = (
                            "browser_context_closed"
                            if (
                                exc.__class__.__name__ == "TargetClosedError"
                                or "context or browser has been closed" in str(exc).lower()
                            )
                            else "browser_runtime_failed"
                        )
                        utils.logger.error(
                            "[XiaoHongShuCrawler.search] Browser runtime error on "
                            f"page {requested_page}: {exc!r}"
                        )
                        accumulator.mark_runtime_failed(
                            detail,
                            source_page=requested_page,
                            source_cursor=search_id,
                            resume_page=requested_page,
                            resume_cursor=search_id,
                            discovery_phase=discovery_phase,
                        )
                        break

            if source_exhausted and not accumulator.stop_reason:
                accumulator.mark_source_exhausted(
                    "saved_source_exhausted",
                    source_page=start_page,
                    source_cursor=frontier_search_id,
                    source_has_more=False,
                    raw_batch_count=0,
                    resume_page=start_page,
                    resume_cursor=frontier_search_id,
                    discovery_phase="frontier",
                )

    async def get_creators_and_notes(self) -> None:
        """Get creator's notes and retrieve their comment information."""
        utils.logger.info("[XiaoHongShuCrawler.get_creators_and_notes] Begin get Xiaohongshu creators")
        for creator_url in config.XHS_CREATOR_ID_LIST:
            try:
                # Parse creator URL to get user_id and security tokens
                creator_info: CreatorUrlInfo = parse_creator_info_from_url(creator_url)
                utils.logger.info(f"[XiaoHongShuCrawler.get_creators_and_notes] Parse creator URL info: {creator_info}")
                user_id = creator_info.user_id

                # get creator detail info from web html content
                createor_info: Dict = await self._run_with_network_recovery(
                    lambda: self.xhs_client.get_creator_info(
                        user_id=user_id,
                        xsec_token=creator_info.xsec_token,
                        xsec_source=creator_info.xsec_source,
                    ),
                    stage=f"creator_mode_profile:user={user_id}",
                )
                if createor_info:
                    await xhs_store.save_creator(user_id, creator=createor_info)
            except ValueError as e:
                utils.logger.error(f"[XiaoHongShuCrawler.get_creators_and_notes] Failed to parse creator URL: {e}")
                continue

            # Use fixed crawling interval
            crawl_interval = config.CRAWLER_MAX_SLEEP_SEC
            # Get all note information of the creator
            all_notes_list = await self._run_with_network_recovery(
                lambda: self.xhs_client.get_all_notes_by_creator(
                    user_id=user_id,
                    crawl_interval=crawl_interval,
                    callback=self.fetch_creator_notes_detail,
                    xsec_token=creator_info.xsec_token,
                    xsec_source=creator_info.xsec_source,
                ),
                stage=f"creator_mode_notes:user={user_id}",
            )

            note_ids = []
            xsec_tokens = []
            for note_item in all_notes_list:
                note_ids.append(note_item.get("note_id"))
                xsec_tokens.append(note_item.get("xsec_token"))
            await self.batch_get_note_comments(note_ids, xsec_tokens)

    async def fetch_creator_notes_detail(self, note_list: List[Dict]):
        """Concurrently obtain the specified post list and save the data"""
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [
            self.get_note_detail_async_task(
                note_id=post_item.get("note_id"),
                xsec_source=post_item.get("xsec_source"),
                xsec_token=post_item.get("xsec_token"),
                semaphore=semaphore,
            ) for post_item in note_list
        ]

        note_details = await asyncio.gather(*task_list)
        for note_detail in note_details:
            if note_detail:
                if self.is_video_note(note_detail):
                    utils.logger.info(
                        f"[XiaoHongShuCrawler.fetch_creator_notes_detail] Skip video note, note_id: {note_detail.get('note_id')}"
                    )
                    continue
                await self.enrich_note_creator(note_detail)
                await xhs_store.update_xhs_note(note_detail)
                await self.get_notice_media(note_detail)

    async def get_specified_notes(self):
        """Get the information and comments of the specified post

        Note: Must specify note_id, xsec_source, xsec_token
        """
        get_note_detail_task_list = []
        detail_semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        for full_note_url in config.XHS_SPECIFIED_NOTE_URL_LIST:
            note_url_info: NoteUrlInfo = parse_note_info_from_note_url(full_note_url)
            utils.logger.info(f"[XiaoHongShuCrawler.get_specified_notes] Parse note url info: {note_url_info}")
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
                    utils.logger.info(
                        f"[XiaoHongShuCrawler.get_specified_notes] Skip video note, note_id: {note_detail.get('note_id')}"
                    )
                    continue
                need_get_comment_note_ids.append(note_detail.get("note_id", ""))
                xsec_tokens.append(note_detail.get("xsec_token", ""))
                await self.enrich_note_creator(note_detail)
                await xhs_store.update_xhs_note(note_detail)
                await self.get_notice_media(note_detail)
        await self.batch_get_note_comments(need_get_comment_note_ids, xsec_tokens)

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
            utils.logger.warning(
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
            utils.logger.warning(
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
            await record_platform_security_limit(
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
            self._env_float("TRIPPOSTCOLLECT_XHS_CREATOR_VERIFY_POLL_SECONDS", 2.0),
        )
        budget = self._get_manual_wait_budget()
        ticket = budget.start(f"creator_profile_verification:{user_id}")
        try:
            await page.bring_to_front()
            utils.logger.warning(
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
                        utils.logger.info(
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

    @staticmethod
    def is_video_note(note_detail: Dict) -> bool:
        note_type = str(note_detail.get("type") or "").strip().lower()
        return note_type in {"video", "视频"} or "video" in note_type

    @staticmethod
    def note_detail_summaries(note_details: List[Optional[Dict]]) -> List[Dict]:
        summaries: List[Dict] = []
        for note_detail in note_details:
            if not note_detail:
                continue
            user_info = note_detail.get("user") or {}
            interact_info = note_detail.get("interact_info") or {}
            creator_profile = note_detail.get("creator_profile") or {}
            summaries.append(
                {
                    "note_id": note_detail.get("note_id"),
                    "type": note_detail.get("type"),
                    "title": note_detail.get("title"),
                    "desc_preview": str(note_detail.get("desc") or "")[:80],
                    "image_count": len(note_detail.get("image_list") or []),
                    "user_id": user_info.get("user_id"),
                    "nickname": user_info.get("nickname"),
                    "liked_count": interact_info.get("liked_count"),
                    "collected_count": interact_info.get("collected_count"),
                    "comment_count": interact_info.get("comment_count"),
                    "share_count": interact_info.get("share_count"),
                    "creator_profile": bool(creator_profile),
                }
            )
        return summaries

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
        utils.logger.info(f"[get_note_detail_async_task] Begin get note detail, note_id: {note_id}")
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
                        utils.logger.warning(
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
                utils.logger.warning(f"[XiaoHongShuCrawler.get_note_detail_async_task] Note not found: {note_id}, {ex}")
                return None
            except DataFetchError as ex:
                utils.logger.error(f"[XiaoHongShuCrawler.get_note_detail_async_task] Get note detail error: {ex}")
                raise
            except KeyError as ex:
                utils.logger.error(f"[XiaoHongShuCrawler.get_note_detail_async_task] have not fund note detail note_id:{note_id}, err: {ex}")
                raise XHSNoteDetailUnavailable(
                    note_id,
                    "detail_parse_failed",
                    attempts=max(1, attempts),
                ) from ex

    async def batch_get_note_comments(self, note_list: List[str], xsec_tokens: List[str]):
        """Batch get note comments"""
        if not config.ENABLE_GET_COMMENTS:
            utils.logger.info("[XiaoHongShuCrawler.batch_get_note_comments] Crawling comment mode is not enabled")
            return

        utils.logger.info(f"[XiaoHongShuCrawler.batch_get_note_comments] Begin batch get note comments, note list: {note_list}")
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list: List[Task] = []
        for index, note_id in enumerate(note_list):
            task = asyncio.create_task(
                self.get_comments(note_id=note_id, xsec_token=xsec_tokens[index], semaphore=semaphore),
                name=note_id,
            )
            task_list.append(task)
        await asyncio.gather(*task_list)

    async def get_comments(self, note_id: str, xsec_token: str, semaphore: asyncio.Semaphore):
        """Get note comments with keyword filtering and quantity limitation"""
        async with semaphore:
            utils.logger.info(f"[XiaoHongShuCrawler.get_comments] Begin get note id comments {note_id}")
            # Use fixed crawling interval
            crawl_interval = config.CRAWLER_MAX_SLEEP_SEC
            await self._run_with_network_recovery(
                lambda: self.xhs_client.get_note_all_comments(
                    note_id=note_id,
                    xsec_token=xsec_token,
                    crawl_interval=crawl_interval,
                    callback=xhs_store.batch_update_xhs_note_comments,
                    max_count=config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES,
                ),
                stage=f"note_comments:note={note_id}",
            )

            # Sleep after fetching comments
            await asyncio.sleep(crawl_interval)
            utils.logger.info(f"[XiaoHongShuCrawler.get_comments] Sleeping for {crawl_interval} seconds after fetching comments for note {note_id}")

    async def create_xhs_client(self, httpx_proxy: Optional[str]) -> XiaoHongShuClient:
        """Create Xiaohongshu client"""
        utils.logger.info("[XiaoHongShuCrawler.create_xhs_client] Begin create Xiaohongshu API client ...")
        identity_headers = await self._browser_identity_headers()
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            self.browser_context,
            urls=self.cookie_urls,
        )
        xhs_client_obj = XiaoHongShuClient(
            proxy=httpx_proxy,
            headers={
                "accept": "application/json, text/plain, */*",
                "cache-control": "no-cache",
                "content-type": "application/json;charset=UTF-8",
                "origin": self.index_url,
                "pragma": "no-cache",
                "priority": "u=1, i",
                "referer": f"{self.index_url}/",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
                **identity_headers,
                "Cookie": cookie_str,
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
            proxy_ip_pool=self.ip_proxy_pool,  # Pass proxy pool for automatic refresh
            manual_wait_budget=self._get_manual_wait_budget(),
        )
        return xhs_client_obj

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        utils.logger.info("[XiaoHongShuCrawler.launch_browser] Begin create browser context ...")
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
        manager = CDPBrowserManager()
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
            utils.logger.info(f"[XiaoHongShuCrawler] CDP browser info: {browser_info}")

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
            utils.logger.warning(
                "[XiaoHongShuCrawler.close] Browser cleanup was cancelled; "
                "retaining lifecycle handles for audit and retry"
            )
            raise
        except TimeoutError:
            utils.logger.error(
                "[XiaoHongShuCrawler.close] Browser cleanup timed out; "
                "retaining lifecycle handles for audit and retry"
            )
            raise
        except Exception as exc:
            utils.logger.error(
                "[XiaoHongShuCrawler.close] Browser cleanup failed; "
                f"retaining lifecycle handles for audit and retry: {exc}"
            )
            raise
        else:
            utils.logger.info("[XiaoHongShuCrawler.close] Browser context closed ...")

    async def get_notice_media(self, note_detail: Dict):
        if not config.ENABLE_GET_MEIDAS:
            utils.logger.info("[XiaoHongShuCrawler.get_notice_media] Crawling image mode is not enabled")
            return
        await self.get_note_images(note_detail)
        utils.logger.info("[XiaoHongShuCrawler.get_notice_media] Video media crawling is disabled by TripPostCollect policy")

    async def get_note_images(self, note_item: Dict):
        """Get note images. Please use get_notice_media

        Args:
            note_item: Note item dictionary
        """
        if not config.ENABLE_GET_MEIDAS:
            return
        note_id = str(note_item.get("note_id") or "")
        image_assets = xhs_store._xhs_image_assets(note_item)
        if not image_assets:
            return
        fetched_assets: List[Dict] = []
        for asset in image_assets:
            source_index = int(asset["source_index"])
            try:
                content, attempts = await fetch_image_bytes_with_retry(
                    lambda: self.xhs_client.get_note_media(asset["url"]),
                    logger=utils.logger,
                    label=(
                        f"platform=xhs note_id={note_id} "
                        f"source_index={source_index}"
                    ),
                )
            except ImageDownloadFetchError as exc:
                await xhs_store.record_xhs_note_image_failure(
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
                await xhs_store.record_xhs_note_image_failure(
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
            await xhs_store.update_xhs_note_images(note_id, fetched_assets)
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
            await xhs_store.record_xhs_note_image_failure(
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

    async def get_notice_video(self, note_item: Dict):
        """Get note videos. Please use get_notice_media

        Args:
            note_item: Note item dictionary
        """
        utils.logger.info("[XiaoHongShuCrawler.get_notice_video] Video media crawling is disabled by TripPostCollect policy")
        return
