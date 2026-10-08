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

"""小红书 crawler 组合类：搜索编排与会话调用；各职责方法由同一类的 mixin 提供。"""

import asyncio
import logging
import os
from asyncio import Task
from typing import Dict, List, Optional

from playwright.async_api import BrowserContext, Error as PlaywrightError, Page
from tenacity import RetryError

from trippostcollect.application.contracts import XhsPorts, XhsReaders, XhsSettings
from trippostcollect.platforms.xhs.author import XhsAuthorMixin
from trippostcollect.platforms.xhs.behavior import XhsBehaviorMixin
from trippostcollect.platforms.xhs.client import XiaoHongShuClient
from trippostcollect.platforms.xhs.detail import XhsDetailMixin
from trippostcollect.platforms.xhs.errors import (
    DataFetchError,
    IPBlockError,
    PlatformRuntimeError,
    XHSCreatorProfileUnavailable,
    XHSImageDownloadError,
    XHSNetworkRecoveryTimeout,
    XHSNoteDetailUnavailable,
    XhsErrorsMixin,
    xhs_cdp_lifecycle_stop_detail,
)
from trippostcollect.platforms.xhs.login import XhsLoginMixin
from trippostcollect.platforms.xhs.manual_wait import XHSManualWaitBudget, XHSManualWaitBudgetExhausted
from trippostcollect.platforms.xhs.media import XhsMediaMixin
from trippostcollect.platforms.xhs.models import SearchSortType
from trippostcollect.platforms.xhs.navigation import XhsNavigationMixin
from trippostcollect.platforms.xhs.parser import XhsParserMixin, _normalized_creator_item, get_search_id, update_xhs_note
from trippostcollect.platforms.xhs.session import XhsSessionMixin
from trippostcollect.records.topic_relevance import topic_relevant_for_web_post
from trippostcollect.runtime.browser import CDPBrowserLifecycleError
from trippostcollect.runtime.cookies import convert_browser_context_cookies
from trippostcollect.runtime.image_retry import is_runtime_blocking_image_error

logger = logging.getLogger("MediaCrawler")


class XiaoHongShuCrawler(
    XhsSessionMixin,
    XhsNavigationMixin,
    XhsLoginMixin,
    XhsAuthorMixin,
    XhsDetailMixin,
    XhsMediaMixin,
    XhsBehaviorMixin,
    XhsParserMixin,
    XhsErrorsMixin,
):
    context_page: Page
    xhs_client: XiaoHongShuClient
    browser_context: BrowserContext
    cdp_manager: Optional[object]

    def __init__(self, *, settings: XhsSettings, inputs: XhsReaders, ports: XhsPorts) -> None:
        self.settings = settings
        self.inputs = inputs
        self.ports = ports
        self.source_keyword = ""
        self.crawler_type = ""
        self.index_url = "https://www.rednote.com" if self.settings.XHS_INTERNATIONAL else "https://www.xiaohongshu.com"
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

    async def start(self) -> None:
        self.inputs.validate_login_contract()
        playwright_proxy_format, httpx_proxy_format = None, None

        async with self.ports.async_playwright() as playwright:
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
                    logger.error(
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

    async def search(self) -> None:
        """Search for notes and retrieve their comment information."""
        logger.info("[XiaoHongShuCrawler.search] Begin search Xiaohongshu keywords")
        accumulator = self.ports.accumulator_factory()
        start_page = self.settings.START_PAGE
        for keyword in self.settings.KEYWORDS.split(","):
            self.source_keyword = keyword
            logger.info(f"[XiaoHongShuCrawler.search] Current search keyword: {keyword}")
            refresh_max_pages = self.inputs.refresh_max_pages()
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
                        logger.info(
                            "[XiaoHongShuCrawler.search] search Xiaohongshu "
                            f"keyword: {keyword}, page: {requested_page}, phase: {discovery_phase}"
                        )
                        notes_res = await self._run_with_network_recovery(
                            lambda: self.xhs_client.get_note_by_keyword(
                                keyword=keyword,
                                search_id=search_id,
                                page=requested_page,
                                sort=(
                                    SearchSortType(self.settings.SORT_TYPE)
                                    if self.settings.SORT_TYPE != ""
                                    else SearchSortType.GENERAL
                                ),
                            ),
                            stage=(
                                f"search:{discovery_phase}:"
                                f"page={requested_page}:cursor={search_id}"
                            ),
                        )
                        if not notes_res:
                            logger.info("[XiaoHongShuCrawler.search] No more content!")
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
                        semaphore = asyncio.Semaphore(self.settings.MAX_CONCURRENCY_NUM)
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
                                    logger.info(
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
                                    _normalized_creator_item(
                                        (note_detail.get("user") or {}).get("user_id", ""),
                                        creator_profile,
                                        current_timestamp=self.ports.current_timestamp,
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
                                await self.update_xhs_note(note_detail)
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

                        logger.info(
                            "[XiaoHongShuCrawler.search] Note detail summaries: "
                            f"{self.note_detail_summaries([item for item in note_details if isinstance(item, dict)])}"
                        )
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
                        logger.error(
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
                        logger.error(
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
                        logger.error(
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
                            logger.error(
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
                        logger.error(
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
                        logger.error(
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
                        logger.error(
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

    async def create_xhs_client(self, httpx_proxy: Optional[str]) -> XiaoHongShuClient:
        """Create Xiaohongshu client"""
        logger.info("[XiaoHongShuCrawler.create_xhs_client] Begin create Xiaohongshu API client ...")
        identity_headers = await self._browser_identity_headers()
        cookie_str, cookie_dict = await convert_browser_context_cookies(
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
            manual_wait_budget=self._get_manual_wait_budget(),
            ports=self.ports.client,
        )
        return xhs_client_obj

    @classmethod
    def bind(cls, dependencies):
        """入口绑定装配工厂；每次构造时取得已解析的配置快照与端口。"""
        class XiaoHongShuCrawler(cls):
            def __init__(self):
                super().__init__(**dependencies())

        return XiaoHongShuCrawler

    async def update_xhs_note(self, note_item: Dict) -> None:
        """使用本实例的关键词、抓取类型、时钟与内容出口。"""
        await store_xhs_note(
            note_item,
            source_keyword=self.source_keyword,
            crawler_type=self.crawler_type,
            save_data_option=self.settings.SAVE_DATA_OPTION,
            current_timestamp=self.ports.current_timestamp,
            content_sink_factory=self.ports.content_sink_factory,
        )

    async def update_xhs_note_images(self, note_id: str, image_content_items: List[Dict]):
        """使用本实例的图片暂存端口。"""
        return await update_xhs_note_images(
            note_id, image_content_items, image_stager_factory=self.ports.image_stager_factory,
        )

    async def record_xhs_note_image_failure(self, note_id: str, image_content_item: Dict):
        """使用本实例的失败证据端口。"""
        return await record_xhs_note_image_failure(
            note_id, image_content_item, image_stager_factory=self.ports.image_stager_factory,
        )


async def store_xhs_note(
    note_item: Dict, *, source_keyword: str, crawler_type: str, save_data_option: str,
    current_timestamp, content_sink_factory,
) -> None:
    """原 store 的 IO 尾部：纯投影后记录日志，再按原时点创建内容出口写出。"""
    local_db_item = update_xhs_note(
        note_item,
        source_keyword=source_keyword,
        current_timestamp=current_timestamp,
        save_data_option=save_data_option,
    )
    logger.info(f"[store.xhs.update_xhs_note] xhs note: {local_db_item}")
    await content_sink_factory(crawler_type).store_content(local_db_item)


async def update_xhs_note_images(note_id: str, image_content_items: List[Dict], *, image_stager_factory):
    """Atomically save all body images for one XHS note."""

    return await image_stager_factory().store_post_images(note_id, image_content_items)


async def record_xhs_note_image_failure(note_id: str, image_content_item: Dict, *, image_stager_factory):
    """Write one failed manifest row without success file metadata."""

    return await image_stager_factory().record_failure(note_id, image_content_item)
