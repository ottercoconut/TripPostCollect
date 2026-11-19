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
from typing import Any, Dict, List, Optional

import httpx
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
from . import parser as weibo_store
from .client import WeiboClient
from .detail_page import WeiboDetailPages
from .models import DataFetchError, PlatformRuntimeError, WeiboImageDownloadError, WeiboFullTextFetchError
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
        self.mobile_index_url = "https://m.weibo.cn"
        self.cookie_urls = [self.mobile_index_url]
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
        if self.config.CRAWLER_TYPE == "search":
            await self._open_search_page(keyword)
            await self.ports.run_required_human_behavior(self.context_page, "weibo")
            await self.search()
        elif self.config.CRAWLER_TYPE == "detail":
            self.source_keyword = keyword
            await self._open_search_page(keyword)
            await self.ports.run_required_human_behavior(self.context_page, "weibo")
            await self.get_specified_notes()

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
                    # 未实现的详情直接抛出，不写成候选级失败，也不回退到移动详情。
                    await self.detail_pages.open_from_search(candidate)
                    processed_count += 1
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

    async def get_specified_notes(self):
        """
        get specified notes info
        :return:
        """
        semaphore = asyncio.Semaphore(self.config.MAX_CONCURRENCY_NUM)
        task_list = [self.get_note_info_task(note_id=note_id, semaphore=semaphore) for note_id in self.config.WEIBO_SPECIFIED_ID_LIST]
        video_details = await asyncio.gather(*task_list)
        for note_item in video_details:
            if note_item:
                mblog = note_item.get("mblog") or {}
                if weibo_store._weibo_pic_urls(mblog):
                    try:
                        await self.get_note_images(mblog)
                    except WeiboImageDownloadError as exc:
                        if self.ports.is_runtime_blocking_image_error(exc.code):
                            raise
                        logger.warning(
                            "[WeiboCrawler.get_specified_notes] Skip note after image "
                            f"failure: {exc}"
                        )
                        continue
                await self.update_weibo_note(note_item)
        

    async def get_note_info_task(self, note_id: str, semaphore: asyncio.Semaphore) -> Optional[Dict]:
        """
        Get note detail task
        :param note_id:
        :param semaphore:
        :return:
        """
        async with semaphore:
            try:
                result = await self.wb_client.get_note_info_by_id(note_id)
                mblog = (result or {}).get("mblog") or {}
                if mblog.get("text"):
                    mblog["content_detail_status"] = "detail_observed"
                    mblog["content_detail_source"] = "mobile_detail"

                # Sleep after fetching note details
                await asyncio.sleep(self.config.CRAWLER_MAX_SLEEP_SEC)
                logger.info(f"[WeiboCrawler.get_note_info_task] Sleeping for {self.config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching note details {note_id}")

                return result
            except PlatformRuntimeError:
                # 登录、频控、风控是运行级阻断，不能当作单条详情缺失吞掉（#114）。
                raise
            except httpx.TransportError as ex:
                # #129：重试用尽后的传输层失败不是单条详情缺失，按运行级 network_unreachable 上抛。
                raise PlatformRuntimeError(
                    f"weibo network_unreachable: {type(ex).__name__}",
                    code="network_unreachable",
                ) from ex
            except DataFetchError as ex:
                logger.error(f"[WeiboCrawler.get_note_info_task] Get note detail error: {ex}")
                return None
            except KeyError as ex:
                logger.error(f"[WeiboCrawler.get_note_info_task] have not fund note detail note_id:{note_id}, err: {ex}")
                return None



    async def get_note_images(self, mblog: Dict):
        """
        get note images
        :param mblog:
        :return:
        """
        if not self.config.ENABLE_GET_MEIDAS:
            logger.info("[WeiboCrawler.get_note_images] Crawling image mode is not enabled")
            return

        note_id = str(mblog.get("id") or "")
        image_assets = weibo_store._weibo_pic_assets(mblog)
        if not image_assets:
            return
        fetched_assets: List[Dict] = []
        for asset in image_assets:
            source_index = int(asset["source_index"])
            try:
                content, attempts = await self.ports.fetch_image_bytes_with_retry(
                    lambda: self.wb_client.get_note_image(asset["url"]),
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
            await asyncio.sleep(self.config.CRAWLER_MAX_SLEEP_SEC)
            logger.info(
                f"[WeiboCrawler.get_note_images] Sleeping for "
                f"{self.config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching image"
            )
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
        """Create xhs client"""
        logger.info("[WeiboCrawler.create_weibo_client] Begin create weibo API client ...")
        cookie_str, cookie_dict = await self.ports.client.convert_browser_context_cookies(
            self.browser_context,
            urls=self.cookie_urls,
        )
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
        weibo_client_obj = WeiboClient(
            proxy=httpx_proxy,
            headers={
                # #127/#211：与浏览器一致，经已登记的状态脚本读取 UA；图片请求沿用这里的 UA。
                "User-Agent": str(user_agent or ""),
                "Cookie": cookie_str,
                "Origin": "https://m.weibo.cn",
                "Referer": "https://m.weibo.cn",
                "Content-Type": "application/json;charset=UTF-8",
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
            ports=self.ports.client,
            post_repair=self.ports.post_repair,
        )
        return weibo_client_obj

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

    async def get_note_full_text(self, note_item: Dict) -> Dict:
        """
        Get full text content of a post
        If the post content is truncated (isLongText=True), request the detail API to get complete content
        :param note_item: Post data, contains mblog field
        :return: Updated post data
        """
        mblog = note_item.get("mblog", {})
        if not mblog:
            return note_item

        # Check if it's a long text
        is_long_text = mblog.get("isLongText", False)
        if not is_long_text:
            mblog["content_detail_status"] = "detail_observed"
            mblog["content_detail_source"] = "search_mblog_complete"
            return note_item

        note_id = str(mblog.get("id") or "")
        if not note_id:
            raise WeiboFullTextFetchError(note_id, "missing_note_id", attempts=1)
        if not self.config.ENABLE_WEIBO_FULL_TEXT:
            raise WeiboFullTextFetchError(note_id, "full_text_disabled", attempts=1)

        try:
            logger.info(f"[WeiboCrawler.get_note_full_text] Fetching full text for note: {note_id}")
            full_note = await self.wb_client.get_note_info_by_id(note_id)
            if full_note and full_note.get("mblog"):
                # Replace original content with complete content
                note_item["mblog"] = full_note["mblog"]
                note_item["mblog"]["content_detail_status"] = "detail_observed"
                note_item["mblog"]["content_detail_source"] = "mobile_detail"
                logger.info(f"[WeiboCrawler.get_note_full_text] Successfully fetched full text for note: {note_id}")
            else:
                raise WeiboFullTextFetchError(note_id, "empty_detail_payload")

            # Sleep after request to avoid rate limiting
            await asyncio.sleep(self.config.CRAWLER_MAX_SLEEP_SEC)
        except PlatformRuntimeError as ex:
            logger.error(
                f"[WeiboCrawler.get_note_full_text] Platform runtime failure for {note_id}: {ex}"
            )
            raise WeiboFullTextFetchError(
                note_id,
                ex.code,
                runtime_blocking=True,
            ) from ex
        except httpx.TransportError as ex:
            # #129：详情 Tenacity 重试用尽后仍是传输层失败：网络整体故障与候选无关，停止本轮，不按候选跳过。
            logger.error(
                f"[WeiboCrawler.get_note_full_text] Network unreachable for {note_id}: "
                f"{type(ex).__name__}"
            )
            raise WeiboFullTextFetchError(
                note_id,
                "network_unreachable",
                runtime_blocking=True,
            ) from ex
        except DataFetchError as ex:
            logger.error(f"[WeiboCrawler.get_note_full_text] Failed to fetch full text for note {note_id}: {ex}")
            raise WeiboFullTextFetchError(note_id, "detail_request_failed") from ex
        except WeiboFullTextFetchError:
            raise
        except Exception as ex:
            logger.error(f"[WeiboCrawler.get_note_full_text] Unexpected error for note {note_id}: {ex}")
            raise WeiboFullTextFetchError(note_id, "unexpected_detail_failure") from ex

        return note_item


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
        """纯投影完成后，沿原调用次序创建内容出口并写出。"""
        item = weibo_store.update_weibo_note(
            note_item, source_keyword=self.source_keyword,
            current_timestamp=self.ports.current_timestamp,
            save_data_option=self.config.SAVE_DATA_OPTION,
        )
        if item is None:
            return
        logger.info(f"[store.weibo.update_weibo_note] weibo note id:{item['note_id']}, title:{item.get('content')[:24]} ...")
        await self.ports.store_factory().store_content(content_item=item)

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
