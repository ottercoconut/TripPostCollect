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

import logging
import os
from asyncio import Task
from typing import Dict, List, Optional

from playwright.async_api import BrowserContext, Error as PlaywrightError, Page

from trippostcollect.application.contracts import XhsPorts, XhsReaders, XhsSettings
from trippostcollect.platforms.xhs.author import XhsAuthorMixin
from trippostcollect.platforms.xhs.behavior import XhsBehaviorMixin, empty_page_focus
from trippostcollect.platforms.xhs.detail import XhsDetailMixin
from trippostcollect.platforms.xhs.errors import (
    IPBlockError,
    PlatformRuntimeError,
    XHSLoginExpired,
    XHSNetworkRecoveryTimeout,
    is_xhs_target_closed_failure,
    xhs_cdp_lifecycle_stop_detail,
)
from trippostcollect.platforms.xhs.login import XhsLoginMixin
from trippostcollect.platforms.xhs.manual_wait import XHSManualWaitBudget
from trippostcollect.platforms.xhs.media import XhsMediaMixin
from trippostcollect.platforms.xhs.navigation import XhsNavigationMixin, XhsSearchEntryFailed
from trippostcollect.platforms.xhs.overlay import XhsOverlayMixin
from trippostcollect.platforms.xhs.parser import XhsParserMixin, update_xhs_note
from trippostcollect.platforms.xhs.scan import ScanState, XhsScanMixin
from trippostcollect.platforms.xhs.session import XhsSessionMixin
from trippostcollect.runtime.browser import CDPBrowserLifecycleError
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
    """全量扫描的运行级失败细节：与登录恢复同一口径，另加搜索入口与轮中登录失效；其他异常返回 None。"""
    detail = _login_recovery_stop_detail(exc)
    if detail is not None:
        return detail
    if isinstance(exc, XhsSearchEntryFailed):
        return str(exc)
    if isinstance(exc, XHSLoginExpired):
        return exc.code
    return None


class XiaoHongShuCrawler(
    XhsScanMixin,
    XhsOverlayMixin,
    XhsSessionMixin,
    XhsNavigationMixin,
    XhsLoginMixin,
    XhsAuthorMixin,
    XhsDetailMixin,
    XhsMediaMixin,
    XhsBehaviorMixin,
    XhsParserMixin,
):
    context_page: Page
    browser_context: BrowserContext
    cdp_manager: Optional[object]

    def __init__(self, *, settings: XhsSettings, inputs: XhsReaders, ports: XhsPorts) -> None:
        self.settings = settings
        self.inputs = inputs
        self.ports = ports
        self.source_keyword = ""
        self.crawler_type = ""
        self.index_url = "https://www.xiaohongshu.com"
        self.explore_url = f"{self.index_url}/explore"
        self.cdp_manager = None
        self._browser_session_started = False
        self.post_interaction_mode = os.environ.get("TRIPPOSTCOLLECT_XHS_POST_INTERACTION", "none").strip()
        self.post_interaction_attempted = False
        # 作者 ID → (作者资料, 首次作者访问 id)；同一作者本轮只点一次，之后的记录引用首次访问。
        self.creator_profile_cache: Dict[str, tuple[Dict, Optional[str]]] = {}
        self._overlay_fail_streak = 0
        self._crawler_page_open_depth = 0
        self._initial_pages: Dict[int, Page] = {}
        self._new_pages: Dict[int, tuple[Page, float, str, float]] = {}
        self._new_page_guard_tasks: Dict[int, Task[None]] = {}
        # 业务阶段按需置前与焦点抽样的累计计数（#107），整体写入行为证据 page_focus。
        self._page_focus: Dict = empty_page_focus()
        self._navigation_diagnostics: List[Dict] = []
        self._navigation_observed_pages: set[int] = set()
        self._navigation_page_errors: List[Dict] = []
        self._navigation_request_failures: List[Dict] = []
        self._manual_wait_budget: Optional[XHSManualWaitBudget] = None
        self.capture: Optional[PageCaptureChannel] = None
        self._trigger_actions: List[tuple[int, str, str, Dict]] = []
        self._scan: Optional[ScanState] = None
        self._behavior_stage_done = False
        # 行为证据创建前的停顿与验证事件暂存（见 _guarded_pause）；行为阶段后并入并置空。
        self._pending_behavior_evidence: Optional[Dict] = None

    async def start(self) -> None:
        self.inputs.validate_login_contract()
        async with self.ports.async_playwright() as playwright:
            try:
                await self._run_browser_session(playwright, None)
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
    # 只记标识与计数：整条记录含作者头像键与值、xsec_token 和正文，会让子进程日志整段替换为头像审计标记（#80）。
    logger.info(
        f"[store.xhs.update_xhs_note] xhs note: note_id={local_db_item.get('note_id')}, "
        f"images={len(local_db_item.get('image_assets') or [])}"
    )
    await content_sink_factory(crawler_type).store_content(local_db_item)


async def update_xhs_note_images(note_id: str, image_content_items: List[Dict], *, image_stager_factory):
    """Atomically save all body images for one XHS note."""

    return await image_stager_factory().store_post_images(note_id, image_content_items)


async def record_xhs_note_image_failure(note_id: str, image_content_item: Dict, *, image_stager_factory):
    """Write one failed manifest row without success file metadata."""

    return await image_stager_factory().record_failure(note_id, image_content_item)
