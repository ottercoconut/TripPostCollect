# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/zhihu/core.py
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

# TripPostCollect：T07 迁入根平台；来源 MediaCrawler 5a68eb5098fcd17308c7fe0b9d53916ae839b303，许可见 resources/licenses/MediaCrawler-LICENSE。

import asyncio
import logging
import os
from typing import Dict, List, Optional, cast, Any
from urllib.parse import quote
from playwright.async_api import BrowserContext, BrowserType, Page, Playwright
from tenacity import RetryError
from trippostcollect.application.contracts import ImageStagingError, ZhihuSettings, ZhihuPorts
from trippostcollect.core import resources
from trippostcollect.core.paths import MEDIACRAWLER_DIR
from trippostcollect.records.topic_relevance import topic_relevant_for_web_post
from trippostcollect.runtime.cookies import convert_str_cookie_to_dict
from trippostcollect.runtime.image_retry import ImageDownloadFetchError, is_runtime_blocking_image_error
from . import models as constant
from .models import ZhihuContent, DataFetchError, PlatformRuntimeError, ZhihuImageDownloadError, ZhihuDetailFetchError
from .client import ZhiHuClient
from .parser import ZhihuExtractor, judge_zhihu_url, merge_search_content_detail
from . import parser as zhihu_store

logger = logging.getLogger("MediaCrawler")

class ZhihuCrawler:
    context_page: Page
    zhihu_client: ZhiHuClient
    browser_context: BrowserContext
    cdp_manager: Optional[Any]

    @classmethod
    def with_dependencies(cls, factory):
        """返回仍由本站定义的 worker 类；每次构造独立装配，避免全局配置。"""
        class ZhihuCrawler(cls):
            def __init__(self):
                super().__init__(*factory())

        return ZhihuCrawler

    def __init__(self, settings: ZhihuSettings, ports: ZhihuPorts) -> None:
        self.settings = settings
        self.ports = ports
        self.source_keyword = ""
        self.index_url = "https://www.zhihu.com"
        self.cookie_urls = [self.index_url]
        # self.user_agent = utils.get_user_agent()
        self.user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        self._extractor = ZhihuExtractor()
        self.cdp_manager = None
        self.ip_proxy_pool = None  # Proxy IP pool for automatic proxy refresh


    async def _activate_latest_zhihu_page(self) -> None:
        try:
            pages = [page for page in self.browser_context.pages if not page.is_closed()]
        except Exception:
            return
        for page in reversed(pages):
            if "zhihu.com" in (page.url or ""):
                self.context_page = page
                return

    async def _close_stale_pages(self, keep_page: Page) -> None:
        try:
            pages = [page for page in self.browser_context.pages if not page.is_closed()]
        except Exception:
            return

        closed_count = 0
        for page in pages:
            if page is keep_page:
                continue
            try:
                await page.close(run_before_unload=False)
                closed_count += 1
            except Exception as exc:
                logger.warning(
                    f"[ZhihuCrawler] Failed to close stale browser tab {page.url}: {exc}"
                )

        if closed_count:
            logger.info(f"[ZhihuCrawler] Closed {closed_count} stale browser tabs")

    async def _wait_for_initial_login_settle(self) -> None:
        settle_seconds = self.ports.initial_settle_seconds()
        await self._activate_latest_zhihu_page()
        try:
            await self.context_page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:
            pass
        try:
            await self.context_page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        if settle_seconds > 0:
            logger.info(
                f"[ZhihuCrawler] Waiting {settle_seconds:.1f}s for Zhihu login state settle ..."
            )
            await asyncio.sleep(settle_seconds)
        await self._activate_latest_zhihu_page()

    async def enrich_search_content_detail(
        self,
        content: ZhihuContent,
    ) -> ZhihuContent:
        """Fetch the full answer/article when search HTML has no content image."""
        if content.image_list and content.content_text:
            content.content_detail_status = "detail_observed"
            content.content_detail_source = "search_content"
            return content
        if content.content_type not in {
            constant.ANSWER_NAME,
            constant.ARTICLE_NAME,
        }:
            return content

        try:
            if content.content_type == constant.ANSWER_NAME:
                detail = await self.zhihu_client.get_answer_info(
                    content.question_id,
                    content.content_id,
                )
            else:
                detail = await self.zhihu_client.get_article_info(content.content_id)
        except (DataFetchError, RetryError) as exc:
            nested = exc
            if isinstance(exc, RetryError):
                try:
                    nested = exc.last_attempt.exception() or exc
                except Exception:
                    nested = exc
            if isinstance(nested, PlatformRuntimeError):
                raise ZhihuDetailFetchError(
                    content.content_id,
                    nested.code,
                    runtime_blocking=True,
                ) from exc
            logger.warning(
                "[ZhihuCrawler.enrich_search_content_detail] Detail request failed "
                f"for {content.content_type}:{content.content_id}: {exc}"
            )
            raise ZhihuDetailFetchError(
                content.content_id,
                "detail_request_failed",
            ) from exc
        else:
            if detail is None:
                logger.warning(
                    "[ZhihuCrawler.enrich_search_content_detail] Detail parse failed "
                    f"for {content.content_type}:{content.content_id}"
                )
                raise ZhihuDetailFetchError(
                    content.content_id,
                    "detail_parse_failed",
                )
            else:
                merge_search_content_detail(content, detail)

        await asyncio.sleep(self.settings.CRAWLER_MAX_SLEEP_SEC)
        return content

    async def start(self) -> None:
        """
        Start the crawler
        Returns:

        """
        playwright_proxy_format, httpx_proxy_format = None, None
        async with self.ports.async_playwright() as playwright:
            # Choose launch mode based on configuration
            if self.settings.ENABLE_CDP_MODE:
                logger.info("[ZhihuCrawler] Launching browser in CDP mode")
                self.browser_context = await self.launch_browser_with_cdp(
                    playwright,
                    playwright_proxy_format,
                    self.user_agent,
                    headless=self.settings.CDP_HEADLESS,
                )
            else:
                logger.info("[ZhihuCrawler] Launching browser in standard mode")
                # Launch a browser context.
                chromium = playwright.chromium
                self.browser_context = await self.launch_browser(
                    chromium, None, self.user_agent, headless=self.settings.HEADLESS
                )
                # stealth.min.js is a js script to prevent the website from detecting the crawler.
                with resources.path("js/stealth.min.js") as stealth_path:
                    await self.browser_context.add_init_script(path=str(stealth_path))

            self.context_page = await self.browser_context.new_page()
            await self._close_stale_pages(self.context_page)
            try:
                await self.context_page.goto(
                    self.index_url,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
            except Exception as exc:
                logger.warning(
                    f"[ZhihuCrawler.start] Initial homepage navigation did not settle: {exc}"
                )
            if self.settings.LOGIN_TYPE == "cookie" and self.ports.initial_cookies():
                for key, value in convert_str_cookie_to_dict(self.ports.initial_cookies()).items():
                    await self.browser_context.add_cookies(
                        [{"name": key, "value": value, "domain": ".zhihu.com", "path": "/"}]
                    )
                try:
                    await self.context_page.reload(
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                except Exception as exc:
                    logger.warning(
                        f"[ZhihuCrawler.start] Cookie reload did not settle: {exc}"
                    )
            await self._wait_for_initial_login_settle()
            await self._close_stale_pages(self.context_page)

            # Create a client to interact with the zhihu website.
            self.zhihu_client = await self.create_zhihu_client(httpx_proxy_format)
            if not await self.zhihu_client.pong():
                login_obj = self.ports.login_factory(
                    login_type=self.settings.LOGIN_TYPE,
                    login_phone="",  # input your phone number
                    browser_context=self.browser_context,
                    context_page=self.context_page,
                    cookie_str=self.ports.initial_cookies(),
                )
                await login_obj.begin()
                await self.zhihu_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )

            search_cookie_keyword = self.settings.KEYWORDS.split(",", maxsplit=1)[0].strip()
            # Zhihu's search API requires opening the search page first to access cookies, homepage alone won't work
            logger.info(
                "[ZhihuCrawler.start] Zhihu navigating to search page to get search page cookies, this process takes about 5 seconds"
            )
            try:
                await self.context_page.goto(
                    f"{self.index_url}/search?q={quote(search_cookie_keyword)}&type=content",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
            except Exception as exc:
                logger.warning(
                    f"[ZhihuCrawler.start] Search-page navigation did not settle: {exc}"
                )
            await asyncio.sleep(5)
            await self.zhihu_client.update_cookies(
                browser_context=self.browser_context,
                urls=self.cookie_urls,
            )

            await self.ports.run_required_human_behavior(self.context_page, "zhihu")
            if self.settings.CRAWLER_TYPE == "search":
                # Search for notes and retrieve their comment information.
                await self.search()
            elif self.settings.CRAWLER_TYPE == "detail":
                # Get the information and comments of the specified post
                self.source_keyword = search_cookie_keyword
                await self.get_specified_notes()
            else:
                pass

            logger.info("[ZhihuCrawler.start] Zhihu Crawler finished ...")

    async def search(self) -> None:
        """Search for notes and retrieve their comment information."""
        logger.info("[ZhihuCrawler.search] Begin search zhihu keywords")
        start_page = self.settings.START_PAGE
        accumulator = self.ports.accumulator_factory("zhihu")
        for keyword in self.settings.KEYWORDS.split(","):
            self.source_keyword = keyword
            logger.info(
                f"[ZhihuCrawler.search] Current search keyword: {keyword}"
            )
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
                    try:
                        requested_page = page
                        logger.info(
                            f"[ZhihuCrawler.search] search zhihu keyword: {keyword}, "
                            f"page: {requested_page}"
                        )
                        content_list: List[ZhihuContent] = (
                            await self.zhihu_client.get_note_by_keyword(
                                keyword=keyword,
                                page=requested_page,
                            )
                        )
                        if not content_list:
                            logger.info("No more content!")
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

                        raw_batch_count = len(content_list)
                        pending_content_ids: set[str] = set()
                        unknown_contents: List[ZhihuContent] = []
                        for content in content_list:
                            content_id = str(content.content_id or "")
                            if accumulator.is_known(content_id) or (
                                content_id and content_id in pending_content_ids
                            ):
                                continue
                            if content_id:
                                pending_content_ids.add(content_id)
                            unknown_contents.append(content)
                        content_list = unknown_contents
                        await asyncio.sleep(self.settings.CRAWLER_MAX_SLEEP_SEC)
                        logger.info(
                            f"[ZhihuCrawler.search] Sleeping for {self.settings.CRAWLER_MAX_SLEEP_SEC} "
                            f"seconds after page {requested_page}"
                        )

                        page += 1
                        accumulator.begin_batch()
                        processed_count = 0
                        stored_contents: List[ZhihuContent] = []
                        for content in content_list:
                            content_id = str(content.content_id or "")
                            try:
                                content = await self.enrich_search_content_detail(content)
                            except ZhihuDetailFetchError as exc:
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
                                    "[ZhihuCrawler.search] Skip content after detail "
                                    f"attempts failed: {exc!r}"
                                )
                                processed_count += 1
                                should_stop = accumulator.skip_candidate_failure(
                                    content_id or str(exc.content_id or ""),
                                    failure_scope="post",
                                    detail="content_detail_failed",
                                    error_code=exc.code,
                                    attempts=exc.attempts,
                                    retryable=True,
                                    source_page=requested_page,
                                    discovery_phase=discovery_phase,
                                )
                                if should_stop:
                                    break
                                continue
                            image_ready = bool(
                                content.content_detail_status == "detail_observed"
                                and content.content_detail_source
                                in {
                                    "search_content",
                                    "answer_detail",
                                    "article_detail",
                                }
                                and zhihu_store.zhihu_content_image_assets(content)
                            )
                            valid = bool(
                                content.content_id
                                and content.content_type in {
                                    constant.ANSWER_NAME,
                                    constant.ARTICLE_NAME,
                                }
                                and content.content_text
                                and content.created_time
                                and content.creator_hash
                                and content.user_nickname
                                and image_ready
                                and content.followers_observed
                            )
                            if valid:
                                try:
                                    await self.get_content_images(content)
                                except ZhihuImageDownloadError as exc:
                                    if is_runtime_blocking_image_error(exc.code):
                                        accumulator.mark_runtime_failed(
                                            exc.code,
                                            source_page=requested_page,
                                            resume_page=requested_page,
                                            discovery_phase=discovery_phase,
                                        )
                                        processed_count += 1
                                        break
                                    should_stop = accumulator.skip_candidate_failure(
                                        str(content.content_id or ""),
                                        failure_scope="image",
                                        detail="image_download_failed",
                                        error_code=exc.code,
                                        attempts=exc.attempts,
                                        source_index=exc.source_index,
                                        source_page=requested_page,
                                        discovery_phase=discovery_phase,
                                    )
                                    processed_count += 1
                                    if should_stop:
                                        break
                                    continue
                            processed_count += 1
                            stored_contents.append(content)
                            await self._store_content(content)
                            should_stop = accumulator.consider(
                                str(content.content_id or ""),
                                valid=valid
                                and topic_relevant_for_web_post(
                                    "zhihu",
                                    content.model_dump(),
                                    fallback_keyword=keyword,
                                ),
                            )
                            if should_stop:
                                break

                        batch_complete = processed_count >= len(content_list)
                        if accumulator.finish_batch(
                            source_page=requested_page,
                            raw_batch_count=raw_batch_count,
                            resume_page=page if batch_complete else requested_page,
                            batch_complete=batch_complete,
                            discovery_phase=discovery_phase,
                            count_stagnation=discovery_phase == "frontier",
                        ):
                            break
                    except DataFetchError:
                        logger.error("[ZhihuCrawler.search] Search content error")
                        accumulator.mark_runtime_failed(
                            "search_request_failed",
                            source_page=page,
                            resume_page=page,
                            discovery_phase=discovery_phase,
                        )
                        return
            if source_exhausted and not accumulator.stop_reason:
                accumulator.mark_source_exhausted(
                    "saved_source_exhausted",
                    source_page=start_page,
                    resume_page=start_page,
                    source_has_more=False,
                    raw_batch_count=0,
                    discovery_phase="frontier",
                )




    async def get_note_detail(
        self, full_note_url: str, semaphore: asyncio.Semaphore
    ) -> Optional[ZhihuContent]:
        """
        Get note detail
        Args:
            full_note_url: str
            semaphore:

        Returns:

        """
        async with semaphore:
            logger.info(
                f"[ZhihuCrawler.get_specified_notes] Begin get specified note {full_note_url}"
            )
            # Judge note type
            note_type: str = judge_zhihu_url(full_note_url)
            if note_type == constant.ANSWER_NAME:
                question_id = full_note_url.split("/")[-3]
                answer_id = full_note_url.split("/")[-1]
                logger.info(
                    f"[ZhihuCrawler.get_specified_notes] Get answer info, question_id: {question_id}, answer_id: {answer_id}"
                )
                try:
                    result = await self.zhihu_client.get_answer_info(question_id, answer_id)
                except DataFetchError as exc:
                    logger.warning(
                        "[ZhihuCrawler.get_note_detail] Answer detail request failed "
                        f"for {answer_id}: {exc}"
                    )
                    return ZhihuContent(
                        content_id=answer_id,
                        question_id=question_id,
                        content_type=constant.ANSWER_NAME,
                        content_url=full_note_url,
                        content_detail_status="request_failed",
                    )

                # Sleep after fetching answer details
                await asyncio.sleep(self.settings.CRAWLER_MAX_SLEEP_SEC)
                logger.info(f"[ZhihuCrawler.get_note_detail] Sleeping for {self.settings.CRAWLER_MAX_SLEEP_SEC} seconds after fetching answer details {answer_id}")

                if (
                    result is None
                    or str(result.content_id or "") != str(answer_id)
                    or result.content_type != constant.ANSWER_NAME
                    or not str(result.content_text or "").strip()
                ):
                    logger.warning(
                        "[ZhihuCrawler.get_note_detail] Answer detail did not contain "
                        f"the requested non-empty entity: {answer_id}"
                    )
                    return ZhihuContent(
                        content_id=answer_id,
                        question_id=question_id,
                        content_type=constant.ANSWER_NAME,
                        content_url=full_note_url,
                        content_detail_status="parse_failed",
                    )
                result.content_detail_status = "detail_observed"
                result.content_detail_source = "answer_detail"
                return result

            elif note_type == constant.ARTICLE_NAME:
                article_id = full_note_url.split("/")[-1]
                logger.info(
                    f"[ZhihuCrawler.get_specified_notes] Get article info, article_id: {article_id}"
                )
                try:
                    result = await self.zhihu_client.get_article_info(article_id)
                except DataFetchError as exc:
                    logger.warning(
                        "[ZhihuCrawler.get_note_detail] Article detail request failed "
                        f"for {article_id}: {exc}"
                    )
                    return ZhihuContent(
                        content_id=article_id,
                        content_type=constant.ARTICLE_NAME,
                        content_url=full_note_url,
                        content_detail_status="request_failed",
                    )

                # Sleep after fetching article details
                await asyncio.sleep(self.settings.CRAWLER_MAX_SLEEP_SEC)
                logger.info(f"[ZhihuCrawler.get_note_detail] Sleeping for {self.settings.CRAWLER_MAX_SLEEP_SEC} seconds after fetching article details {article_id}")

                if (
                    result is None
                    or str(result.content_id or "") != str(article_id)
                    or result.content_type != constant.ARTICLE_NAME
                    or not str(result.content_text or "").strip()
                ):
                    logger.warning(
                        "[ZhihuCrawler.get_note_detail] Article detail did not contain "
                        f"the requested non-empty entity: {article_id}"
                    )
                    return ZhihuContent(
                        content_id=article_id,
                        content_type=constant.ARTICLE_NAME,
                        content_url=full_note_url,
                        content_detail_status="parse_failed",
                    )
                result.content_detail_status = "detail_observed"
                result.content_detail_source = "article_detail"
                return result

            elif note_type == constant.VIDEO_NAME:
                video_id = full_note_url.split("/")[-1]
                logger.info(
                    f"[ZhihuCrawler.get_specified_notes] Get video info, video_id: {video_id}"
                )
                result = await self.zhihu_client.get_video_info(video_id)

                # Sleep after fetching video details
                await asyncio.sleep(self.settings.CRAWLER_MAX_SLEEP_SEC)
                logger.info(f"[ZhihuCrawler.get_note_detail] Sleeping for {self.settings.CRAWLER_MAX_SLEEP_SEC} seconds after fetching video details {video_id}")

                return result

    async def get_specified_notes(self):
        """
        Get the information and comments of the specified post
        Returns:

        """
        get_note_detail_task_list = []
        semaphore = asyncio.Semaphore(self.settings.MAX_CONCURRENCY_NUM)
        for full_note_url in self.settings.ZHIHU_SPECIFIED_ID_LIST:
            # remove query params
            full_note_url = full_note_url.split("?")[0]
            crawler_task = self.get_note_detail(
                full_note_url=full_note_url,
                semaphore=semaphore,
            )
            get_note_detail_task_list.append(crawler_task)

        need_get_comment_notes: List[ZhihuContent] = []
        note_details = await asyncio.gather(*get_note_detail_task_list)
        for index, note_detail in enumerate(note_details):
            if not note_detail:
                logger.info(
                    f"[ZhihuCrawler.get_specified_notes] Note {self.settings.ZHIHU_SPECIFIED_ID_LIST[index]} not found"
                )
                continue

            note_detail = cast(ZhihuContent, note_detail)  # only for type check
            content_id = str(note_detail.content_id or "")
            image_assets = zhihu_store.zhihu_content_image_assets(note_detail)
            detail_ready = bool(
                note_detail.content_detail_status == "detail_observed"
                and note_detail.content_detail_source
                in {"answer_detail", "article_detail"}
                and str(note_detail.content_text or "").strip()
                and image_assets
            )
            if not detail_ready:
                logger.warning(
                    "[ZhihuCrawler.get_specified_notes] Skip incomplete detail candidate: "
                    f"content_id={content_id or '<missing>'}, "
                    f"detail_status={note_detail.content_detail_status or '<missing>'}, "
                    f"image_count={len(image_assets)}"
                )
                continue
            try:
                await self.get_content_images(note_detail)
            except ZhihuImageDownloadError as exc:
                if is_runtime_blocking_image_error(exc.code):
                    raise
                logger.warning(
                    "[ZhihuCrawler.get_specified_notes] Skip detail candidate after image "
                    f"failure: content_id={content_id}, code={exc.code}, "
                    f"source_index={exc.source_index}, attempts={exc.attempts}"
                )
                continue
            await self._store_content(note_detail)
            need_get_comment_notes.append(note_detail)


    async def get_content_images(self, content: ZhihuContent) -> None:
        """Download only observed answer/article body images."""

        if not self.settings.ENABLE_GET_MEIDAS:
            return
        content_id = str(content.content_id or "")
        image_assets = zhihu_store.zhihu_content_image_assets(content)
        if not image_assets:
            return
        fetched_assets: List[Dict] = []
        for asset in image_assets:
            source_index = int(asset["source_index"])
            try:
                payload, attempts = await self.ports.fetch_image_bytes_with_retry(
                    lambda: self.zhihu_client.get_content_image(
                        asset["url"], referer=content.content_url
                    ),
                    logger=logger,
                    label=(
                        f"platform=zhihu content_id={content_id} "
                        f"source_index={source_index}"
                    ),
                )
            except ImageDownloadFetchError as exc:
                await self.record_zhihu_content_image_failure(
                    content_id,
                    {
                        **asset,
                        "attempts": exc.attempts,
                        "http_status": exc.http_status,
                        "error_code": exc.code,
                    },
                )
                raise ZhihuImageDownloadError(
                    content_id, source_index, exc.code, exc.attempts
                ) from exc
            await asyncio.sleep(self.settings.CRAWLER_MAX_SLEEP_SEC)
            if payload is None:
                await self.record_zhihu_content_image_failure(
                    content_id,
                    {
                        **asset,
                        "attempts": attempts,
                        "http_status": None,
                        "error_code": "image_download_retryable",
                    },
                )
                raise ZhihuImageDownloadError(
                    content_id,
                    source_index,
                    "image_download_retryable",
                    attempts,
                )
            fetched_assets.append(
                {**asset, "content": payload, "attempts": attempts, "http_status": 200}
            )
        try:
            await self.update_zhihu_content_images(content_id, fetched_assets)
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
            await self.record_zhihu_content_image_failure(
                content_id,
                {
                    **failed_asset,
                    "attempts": int(failed_asset.get("attempts") or 1),
                    "http_status": 200,
                    "error_code": exc.code,
                },
            )
            raise ZhihuImageDownloadError(
                content_id,
                source_index,
                exc.code,
                int(failed_asset.get("attempts") or 1),
            ) from exc

    async def create_zhihu_client(self, httpx_proxy: Optional[str]) -> ZhiHuClient:
        """Create zhihu client"""
        logger.info(
            "[ZhihuCrawler.create_zhihu_client] Begin create zhihu API client ..."
        )
        cookie_str, cookie_dict = await self.ports.convert_browser_context_cookies(
            self.browser_context,
            urls=self.cookie_urls,
        )
        zhihu_client_obj = self.ports.client_factory(
            proxy=httpx_proxy,
            headers={
                "accept": "*/*",
                "accept-language": "zh-CN,zh;q=0.9",
                "cookie": cookie_str,
                "priority": "u=1, i",
                "referer": "https://www.zhihu.com/search?q=python&time_interval=a_year&type=content",
                "user-agent": self.user_agent,
                "x-api-version": "3.0.91",
                "x-app-za": "OS=Web",
                "x-requested-with": "fetch",
                "x-zse-93": "101_3_3.0",
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
        )
        return zhihu_client_obj

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        logger.info(
            "[ZhihuCrawler.launch_browser] Begin create browser context ..."
        )
        if self.settings.SAVE_LOGIN_STATE:
            # feat issue #14
            # we will save login state to avoid login every time
            user_data_dir = os.path.join(
                MEDIACRAWLER_DIR, "browser_data", self.settings.USER_DATA_DIR % self.settings.PLATFORM
            )  # type: ignore
            browser_context = await chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=headless,
                proxy=playwright_proxy,  # type: ignore
                viewport={"width": 1920, "height": 1080},
                user_agent=user_agent,
                args=self.ports.project_browser_args(),
            )
            return browser_context
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy)  # type: ignore
            browser_context = await browser.new_context(
                viewport={"width": 1920, "height": 1080}, user_agent=user_agent
            )
            return browser_context

    async def launch_browser_with_cdp(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """
        Launch browser using CDP mode
        """
        try:
            self.cdp_manager = self.ports.browser_manager_factory()
            browser_context = await self.cdp_manager.launch_and_connect(
                playwright=playwright,
                playwright_proxy=playwright_proxy,
                user_agent=user_agent,
                headless=headless,
            )

            # Display browser information
            browser_info = await self.cdp_manager.get_browser_info()
            logger.info(f"[ZhihuCrawler] CDP browser info: {browser_info}")

            return browser_context

        except Exception as e:
            logger.error(f"[ZhihuCrawler] CDP mode launch failed, falling back to standard mode: {e}")
            # Fall back to standard mode
            chromium = playwright.chromium
            return await self.launch_browser(
                chromium, playwright_proxy, user_agent, headless
            )

    async def close(self):
        """Close browser context"""
        # Special handling if using CDP mode
        if self.cdp_manager:
            await self.cdp_manager.cleanup()
            self.cdp_manager = None
        else:
            await self.browser_context.close()
        logger.info("[ZhihuCrawler.close] Browser context closed ...")

    async def update_zhihu_content_images(self, content_id: str, image_content_items: List[dict]):
        """使用本实例的图片暂存端口。"""
        return await update_zhihu_content_images(
            content_id, image_content_items, image_stager_factory=self.ports.image_stager_factory,
        )


    async def record_zhihu_content_image_failure(self, content_id: str, image_content_item: dict):
        """使用本实例的失败证据端口。"""
        return await record_zhihu_content_image_failure(
            content_id, image_content_item, image_stager_factory=self.ports.image_stager_factory,
        )

    async def _store_content(self, content_item: ZhihuContent) -> None:
        """使用本实例的关键词、时钟和内容出口。"""
        await store_zhihu_content(
            content_item, source_keyword=lambda: self.source_keyword,
            current_timestamp=self.ports.current_timestamp,
            content_sink_factory=self.ports.content_sink_factory,
        )


async def update_zhihu_content_images(content_id: str, image_content_items: List[dict], *, image_stager_factory):
    """在原写出时点构造暂存器，原子保存整帖正文图片。"""
    return await image_stager_factory().store_post_images(content_id, image_content_items)


async def record_zhihu_content_image_failure(content_id: str, image_content_item: dict, *, image_stager_factory):
    """在原写出时点构造暂存器，保存失败图片证据。"""
    return await image_stager_factory().record_failure(content_id, image_content_item)


async def store_zhihu_content(content_item: ZhihuContent, *, source_keyword, current_timestamp, content_sink_factory):
    """原 store 的 IO 尾部；纯投影后读取时钟，再按原时点创建内容出口。"""
    local_db_item = zhihu_store.update_zhihu_content(
        content_item, source_keyword=source_keyword(),
    )
    local_db_item.update({"last_modify_ts": current_timestamp()})
    logger.info(f"[store.zhihu.update_zhihu_content] zhihu content: {local_db_item}")
    await content_sink_factory().store_content(local_db_item)
