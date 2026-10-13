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

from trippostcollect.application.contracts import XhsPorts, XhsReaders, XhsSettings
from trippostcollect.platforms.xhs.author import XhsAuthorMixin
from trippostcollect.platforms.xhs.behavior import XhsBehaviorMixin
from trippostcollect.platforms.xhs.client import XiaoHongShuClient
from trippostcollect.platforms.xhs.detail import XhsDetailMixin
from trippostcollect.platforms.xhs.errors import (
    IPBlockError,
    PlatformRuntimeError,
    XHSCreatorProfileUnavailable,
    XHSImageDownloadError,
    XHSLoginExpired,
    XHSNetworkRecoveryTimeout,
    XHSNoteDetailUnavailable,
    XhsErrorsMixin,
    is_xhs_target_closed_failure,
    xhs_cdp_lifecycle_stop_detail,
)
from trippostcollect.platforms.xhs.login import XhsLoginMixin
from trippostcollect.platforms.xhs.manual_wait import XHSManualWaitBudget
from trippostcollect.platforms.xhs.media import XhsMediaMixin
from trippostcollect.platforms.xhs.navigation import XhsNavigationMixin, XhsSearchEntryFailed
from trippostcollect.platforms.xhs.parser import XhsParserMixin, _normalized_creator_item, update_xhs_note
from trippostcollect.platforms.xhs.scan import ScanState, SearchItem, SearchPage, XhsScanMixin
from trippostcollect.platforms.xhs.session import XhsSessionMixin
from trippostcollect.records.topic_relevance import topic_relevant_for_web_post
from trippostcollect.runtime.browser import CDPBrowserLifecycleError
from trippostcollect.runtime.cookies import convert_browser_context_cookies
from trippostcollect.runtime.image_retry import is_runtime_blocking_image_error
from trippostcollect.runtime.page_capture import PageCaptureChannel

logger = logging.getLogger("MediaCrawler")


def _mark_cdp_lifecycle_failed(
    accumulator, exc: CDPBrowserLifecycleError, *, source_page, source_cursor
) -> None:
    """浏览器生命周期失败统一记为 runtime_failed；登录恢复经 _login_recovery_stop_detail 取同一细节。"""
    detail = xhs_cdp_lifecycle_stop_detail(exc)
    logger.error(
        "[XiaoHongShuCrawler.search] CDP lifecycle ended on "
        f"page {source_page}: {exc!r}"
    )
    accumulator.mark_runtime_failed(
        detail,
        source_page=source_page,
        source_cursor=source_cursor,
        resume_page=None,
        resume_cursor=None,
        discovery_phase="full_scan",
    )


def _playwright_stop_detail(exc: PlaywrightError) -> str:
    """Playwright 运行期失败的停止细节；页内请求与登录恢复共用。"""
    if is_xhs_target_closed_failure(exc):
        return "browser_context_closed"
    return "browser_runtime_failed"


def _login_recovery_stop_detail(exc: BaseException) -> Optional[str]:
    """页内登录恢复中的运行级失败按同级页内分支同一口径给出停止细节；其他异常返回 None。"""
    if isinstance(exc, CDPBrowserLifecycleError):
        return xhs_cdp_lifecycle_stop_detail(exc)
    if isinstance(exc, XHSNetworkRecoveryTimeout):
        return "network_recovery_timeout"
    if isinstance(exc, IPBlockError):
        return "ip_blocked_300012"
    if isinstance(exc, PlatformRuntimeError):
        return exc.code
    if isinstance(exc, PlaywrightError):
        return _playwright_stop_detail(exc)
    return None


def _full_scan_stop_detail(exc: BaseException) -> Optional[str]:
    """全量扫描的运行级失败细节：与登录恢复同一口径，另加搜索入口与过渡详情路径；其他异常返回 None。"""
    detail = _login_recovery_stop_detail(exc)
    if detail is not None:
        return detail
    if isinstance(exc, XhsSearchEntryFailed):
        return str(exc)
    if isinstance(exc, XHSLoginExpired):
        return exc.code
    if isinstance(exc, XHSNoteDetailUnavailable):
        return "note_detail_unavailable"
    return None


class XiaoHongShuCrawler(
    XhsScanMixin,
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
        self.capture: Optional[PageCaptureChannel] = None
        self._trigger_actions: List[tuple[int, str]] = []
        self._scan: Optional[ScanState] = None
        self._behavior_stage_done = False
        # 行为证据创建前的停顿与验证事件暂存（见 _guarded_pause）；行为阶段后并入并置空。
        self._pending_behavior_evidence: Optional[Dict] = None

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
                self._close_capture_channel()
                # Stopping Playwright tears down the CDP transport before
                # app_runner invokes the later resource cleanup. Mark that
                # disconnect as planned without closing the browser here.
                manager = getattr(self, "cdp_manager", None)
                if manager is not None:
                    manager.mark_planned_cleanup(
                        "playwright_context_exit"
                    )

    async def search(self) -> None:
        """按关键词逐个全量滚动扫描；运行级失败在此集中写停止事件，来源耗尽由扫描判定。"""
        logger.info("[XiaoHongShuCrawler.search] Begin search Xiaohongshu keywords")
        accumulator = self.ports.accumulator_factory()
        for keyword in (item.strip() for item in self.settings.KEYWORDS.split(",")):
            if not keyword:
                continue
            if not accumulator.can_continue:
                break
            self.source_keyword = keyword
            logger.info(f"[XiaoHongShuCrawler.search] Current search keyword: {keyword}")
            try:
                await self._run_full_scan(keyword, accumulator)
            except CDPBrowserLifecycleError as exc:
                _mark_cdp_lifecycle_failed(accumulator, exc, **self._scan_position())
            except Exception as exc:
                detail = _full_scan_stop_detail(exc)
                if detail is None:
                    raise
                logger.error(
                    "[XiaoHongShuCrawler.search] Full scan stopped: "
                    f"detail={detail}, error={exc!r}"
                )
                accumulator.mark_runtime_failed(
                    detail,
                    **self._scan_position(),
                    resume_page=None,
                    resume_cursor=None,
                    discovery_phase="full_scan",
                )

    async def _process_note_card(self, item: SearchItem, search_page: SearchPage) -> None:
        """过渡实现（提交 5 由浮层处理取代）：经旧 API 取详情、作者与图片，并对本篇作出决定。"""
        accumulator = self._scan.accumulator
        identity = item.note_id
        position = {
            "source_page": search_page.page,
            "source_cursor": search_page.session.search_id,
            "discovery_phase": "full_scan",
        }
        try:
            note_detail = await self.get_note_detail_async_task(
                note_id=identity,
                xsec_source=item.xsec_source,
                xsec_token=item.xsec_token,
                semaphore=asyncio.Semaphore(1),
            )
        except Exception as exc:
            request_failure = self._request_failure_exception(exc)
            if self._is_login_expired_failure(exc):
                raise XHSLoginExpired("note_detail") from exc
            detail_text = str(request_failure).lower()
            if isinstance(
                request_failure,
                (IPBlockError, PlatformRuntimeError, XHSNetworkRecoveryTimeout, CDPBrowserLifecycleError),
            ) or isinstance(exc, PlaywrightError):
                raise request_failure
            if any(
                marker in detail_text
                for marker in ("platform_security_limit", "captcha", "rate_limit", "login_required")
            ):
                raise PlatformRuntimeError(str(request_failure), code="search_or_detail_request_failed") from exc
            accumulator.skip_candidate_failure(
                identity,
                failure_scope="post",
                detail="note_detail_unavailable",
                error_code=exc.code if isinstance(exc, XHSNoteDetailUnavailable) else "detail_request_failed",
                attempts=(
                    exc.attempts
                    if isinstance(exc, XHSNoteDetailUnavailable)
                    else self._request_failure_attempts(exc)
                ),
                retryable=True,
                **position,
            )
            return
        if not note_detail:
            accumulator.consider(identity, valid=False)
            return
        if self.is_video_note(note_detail):
            logger.info(f"[XiaoHongShuCrawler.search] Skip video note, note_id: {identity}")
            accumulator.consider(identity, valid=False)
            return
        await self._maybe_run_post_interaction(note_detail)
        try:
            await self.enrich_note_creator(note_detail)
        except XHSCreatorProfileUnavailable as exc:
            accumulator.skip_candidate_failure(
                identity,
                failure_scope="post",
                detail=f"creator_profile_failed:{exc.reason}" if exc.reason else "creator_profile_failed",
                error_code="creator_profile_unavailable",
                attempts=exc.attempts,
                retryable=True,
                **position,
            )
            return
        except RuntimeError as exc:
            if isinstance(exc, (XHSNetworkRecoveryTimeout, CDPBrowserLifecycleError)):
                raise
            detail_text = str(exc).lower()
            if any(
                marker in detail_text
                for marker in ("platform_security_limit", "captcha", "rate_limit", "login_required")
            ):
                raise
            retry_exhausted = "creator_profile_unavailable_after_retry" in detail_text
            accumulator.skip_candidate_failure(
                identity,
                failure_scope="post",
                detail="creator_profile_failed",
                error_code="creator_profile_unavailable" if retry_exhausted else type(exc).__name__,
                attempts=1,
                retryable=retry_exhausted,
                **position,
            )
            return
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
        followers_observed = any(creator_item.get(key) not in (None, "") for key in ("fans_count", "fans"))
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
                for key in ("liked_count", "collected_count", "comment_count", "share_count")
            )
        )
        if valid:
            try:
                await self.get_notice_media(note_detail)
            except XHSImageDownloadError as exc:
                if is_runtime_blocking_image_error(exc.code):
                    raise PlatformRuntimeError(f"XHS image download {exc.code}", code=exc.code) from exc
                accumulator.skip_candidate_failure(
                    identity,
                    failure_scope="image",
                    detail="image_download_failed",
                    error_code=exc.code,
                    attempts=exc.attempts,
                    source_index=exc.source_index,
                    **position,
                )
                return
        await self.update_xhs_note(note_detail)
        target_valid = valid and topic_relevant_for_web_post(
            "xhs", note_detail, fallback_keyword=self._scan.keyword,
        )
        accumulator.consider(identity, valid=target_valid)

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
