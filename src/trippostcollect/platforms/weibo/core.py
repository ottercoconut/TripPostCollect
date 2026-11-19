# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/weibo/core.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2023/12/23 15:41
# @Desc    : Weibo crawler main workflow code


# TripPostCollect T05：迁自 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303；仅拆分职责与注入依赖。
import asyncio
import logging
from hashlib import sha256
from typing import Any, Dict, List, Optional

from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Error as PlaywrightError,
    Page,
    Playwright,
    async_playwright,
)

from trippostcollect.application.contracts import ImageStagingError, WeiboPorts
from trippostcollect.core.paths import PROFILE_VIEWPORT, platform_profile_dir
from trippostcollect.records.topic_relevance import topic_relevant_for_web_post
from . import parser as weibo_store
from .client import WEIBO_CONTENT_IMAGE_RULE, WeiboClient
from .detail_page import WeiboDetailPages, WeiboOpened
from .models import (
    PlatformRuntimeError, WeiboDetailFetchError, WeiboImageDownloadError, WeiboRepairNotImplemented,
)
from .search_page import (
    HOME_URL,
    WeiboPageCapture,
    WeiboSearchPageInvalid,
    WeiboSearchSnapshot,
    search_url,
)

logger = logging.getLogger("TripPostCollect")

class WeiboCrawler:
    context_page: Page
    wb_client: WeiboClient
    browser_context: BrowserContext

    def __init__(self, config, ports: WeiboPorts):
        self.config = config
        self.ports = ports
        self.source_keyword = ""
        self.index_url = HOME_URL
        self.cdp_manager = None
        self._capture: WeiboPageCapture | None = None
        self.detail_pages: WeiboDetailPages | None = None
        self._opened_keyword = ""

    async def start(self):
        playwright_proxy_format, httpx_proxy_format = None, None
        async with async_playwright() as playwright:
            # Select launch mode based on configuration
            if self.config.ENABLE_CDP_MODE:
                logger.info("[WeiboCrawler] Launching browser with CDP mode")
                # #127：不强制 UA，浏览器自报真实 UA；httpx 头从 navigator.userAgent 读取。
                self.browser_context = await self.launch_browser_with_cdp(
                    playwright,
                    playwright_proxy_format,
                    None,
                    headless=self.config.CDP_HEADLESS,
                )
            else:
                logger.info("[WeiboCrawler] Launching browser with standard mode")
                # Launch a browser context.
                chromium = playwright.chromium
                self.browser_context = await self.launch_browser(chromium, None, None, headless=self.config.HEADLESS)

            # #136：标准模式持久 context 复用自带的第一页，不另开空白页；没有页面时才新建。CDP 与非持久分支照旧新建。
            persistent = self.config.SAVE_LOGIN_STATE and not self.config.ENABLE_CDP_MODE
            pages = self.browser_context.pages if persistent else []
            self.context_page = pages[0] if pages else await self.browser_context.new_page()
            try:
                await self._run_after_browser_ready(httpx_proxy_format)
            finally:
                await self._close_capture()
            logger.info("[WeiboCrawler.start] Weibo Crawler finished ...")

    async def _close_capture(self) -> None:
        capture = getattr(self, "_capture", None)
        if capture is None:
            return
        self._capture = None
        await capture.aclose()

    def _stop_for_block(self, code: str) -> None:
        """登录、验证或频控：搜索模式记运行级停止后返回，不发起搜索，也不在本轮登录。"""

        logger.error(
            "[WeiboCrawler.start] Weibo session blocked code=%s; "
            "run scripts/login_warmup.py --targets weibo before the next formal run",
            code,
        )
        if self.config.CRAWLER_TYPE != "search":
            raise PlatformRuntimeError(f"weibo {code}", code=code)
        self.ports.accumulator().mark_runtime_failed(
            code,
            source_page=1,
            resume_page=1,
            discovery_phase="frontier",
        )

    async def _run_after_browser_ready(self, httpx_proxy_format: Optional[str]) -> None:
        if getattr(self.config, "CRAWLER_TYPE", "search") == "detail":
            raise WeiboRepairNotImplemented()
        try:
            await self.context_page.goto(
                self.index_url, wait_until="domcontentloaded", timeout=30_000,
            )
        except PlaywrightError as exc:
            logger.warning(
                "[WeiboCrawler.start] Homepage navigation did not settle: %s",
                type(exc).__name__,
            )
        ledger_mode = "repair" if self.config.CRAWLER_TYPE == "detail" else "discovery"
        self._capture = WeiboPageCapture.open(self.browser_context, mode=ledger_mode)
        self._capture.install(self.context_page)
        self.detail_pages = WeiboDetailPages(self._capture, self.context_page)
        blocked = await self._capture.confirm_login(self.context_page)
        if blocked:
            self._stop_for_block(blocked)
            return
        self.wb_client = await self.create_weibo_client(httpx_proxy_format)
        keyword = self.config.KEYWORDS.split(",", maxsplit=1)[0].strip()
        await self._open_search_page(keyword)
        await self.ports.run_required_human_behavior(self.context_page, "weibo")
        await self.search()

    async def _open_search_page(self, keyword: str) -> None:
        capture = self._capture
        if capture is None:
            raise RuntimeError("weibo_capture_channel_missing")
        capture.begin_navigation()
        await self.context_page.goto(
            search_url(keyword),
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        self._opened_keyword = keyword

    async def search(self) -> None:
        """#211：每轮从搜索第一页点下一页翻到结束标记。不按保存页码恢复，也不做顶部刷新。"""

        logger.info("[WeiboCrawler.search] Begin search weibo keywords")
        accumulator = self.ports.accumulator()
        discovery_phase = "frontier"
        capture = self._capture
        if capture is None:
            raise RuntimeError("weibo_capture_channel_missing")
        if self.detail_pages is None:
            self.detail_pages = WeiboDetailPages(capture, self.context_page)
        for raw_keyword in self.config.KEYWORDS.split(","):
            keyword = raw_keyword.strip()
            if not keyword or not accumulator.can_continue:
                continue
            self.source_keyword = keyword
            logger.info("[WeiboCrawler.search] Current search keyword: %s", keyword)
            if keyword != self._opened_keyword:
                await self._open_search_page(keyword)
            page = 1
            while accumulator.can_continue:
                requested_page = page
                logger.info(
                    "[WeiboCrawler.search] search weibo keyword page=%s",
                    requested_page,
                )
                try:
                    snapshot = await capture.read_search(requested_page)
                except WeiboSearchPageInvalid:
                    accumulator.mark_runtime_failed(
                        "search_page_invalid",
                        source_page=requested_page,
                        resume_page=requested_page,
                        discovery_phase=discovery_phase,
                    )
                    return
                except PlatformRuntimeError as exc:
                    accumulator.mark_runtime_failed(
                        exc.code,
                        source_page=requested_page,
                        resume_page=requested_page,
                        discovery_phase=discovery_phase,
                    )
                    return
                if snapshot.stop_detail and not snapshot.cards:
                    self._mark_search_exhausted(accumulator, snapshot, requested_page, discovery_phase)
                    return
                unknown = self._unknown_candidates(accumulator, snapshot)
                accumulator.begin_batch()
                processed_count = 0
                for candidate in unknown:
                    try:
                        opened = await self.detail_pages.open_from_search(candidate)
                    except WeiboDetailFetchError as exc:
                        if exc.runtime_blocking:
                            accumulator.mark_runtime_failed(
                                exc.code,
                                source_page=requested_page,
                                resume_page=requested_page,
                                discovery_phase=discovery_phase,
                            )
                            processed_count += 1
                            break
                        logger.error(
                            "[WeiboCrawler.search] Skip note after detail attempts failed "
                            "mid=%s code=%s attempts=%s",
                            candidate.mid,
                            exc.code,
                            exc.attempts,
                        )
                        processed_count += 1
                        if accumulator.skip_candidate_failure(
                            candidate.mid,
                            failure_scope="post",
                            detail="content_detail_failed",
                            error_code=exc.code,
                            attempts=exc.attempts,
                            retryable=exc.retryable,
                            source_page=requested_page,
                            discovery_phase=discovery_phase,
                        ):
                            break
                        continue
                    processed_count += 1
                    if not isinstance(opened, WeiboOpened):
                        continue
                    if opened.video:
                        accumulator.consider(candidate.mid, valid=False)
                        continue
                    record = opened.record
                    if record is None:
                        continue
                    record["source_keyword"] = self.source_keyword
                    record["last_modify_ts"] = self.ports.current_timestamp()
                    skip_code = weibo_store.structural_skip_code(record)
                    if skip_code:
                        if accumulator.skip_candidate_failure(
                            candidate.mid,
                            failure_scope="post",
                            detail="content_detail_failed",
                            error_code=skip_code,
                            attempts=1,
                            retryable=False,
                            source_page=requested_page,
                            discovery_phase=discovery_phase,
                        ):
                            break
                        continue
                    try:
                        await self.get_note_images(record)
                    except WeiboImageDownloadError as exc:
                        if self.ports.is_runtime_blocking_image_error(exc.code):
                            accumulator.mark_runtime_failed(
                                exc.code,
                                source_page=requested_page,
                                resume_page=requested_page,
                                discovery_phase=discovery_phase,
                            )
                            break
                        if accumulator.skip_candidate_failure(
                            candidate.mid,
                            failure_scope="image",
                            detail="image_download_failed",
                            error_code=exc.code,
                            attempts=exc.attempts,
                            source_index=exc.source_index,
                            source_page=requested_page,
                            discovery_phase=discovery_phase,
                        ):
                            break
                        continue
                    await self.update_weibo_note(record)
                    if accumulator.consider(
                        candidate.mid,
                        valid=topic_relevant_for_web_post(
                            "weibo", record, fallback_keyword=keyword,
                        ),
                    ):
                        break
                batch_complete = processed_count >= len(unknown)
                resume_page = requested_page + 1 if batch_complete and snapshot.has_more else requested_page
                if accumulator.finish_batch(
                    source_page=requested_page,
                    source_has_more=snapshot.has_more,
                    raw_batch_count=len(snapshot.cards),
                    resume_page=resume_page,
                    batch_complete=batch_complete,
                    discovery_phase=discovery_phase,
                    count_stagnation=True,
                ):
                    return
                if snapshot.stop_detail:
                    self._mark_search_exhausted(
                        accumulator,
                        snapshot,
                        requested_page,
                        discovery_phase,
                        batch_complete=batch_complete,
                    )
                    return
                try:
                    await capture.turn_page()
                except WeiboSearchPageInvalid:
                    accumulator.mark_runtime_failed(
                        "search_page_invalid",
                        source_page=requested_page,
                        resume_page=requested_page,
                        discovery_phase=discovery_phase,
                    )
                    return
                page += 1

    def _unknown_candidates(self, accumulator: Any, snapshot: WeiboSearchSnapshot) -> list:
        pending: set[str] = set()
        unknown = []
        for candidate in snapshot.cards:
            mid = candidate.mid
            if accumulator.is_known(mid) or mid in pending:
                continue
            pending.add(mid)
            unknown.append(candidate)
        return unknown

    def _mark_search_exhausted(
        self,
        accumulator: Any,
        snapshot: WeiboSearchSnapshot,
        source_page: int,
        discovery_phase: str,
        *,
        batch_complete: bool = True,
    ) -> None:
        accumulator.mark_source_exhausted(
            snapshot.stop_detail or "search_page_invalid",
            source_page=source_page,
            raw_batch_count=len(snapshot.cards),
            resume_page=source_page,
            source_has_more=False,
            batch_complete=batch_complete,
            discovery_phase=discovery_phase,
            source_visit_id=snapshot.visit_id or None,
            source_page_info=snapshot.page_info,
            source_max_page=snapshot.max_page,
            page_cap_reached=snapshot.page_cap_reached,
        )


    async def get_note_images(self, record: Dict):
        """下载本条 pic_infos 正文图，并按 cdn_direct 登记。"""

        if not self.config.ENABLE_GET_MEIDAS:
            logger.info("[WeiboCrawler.get_note_images] Crawling image mode is not enabled")
            return
        note_id = str(record.get("note_id") or "")
        image_assets = list(record.get("image_assets") or [])
        if not image_assets:
            return
        fetched_assets: List[Dict] = []
        capture = self._capture
        for asset in image_assets:
            source_index = int(asset["source_index"])
            urls = list(asset.get("urls") or [asset.get("url")])
            try:
                content, attempts = await self.ports.fetch_image_bytes_with_retry(
                    lambda urls=urls: self.wb_client.get_note_image(urls),
                    logger=logger,
                    label=(
                        f"platform=weibo note_id={note_id} "
                        f"source_index={source_index}"
                    ),
                )
            except self.ports.image_error as exc:
                await self.record_weibo_note_image_failure(
                    note_id,
                    {
                        **asset,
                        "attempts": exc.attempts,
                        "http_status": exc.http_status,
                        "error_code": exc.code,
                    },
                )
                raise WeiboImageDownloadError(
                    note_id, source_index, exc.code, exc.attempts
                ) from exc
            if self.config.CRAWLER_MAX_SLEEP_SEC:
                await asyncio.sleep(self.config.CRAWLER_MAX_SLEEP_SEC)
            if content is None:
                failure = {
                    **asset,
                    "attempts": attempts,
                    "http_status": None,
                    "error_code": "image_download_retryable",
                }
                await self.record_weibo_note_image_failure(note_id, failure)
                raise WeiboImageDownloadError(
                    note_id,
                    source_index,
                    "image_download_retryable",
                    attempts,
                )
            digest = sha256(content).hexdigest()
            asset_key = weibo_store.weibo_source_asset_key(asset.get("pid"), str(asset.get("url") or ""))
            if capture is not None:
                visit_id = capture.channel.record_direct_visit(
                    source="cdn_direct",
                    stage="note_image",
                    rule=WEIBO_CONTENT_IMAGE_RULE,
                    subject_id=note_id,
                    trigger=None,
                    status=200,
                    asset_key=asset_key,
                    sha256=digest,
                )
                refs = dict(record.get("content_visit_refs") or {})
                refs["images"] = [*list(refs.get("images") or []), visit_id]
                record["content_visit_refs"] = refs
            fetched_assets.append(
                {
                    **asset,
                    "content": content,
                    "attempts": attempts,
                    "http_status": 200,
                }
            )
        try:
            await self.update_weibo_note_images(note_id, fetched_assets)
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
            await self.record_weibo_note_image_failure(
                note_id,
                {
                    **failed_asset,
                    "attempts": int(failed_asset.get("attempts") or 1),
                    "http_status": 200,
                    "error_code": exc.code,
                },
            )
            raise WeiboImageDownloadError(
                note_id,
                source_index,
                exc.code,
                int(failed_asset.get("attempts") or 1),
            ) from exc

    async def create_weibo_client(self, httpx_proxy: Optional[str]) -> WeiboClient:
        """正文图只带浏览器 UA。代理参数保留调用签名，图片请求不使用它。"""

        logger.info("[WeiboCrawler.create_weibo_client] Begin create weibo image client")
        _ = httpx_proxy
        capture = self._capture
        if capture is None:
            raise RuntimeError("weibo_capture_channel_missing")
        user_agent, _visit = await capture.channel.read_state(
            self.context_page,
            "weibo.user_agent",
            stage="browser_ua",
            subject_id=None,
            trigger=None,
        )
        return WeiboClient(user_agent=str(user_agent or ""), ports=self.ports.client)

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        logger.info("[WeiboCrawler.launch_browser] Begin create browser context ...")
        if self.config.SAVE_LOGIN_STATE:
            # #136：与 warmup 共用持久 context 启动配置（沙箱开启、视口、locale/timezone），不传 UA。
            # 正式流程 ENABLE_IP_PROXY=False，playwright_proxy 恒为 None，共享配置不带代理。
            return await self.ports.launch_profile_context(
                chromium, platform_profile_dir("weibo"), headless=headless,
            )
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy)  # type: ignore
            browser_context = await browser.new_context(viewport=PROFILE_VIEWPORT, user_agent=user_agent)
            return browser_context

    async def launch_browser_with_cdp(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """
        Launch browser with CDP mode
        """
        try:
            self.cdp_manager = self.ports.browser_manager()
            browser_context = await self.cdp_manager.launch_and_connect(
                playwright=playwright,
                playwright_proxy=playwright_proxy,
                user_agent=user_agent,
                headless=headless,
            )

            # Display browser information
            browser_info = await self.cdp_manager.get_browser_info()
            logger.info(f"[WeiboCrawler] CDP browser info: {browser_info}")

            return browser_context

        except Exception as e:
            logger.error(f"[WeiboCrawler] CDP mode startup failed, falling back to standard mode: {e}")
            # Fallback to standard mode
            chromium = playwright.chromium
            return await self.launch_browser(chromium, playwright_proxy, user_agent, headless)

    async def close(self):
        """Close browser context"""
        await self._close_capture()
        # Special handling if using CDP mode
        if self.cdp_manager:
            await self.cdp_manager.cleanup()
            self.cdp_manager = None
        else:
            await self.browser_context.close()
        logger.info("[WeiboCrawler.close] Browser context closed ...")

    async def update_weibo_note(self, note_item: Dict):
        """写出已经投影的记录，或把 show/side_user 包投影后再写出。"""

        if note_item.get("note_id") and note_item.get("content_detail_source") == "desktop_detail":
            record = dict(note_item)
        else:
            parsed = weibo_store.parse_weibo_detail(
                note_item.get("status") or note_item.get("mblog") or {},
                note_item.get("side_user") or {},
                mblogid=str(note_item.get("mblogid") or ""),
                source_keyword=self.source_keyword,
                current_timestamp=self.ports.current_timestamp,
                visit_refs=note_item.get("content_visit_refs") or {},
            )
            record = parsed.record
        if not record:
            return
        record["source_keyword"] = self.source_keyword or record.get("source_keyword") or ""
        record["last_modify_ts"] = self.ports.current_timestamp()
        logger.info(
            "[store.weibo.update_weibo_note] weibo note id:%s images:%s",
            record.get("note_id"),
            record.get("image_count"),
        )
        await self.ports.store_factory().store_content(content_item=record)

    async def update_weibo_note_images(self, note_id: str, image_content_items: List[Dict]):
        return await self.ports.image_stager().store_post_images(note_id, image_content_items)

    async def record_weibo_note_image_failure(self, note_id: str, image_content_item: Dict):
        return await self.ports.image_stager().record_failure(note_id, image_content_item)


def bind_weibo_crawler(config, ports_factory):
    """给 worker 的无参 crawler 类绑定装配；每个实例独立构造快照与端口。"""
    class ConfiguredWeiboCrawler(WeiboCrawler):
        def __init__(self):
            options, ports = ports_factory(config)
            super().__init__(options, ports)

    ConfiguredWeiboCrawler.__name__ = WeiboCrawler.__name__
    return ConfiguredWeiboCrawler
