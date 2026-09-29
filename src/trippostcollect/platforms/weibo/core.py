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
import os
from typing import Dict, List, Optional
from urllib.parse import quote

from playwright.async_api import BrowserContext, BrowserType, Page, Playwright, async_playwright

from trippostcollect.application.contracts import ImageStagingError, WeiboPorts
from trippostcollect.core import resources
from trippostcollect.core.paths import MEDIACRAWLER_DIR
from trippostcollect.records.topic_relevance import topic_relevant_for_web_post
from trippostcollect.runtime.helpers import get_user_agent, get_mobile_user_agent
from . import parser as weibo_store
from .client import WeiboClient
from .models import DataFetchError, PlatformRuntimeError, SearchType, WeiboImageDownloadError, WeiboFullTextFetchError
from .parser import filter_search_result_card
from .login import WeiboLogin

logger = logging.getLogger("MediaCrawler")

class WeiboCrawler:
    context_page: Page
    wb_client: WeiboClient
    browser_context: BrowserContext

    def __init__(self, config, ports: WeiboPorts):
        self.config = config
        self.ports = ports
        self.source_keyword = ""
        self.index_url = "https://www.weibo.com"
        self.mobile_index_url = "https://m.weibo.cn"
        self.cookie_urls = [self.mobile_index_url]
        self.user_agent = get_user_agent()
        self.mobile_user_agent = get_mobile_user_agent()
        self.cdp_manager = None

    async def start(self):
        playwright_proxy_format, httpx_proxy_format = None, None
        async with async_playwright() as playwright:
            # Select launch mode based on configuration
            if self.config.ENABLE_CDP_MODE:
                logger.info("[WeiboCrawler] Launching browser with CDP mode")
                self.browser_context = await self.launch_browser_with_cdp(
                    playwright,
                    playwright_proxy_format,
                    self.mobile_user_agent,
                    headless=self.config.CDP_HEADLESS,
                )
            else:
                logger.info("[WeiboCrawler] Launching browser with standard mode")
                # Launch a browser context.
                chromium = playwright.chromium
                self.browser_context = await self.launch_browser(chromium, None, self.mobile_user_agent, headless=self.config.HEADLESS)

                # stealth.min.js is a js script to prevent the website from detecting the crawler.
                with resources.path("js/stealth.min.js") as stealth_path:
                    await self.browser_context.add_init_script(path=str(stealth_path))


            self.context_page = await self.browser_context.new_page()
            await self.context_page.goto(self.index_url)
            await asyncio.sleep(2)


            # Create a client to interact with the xiaohongshu website.
            self.wb_client = await self.create_weibo_client(httpx_proxy_format)
            if not await self.wb_client.pong():
                login_obj = WeiboLogin(
                    login_type=self.config.LOGIN_TYPE,
                    login_phone="",  # your phone number
                    browser_context=self.browser_context,
                    context_page=self.context_page,
                    cookie_str=self.config.COOKIES,
                    ports=self.ports.login,
                )
                await login_obj.begin()

                # After successful login, redirect to mobile website and update mobile cookies
                logger.info("[WeiboCrawler.start] redirect weibo mobile homepage and update cookies on mobile platform")
                await self.context_page.goto(self.mobile_index_url)
                await asyncio.sleep(3)
                # Only get mobile cookies to avoid confusion between PC and mobile cookies
                await self.wb_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )

            if self.config.CRAWLER_TYPE == "search":
                # Search for video and retrieve their comment information.
                behavior_keyword = self.config.KEYWORDS.split(",", maxsplit=1)[0].strip()
                container_id = quote(f"100103type=1&q={behavior_keyword}", safe="")
                await self.context_page.goto(
                    f"{self.mobile_index_url}/search?containerid={container_id}",
                    wait_until="domcontentloaded",
                )
                await self.ports.run_required_human_behavior(self.context_page, "weibo")
                await self.wb_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )
                await self.search()
            elif self.config.CRAWLER_TYPE == "detail":
                # Get the information and comments of the specified post
                behavior_keyword = self.config.KEYWORDS.split(",", maxsplit=1)[0].strip()
                self.source_keyword = behavior_keyword
                container_id = quote(f"100103type=1&q={behavior_keyword}", safe="")
                await self.context_page.goto(
                    f"{self.mobile_index_url}/search?containerid={container_id}",
                    wait_until="domcontentloaded",
                )
                await self.ports.run_required_human_behavior(self.context_page, "weibo")
                await self.wb_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )
                await self.get_specified_notes()
            else:
                pass
            logger.info("[WeiboCrawler.start] Weibo Crawler finished ...")

    async def search(self):
        """
        search weibo note with keywords
        :return:
        """
        logger.info("[WeiboCrawler.search] Begin search weibo keywords")
        start_page = self.config.START_PAGE
        accumulator = self.ports.accumulator()

        # Set the search type based on the configuration for weibo
        if self.config.WEIBO_SEARCH_TYPE == "default":
            search_type = SearchType.DEFAULT
        elif self.config.WEIBO_SEARCH_TYPE == "real_time":
            search_type = SearchType.REAL_TIME
        elif self.config.WEIBO_SEARCH_TYPE == "popular":
            search_type = SearchType.POPULAR
        elif self.config.WEIBO_SEARCH_TYPE == "video":
            search_type = SearchType.VIDEO
        else:
            logger.error(f"[WeiboCrawler.search] Invalid WEIBO_SEARCH_TYPE: {self.config.WEIBO_SEARCH_TYPE}")
            return

        for keyword in self.config.KEYWORDS.split(","):
            self.source_keyword = keyword
            logger.info(f"[WeiboCrawler.search] Current search keyword: {keyword}")
            phases: list[tuple[str, int, int | None]] = []
            refresh_max_pages = self.ports.refresh_max_pages()
            if start_page > 1 and refresh_max_pages > 0:
                phases.append(("refresh", 1, min(start_page - 1, refresh_max_pages)))
            source_exhausted = self.ports.source_exhausted()
            if not source_exhausted:
                phases.append(("frontier", start_page, None))
            for discovery_phase, phase_start, phase_end in phases:
                page = phase_start
                while (
                    accumulator.can_continue
                    and (phase_end is None or page <= phase_end)
                ):
                    logger.info(
                        f"[WeiboCrawler.search] search weibo keyword: {keyword}, page: {page}"
                    )
                    requested_page = page
                    try:
                        search_res = await self.wb_client.get_note_by_keyword(
                            keyword=keyword,
                            page=requested_page,
                            search_type=search_type,
                        )
                    except DataFetchError:
                        accumulator.mark_runtime_failed(
                            "search_request_failed",
                            source_page=requested_page,
                            resume_page=requested_page,
                            discovery_phase=discovery_phase,
                        )
                        raise
                    note_id_list: List[str] = []
                    note_list = filter_search_result_card(search_res.get("cards"))
                    if not note_list:
                        if discovery_phase == "frontier":
                            accumulator.mark_source_exhausted(
                                "empty_page",
                                source_page=requested_page,
                                raw_batch_count=0,
                                resume_page=requested_page,
                                source_has_more=False,
                                discovery_phase=discovery_phase,
                            )
                        break
                    raw_batch_count = len(note_list)
                    pending_note_ids: set[str] = set()
                    unknown_notes = []
                    for note_item in note_list:
                        note_id = str(
                            ((note_item or {}).get("mblog") or {}).get("id") or ""
                        )
                        if accumulator.is_known(note_id) or (
                            note_id and note_id in pending_note_ids
                        ):
                            continue
                        if note_id:
                            pending_note_ids.add(note_id)
                        unknown_notes.append(note_item)
                    note_list = unknown_notes
                    accumulator.begin_batch()
                    processed_count = 0
                    for note_item in note_list:
                        processed_count += 1
                        if not note_item:
                            continue
                        preliminary_id = str(
                            ((note_item or {}).get("mblog") or {}).get("id") or ""
                        )
                        try:
                            note_item = await self.get_note_full_text(note_item)
                        except WeiboFullTextFetchError as exc:
                            if exc.runtime_blocking:
                                accumulator.mark_runtime_failed(
                                    exc.code,
                                    source_page=requested_page,
                                    resume_page=requested_page,
                                    discovery_phase=discovery_phase,
                                )
                                break
                            logger.error(
                                "[WeiboCrawler.search] Skip full-text candidate after "
                                f"failed attempts: {exc!r}"
                            )
                            should_stop = accumulator.skip_candidate_failure(
                                preliminary_id or exc.note_id,
                                failure_scope="post",
                                detail="full_text_request_failed",
                                error_code=exc.code,
                                attempts=exc.attempts,
                                retryable=True,
                                source_page=requested_page,
                                discovery_phase=discovery_phase,
                            )
                            if should_stop:
                                break
                            continue
                        mblog: Dict = note_item.get("mblog")
                        if not mblog:
                            continue
                        note_id = str(mblog.get("id") or "")
                        user = mblog.get("user") or {}
                        followers_observed = any(
                            key in user and user.get(key) not in (None, "")
                            for key in (
                                "followers_count",
                                "followers_count_str",
                                "fans_count",
                                "fans_count_str",
                            )
                        )
                        valid = bool(
                            note_id
                            and mblog.get("text")
                            and mblog.get("content_detail_status") == "detail_observed"
                            and mblog.get("content_detail_source")
                            in {"search_mblog_complete", "mobile_detail"}
                            and mblog.get("created_at")
                            and user.get("id")
                            and user.get("screen_name")
                            and followers_observed
                            and weibo_store._weibo_pic_urls(mblog)
                            and all(
                                mblog.get(key) not in (None, "")
                                for key in (
                                    "attitudes_count",
                                    "comments_count",
                                    "reposts_count",
                                )
                            )
                        )
                        if valid:
                            try:
                                await self.get_note_images(mblog)
                            except WeiboImageDownloadError as exc:
                                if self.ports.is_runtime_blocking_image_error(exc.code):
                                    accumulator.mark_runtime_failed(
                                        exc.code,
                                        source_page=requested_page,
                                        resume_page=requested_page,
                                        discovery_phase=discovery_phase,
                                    )
                                    break
                                should_stop = accumulator.skip_candidate_failure(
                                    note_id,
                                    failure_scope="image",
                                    detail="image_download_failed",
                                    error_code=exc.code,
                                    attempts=exc.attempts,
                                    source_index=exc.source_index,
                                    source_page=requested_page,
                                    discovery_phase=discovery_phase,
                                )
                                if should_stop:
                                    break
                                continue
                        await self.update_weibo_note(note_item)
                        note_id_list.append(note_id)
                        target_valid = valid and topic_relevant_for_web_post(
                            "weibo",
                            {
                                "content": weibo_store.persisted_weibo_content_text(
                                    mblog
                                ),
                                "source_keyword": keyword,
                            },
                            fallback_keyword=keyword,
                        )
                        should_stop = accumulator.consider(note_id, valid=target_valid)
                        if should_stop:
                            break

                    batch_complete = processed_count >= len(note_list)
                    page += 1
                    await asyncio.sleep(self.config.CRAWLER_MAX_SLEEP_SEC)
                    logger.info(
                        f"[WeiboCrawler.search] Sleeping for {self.config.CRAWLER_MAX_SLEEP_SEC} "
                        f"seconds after page {page-1}"
                    )
                    
                    if accumulator.finish_batch(
                        source_page=requested_page,
                        raw_batch_count=raw_batch_count,
                        resume_page=page if batch_complete else requested_page,
                        batch_complete=batch_complete,
                        discovery_phase=discovery_phase,
                        count_stagnation=discovery_phase == "frontier",
                    ):
                        break
            if source_exhausted and not accumulator.stop_reason:
                accumulator.mark_source_exhausted(
                    "saved_source_exhausted",
                    source_page=start_page,
                    resume_page=start_page,
                    source_has_more=False,
                    raw_batch_count=0,
                    discovery_phase="frontier",
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
        weibo_client_obj = WeiboClient(
            proxy=httpx_proxy,
            headers={
                "User-Agent": get_mobile_user_agent(),
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
            user_data_dir = os.path.join(MEDIACRAWLER_DIR, "browser_data", self.config.USER_DATA_DIR % self.config.PLATFORM)  # type: ignore
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
                args=self.ports.project_browser_args(),
            )
            return browser_context
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy)  # type: ignore
            browser_context = await browser.new_context(viewport={"width": 1920, "height": 1080}, user_agent=user_agent)
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
