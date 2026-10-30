# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/douyin/core.py
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
# TripPostCollect：T06 按职责迁入；来源 fork 5a68eb5098fcd17308c7fe0b9d53916ae839b303，原许可保留。

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from playwright.async_api import BrowserContext, BrowserType, Page, Playwright
from trippostcollect.application.contracts import DouyinSettings, DouyinReaders, DouyinCrawlerPorts, ImageStagingError
from trippostcollect.core import resources
from trippostcollect.core.paths import platform_profile_dir
from trippostcollect.records.topic_relevance import topic_relevant_for_web_post
from trippostcollect.runtime.image_retry import ImageDownloadFetchError, is_runtime_blocking_image_error
from trippostcollect.platforms.douyin import parser
from trippostcollect.platforms.douyin.client import DouYinClient, inspect_empty_first_page
from trippostcollect.platforms.douyin.models import (
    DataFetchError, PlatformRuntimeError, SearchResponseError, DouyinImageDownloadError,
)
from trippostcollect.platforms.douyin.parser import parse_video_info_from_url
from trippostcollect.platforms.douyin.login import DouYinLogin

logger = logging.getLogger("MediaCrawler")

class DouYinCrawler:
    context_page: Page
    dy_client: DouYinClient
    browser_context: BrowserContext
    cdp_manager: Any

    def __init__(self, *, settings: DouyinSettings, inputs: DouyinReaders, ports: DouyinCrawlerPorts) -> None:
        self.settings = settings
        self.inputs = inputs
        self.ports = ports
        self.source_keyword = ""
        self.crawler_type = ""
        self.index_url = "https://www.douyin.com"
        self.cookie_urls = [
            "https://douyin.com",
            self.index_url,
            "https://creator.douyin.com",
            "https://douhot.douyin.com",
            "https://live.douyin.com",
        ]
        self.cdp_manager = None
        self.ip_proxy_pool = None  # Proxy IP pool for automatic proxy refresh
        self.creator_profile_cache: Dict[str, Dict] = {}
        self.creator_profile_failure_cache: Dict[str, Dict[str, Any]] = {}
        self.creator_profile_enriched_count = 0

    async def start(self) -> None:
        playwright_proxy_format, httpx_proxy_format = None, None
        async with self.ports.async_playwright() as playwright:
            # Select startup mode based on configuration
            if self.settings.ENABLE_CDP_MODE:
                logger.info("[DouYinCrawler] 使用CDP模式启动浏览器")
                self.browser_context = await self.launch_browser_with_cdp(
                    playwright,
                    playwright_proxy_format,
                    None,
                    headless=self.settings.CDP_HEADLESS,
                )
            else:
                logger.info("[DouYinCrawler] 使用标准模式启动浏览器")
                # Launch a browser context.
                chromium = playwright.chromium
                self.browser_context = await self.launch_browser(
                    chromium,
                    playwright_proxy_format,
                    user_agent=None,
                    headless=self.settings.HEADLESS,
                )
                # stealth.min.js is a js script to prevent the website from detecting the crawler.
                with resources.path("js/stealth.min.js") as stealth_path:
                    await self.browser_context.add_init_script(path=str(stealth_path))

            self.context_page = await self.browser_context.new_page()
            await self.context_page.goto(self.index_url, wait_until="domcontentloaded")

            self.dy_client = await self.create_douyin_client(httpx_proxy_format)
            if not await self.dy_client.pong(browser_context=self.browser_context):
                login_obj = DouYinLogin(
                    login_type=self.settings.LOGIN_TYPE,
                    login_phone="",  # you phone number
                    browser_context=self.browser_context,
                    context_page=self.context_page,
                    cookie_str=self.settings.COOKIES,
                    ports=self.ports.login,
                )
                await login_obj.begin()
                await self.dy_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )
            self.crawler_type = self.settings.CRAWLER_TYPE
            if self.settings.CRAWLER_TYPE == "search":
                # Search for notes and retrieve their comment information.
                behavior_keyword = self.settings.KEYWORDS.split(",", maxsplit=1)[0].strip()
                search_url = f"{self.index_url}/search/{quote(behavior_keyword)}?type=general"
                await self.context_page.goto(search_url, wait_until="domcontentloaded")
                await self.ports.run_required_human_behavior(self.context_page, "douyin")
                await self.dy_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )
                await self.search()
            elif self.settings.CRAWLER_TYPE == "detail":
                # Get the information and comments of the specified post
                behavior_keyword = self.settings.KEYWORDS.split(",", maxsplit=1)[0].strip()
                self.source_keyword = behavior_keyword
                search_url = f"{self.index_url}/search/{quote(behavior_keyword)}?type=general"
                await self.context_page.goto(search_url, wait_until="domcontentloaded")
                await self.ports.run_required_human_behavior(self.context_page, "douyin")
                await self.dy_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )
                await self.get_specified_awemes()
            logger.info("[DouYinCrawler.start] Douyin Crawler finished ...")

    async def search(self) -> None:
        """#124：每轮从第 1 页（offset 0、空 search ID）连续翻到平台 has_more=false。

        不跨进程跳到深层 page/offset/search ID，也没有顶部刷新、前沿重绑定或耗尽后 reseed；
        已处理 ID 在作者、详情与媒体请求前由累积器过滤。checkpoint 只记录本轮最后位置供审计。
        """
        logger.info("[DouYinCrawler.search] Begin search douyin keywords")
        dy_limit_count = 10  # Must match the search API count parameter.
        accumulator = self.ports.candidates()
        discovery_phase = "frontier"
        for keyword in self.settings.KEYWORDS.split(","):
            self.source_keyword = keyword
            logger.info(f"[DouYinCrawler.search] Current keyword: {keyword}")
            aweme_list: List[str] = []
            page = 1
            next_offset = 0
            dy_search_id = ""
            source_has_more = None
            next_search_id = ""
            resume_page = page
            resume_offset = next_offset
            while accumulator.can_continue:
                requested_page = page
                requested_offset = next_offset
                requested_search_id = dy_search_id
                try:
                    logger.info(
                        f"[DouYinCrawler.search] search douyin keyword: {keyword}, "
                        f"page: {requested_page}, offset: {requested_offset}"
                    )
                    posts_res = await self.dy_client.search_info_by_keyword(
                        keyword=keyword,
                        offset=requested_offset,
                        search_id=requested_search_id,
                    )
                except SearchResponseError as exc:
                    logger.error(
                        f"[DouYinCrawler.search] invalid search response for "
                        f"keyword: {keyword}, reason: {exc.reason}"
                    )
                    accumulator.mark_runtime_failed(
                        exc.reason,
                        source_page=requested_page,
                        source_offset=requested_offset,
                        source_cursor=requested_search_id,
                        resume_page=requested_page,
                        resume_offset=requested_offset,
                        resume_cursor=requested_search_id,
                        discovery_phase=discovery_phase,
                    )
                    break
                except PlatformRuntimeError as exc:
                    logger.error(
                        f"[DouYinCrawler.search] platform runtime failure for "
                        f"keyword: {keyword}, code: {exc.code}"
                    )
                    accumulator.mark_runtime_failed(
                        exc.code,
                        source_page=requested_page,
                        source_offset=requested_offset,
                        source_cursor=requested_search_id,
                        resume_page=requested_page,
                        resume_offset=requested_offset,
                        resume_cursor=requested_search_id,
                        discovery_phase=discovery_phase,
                    )
                    break
                except DataFetchError:
                    logger.error(
                        f"[DouYinCrawler.search] search douyin keyword: {keyword} failed"
                    )
                    accumulator.mark_runtime_failed(
                        "search_request_failed",
                        source_page=requested_page,
                        source_offset=requested_offset,
                        source_cursor=requested_search_id,
                        resume_page=requested_page,
                        resume_offset=requested_offset,
                        resume_cursor=requested_search_id,
                        discovery_phase=discovery_phase,
                    )
                    break
                if "data" not in posts_res or posts_res.get("data") is None:
                    accumulator.mark_runtime_failed(
                        "missing_data_field",
                        source_page=requested_page,
                        source_offset=requested_offset,
                        source_cursor=requested_search_id,
                        resume_page=requested_page,
                        resume_offset=requested_offset,
                        resume_cursor=requested_search_id,
                        discovery_phase=discovery_phase,
                    )
                    break

                post_items = posts_res.get("data") or []
                source_has_more = (
                    posts_res.get("has_more") if "has_more" in posts_res else None
                )
                # 响应可带 extra: null（has_more=0 时校验允许）；不能对 None 取 logid。
                response_logid = (posts_res.get("extra") or {}).get("logid", "")
                next_search_id = requested_search_id or response_logid
                resume_page = requested_page + 1
                resume_offset = requested_offset + dy_limit_count
                fresh_first_page = bool(
                    requested_page == 1
                    and requested_offset == 0
                    and not requested_search_id
                )
                self.ports.append_execution_event(
                    "douyin_search_response_observed",
                    {
                        "platform": "douyin",
                        "discovery_phase": discovery_phase,
                        "source_page": requested_page,
                        "source_offset": requested_offset,
                        "source_cursor_present": bool(requested_search_id),
                        "fresh_first_page": fresh_first_page,
                        "status_code": posts_res.get("status_code"),
                        "data_count": len(post_items),
                        "has_more": source_has_more,
                        "next_search_id_present": bool(next_search_id),
                        "cursor_source": (
                            "request_search_id"
                            if requested_search_id
                            else "response_logid"
                        ),
                        "response_logid_matches_cursor": bool(
                            response_logid and response_logid == next_search_id
                        ),
                    },
                )
                accumulator.begin_batch()
                if not post_items:
                    if source_has_more in (True, 1) and next_search_id:
                        page = resume_page
                        next_offset = resume_offset
                        dy_search_id = next_search_id
                        if accumulator.finish_batch(
                            source_page=requested_page,
                            source_offset=requested_offset,
                            source_cursor=requested_search_id,
                            next_cursor=next_search_id,
                            source_has_more=source_has_more,
                            raw_batch_count=0,
                            resume_page=resume_page,
                            resume_offset=resume_offset,
                            resume_cursor=next_search_id,
                            batch_complete=True,
                            discovery_phase=discovery_phase,
                        ):
                            break
                        await asyncio.sleep(self.settings.CRAWLER_MAX_SLEEP_SEC)
                        continue
                    if fresh_first_page:
                        page_check = await inspect_empty_first_page(self.context_page)
                        self.ports.append_execution_event(
                            "douyin_empty_first_page_checked",
                            {
                                "platform": "douyin",
                                "discovery_phase": discovery_phase,
                                "source_page": requested_page,
                                "source_offset": requested_offset,
                                "source_cursor_present": False,
                                "api_has_more": source_has_more,
                                "next_search_id_present": bool(next_search_id),
                                **page_check,
                            },
                        )
                        if page_check["classification"] != "explicit_no_results":
                            failure_detail = (
                                "empty_api_response_with_visible_results"
                                if page_check["classification"] == "visible_results"
                                else "ambiguous_empty_first_page"
                            )
                            accumulator.mark_runtime_failed(
                                failure_detail,
                                source_page=requested_page,
                                source_offset=requested_offset,
                                source_cursor=requested_search_id,
                                resume_page=requested_page,
                                resume_offset=requested_offset,
                                resume_cursor=requested_search_id,
                                discovery_phase=discovery_phase,
                            )
                            break
                    empty_detail = (
                        "verified_empty_first_page"
                        if fresh_first_page
                        else "empty_page"
                    )
                    accumulator.mark_source_exhausted(
                        empty_detail,
                        source_page=requested_page,
                        source_offset=requested_offset,
                        source_cursor=requested_search_id,
                        next_cursor=next_search_id,
                        source_has_more=False,
                        raw_batch_count=0,
                        resume_page=requested_page,
                        resume_offset=requested_offset,
                        resume_cursor=requested_search_id,
                        discovery_phase=discovery_phase,
                    )
                    break

                raw_batch_count = len(post_items)
                page_aweme_list: List[str] = []
                processed_count = 0
                for post_item in post_items:
                    try:
                        aweme_info: Dict = (
                            post_item.get("aweme_info")
                            or post_item.get("aweme_mix_info", {}).get("mix_items")[0]
                        )
                    except (AttributeError, IndexError, TypeError):
                        processed_count += 1
                        if accumulator.consider("", valid=False):
                            break
                        continue
                    aweme_id = str(aweme_info.get("aweme_id") or "")
                    if accumulator.is_known(aweme_id):
                        processed_count += 1
                        continue
                    if not parser.is_douyin_image_note(aweme_info):
                        # #116：视频只计入已处理候选，不请求作者资料，也不写内容记录。
                        processed_count += 1
                        if accumulator.consider(aweme_id, valid=False):
                            break
                        continue
                    # #124：搜索项只来自浏览器截获的搜索卡片（首页重建随直接请求删除）。
                    content_detail_source = parser.DOUYIN_SEARCH_CONTENT_SOURCE
                    aweme_info = await self.enrich_aweme_creator(aweme_info)
                    if aweme_info.get("creator_profile_runtime_error"):
                        accumulator.mark_runtime_failed(
                            str(aweme_info["creator_profile_runtime_error"]),
                            source_page=requested_page,
                            source_offset=requested_offset,
                            source_cursor=requested_search_id,
                            resume_page=requested_page,
                            resume_offset=requested_offset,
                            resume_cursor=requested_search_id,
                            discovery_phase=discovery_phase,
                        )
                        processed_count += 1
                        break
                    if aweme_info.get("creator_profile_error"):
                        should_stop = accumulator.skip_candidate_failure(
                            aweme_id,
                            failure_scope="post",
                            detail="creator_profile_failed",
                            error_code=str(aweme_info["creator_profile_error"]),
                            attempts=int(
                                aweme_info.get("creator_profile_attempts") or 1
                            ),
                            retryable=True,
                            source_page=requested_page,
                            source_offset=requested_offset,
                            source_cursor=requested_search_id,
                            discovery_phase=discovery_phase,
                        )
                        processed_count += 1
                        if should_stop:
                            break
                        continue
                    author = aweme_info.get("author") or {}
                    creator_profile = aweme_info.get("creator_profile") or {}
                    author_stats = parser._normalized_author_stats(
                        author,
                        creator_profile,
                    )
                    statistics = aweme_info.get("statistics") or {}
                    # 与导入门禁对齐：正文证据、正文图和作者资料粉丝缺一项都不下载图片。
                    valid = bool(
                        parser.douyin_content_detail_observed(aweme_info, aweme_id)
                        and author.get("nickname")
                        and parser._extract_note_image_list(aweme_info)
                        and author_stats.get("followers_observed")
                        and author_stats.get("author_followers_source") == "creator_profile"
                        and all(
                            statistics.get(key) not in (None, "")
                            for key in (
                                "digg_count",
                                "collect_count",
                                "comment_count",
                                "share_count",
                            )
                        )
                    )
                    if valid:
                        try:
                            await self.get_aweme_images(aweme_item=aweme_info)
                        except DouyinImageDownloadError as exc:
                            logger.error(
                                "[DouYinCrawler.search] Image materialization failed: "
                                f"{exc!r}"
                            )
                            if is_runtime_blocking_image_error(exc.code):
                                accumulator.mark_runtime_failed(
                                    exc.code,
                                    source_page=requested_page,
                                    source_offset=requested_offset,
                                    source_cursor=requested_search_id,
                                    resume_page=requested_page,
                                    resume_offset=requested_offset,
                                    resume_cursor=requested_search_id,
                                    discovery_phase=discovery_phase,
                                )
                                processed_count += 1
                                break
                            should_stop = accumulator.skip_candidate_failure(
                                aweme_id,
                                failure_scope="image",
                                detail="image_download_failed",
                                error_code=exc.code,
                                attempts=exc.attempts,
                                source_index=exc.source_index,
                                source_page=requested_page,
                                source_offset=requested_offset,
                                source_cursor=requested_search_id,
                                discovery_phase=discovery_phase,
                            )
                            processed_count += 1
                            if should_stop:
                                break
                            continue
                    await self.update_douyin_aweme(
                        aweme_item=aweme_info,
                        content_detail_source=content_detail_source,
                        expected_aweme_id=aweme_id,
                    )
                    processed_count += 1
                    aweme_list.append(aweme_id)
                    page_aweme_list.append(aweme_id)
                    target_valid = valid and topic_relevant_for_web_post(
                        "douyin",
                        aweme_info,
                        fallback_keyword=keyword,
                    )
                    should_stop = accumulator.consider(aweme_id, valid=target_valid)
                    if should_stop:
                        break

                logger.info("[DouYinCrawler.batch_get_note_comments] Crawling comment mode is not enabled")
                batch_complete = processed_count >= raw_batch_count
                if not batch_complete:
                    resume_page = requested_page
                    resume_offset = requested_offset
                    next_search_id = requested_search_id
                if accumulator.finish_batch(
                    source_page=requested_page,
                    source_offset=requested_offset,
                    source_cursor=requested_search_id,
                    next_cursor=next_search_id,
                    source_has_more=source_has_more,
                    raw_batch_count=raw_batch_count,
                    resume_page=resume_page,
                    resume_offset=resume_offset,
                    resume_cursor=next_search_id,
                    batch_complete=batch_complete,
                    discovery_phase=discovery_phase,
                ):
                    break
                if source_has_more in (False, 0):
                    accumulator.mark_source_exhausted(
                        "has_more_false",
                        source_page=requested_page,
                        source_offset=requested_offset,
                        source_cursor=requested_search_id,
                        next_cursor=next_search_id,
                        source_has_more=False,
                        raw_batch_count=raw_batch_count,
                        resume_page=resume_page,
                        resume_offset=resume_offset,
                        resume_cursor=next_search_id,
                        batch_complete=batch_complete,
                        discovery_phase=discovery_phase,
                    )
                    break

                page = requested_page + 1
                next_offset = requested_offset + dy_limit_count
                dy_search_id = next_search_id
                await asyncio.sleep(self.settings.CRAWLER_MAX_SLEEP_SEC)
                logger.info(
                    f"[DouYinCrawler.search] Sleeping for {self.settings.CRAWLER_MAX_SLEEP_SEC} "
                    f"seconds after page {requested_page}"
                )
            logger.info(f"[DouYinCrawler.search] keyword:{keyword}, aweme_list:{aweme_list}")

    async def enrich_aweme_creator(self, aweme_info: Dict) -> Dict:
        if self.inputs.enrich_creators() != "1":
            return aweme_info
        author = aweme_info.get("author") or {}
        if self.inputs.enrich_only_images() == "1":
            if not parser._extract_note_image_list(aweme_info) and str(aweme_info.get("aweme_type")) not in {"68"}:
                return aweme_info
        sec_uid = author.get("sec_uid") or author.get("sec_user_id")
        if not sec_uid:
            return aweme_info
        if sec_uid in self.creator_profile_cache:
            creator_profile = self.creator_profile_cache[sec_uid]
            if creator_profile:
                aweme_info["creator_profile"] = creator_profile
            return aweme_info
        if sec_uid in self.creator_profile_failure_cache:
            cached_failure = self.creator_profile_failure_cache[sec_uid]
            aweme_info["creator_profile_attempts"] = int(
                cached_failure.get("attempts") or 1
            )
            if cached_failure.get("runtime_error"):
                aweme_info["creator_profile_runtime_error"] = str(
                    cached_failure["runtime_error"]
                )
            else:
                aweme_info["creator_profile_error"] = str(
                    cached_failure.get("error") or "detail_request_failed"
                )
            return aweme_info
        max_enrich = int(self.inputs.max_creator_enrich())
        if max_enrich >= 0 and self.creator_profile_enriched_count >= max_enrich:
            return aweme_info
        creator_profile = None
        last_error: DataFetchError | None = None
        attempts = 0
        for attempt in range(1, 4):
            attempts = attempt
            try:
                creator_profile = await self.dy_client.get_user_info(sec_uid)
                last_error = None
                if creator_profile:
                    break
            except PlatformRuntimeError as exc:
                # 登录失效、网关拦截与限流是运行级阻断：不重试，不当作单条候选失败。
                logger.error(
                    "[DouYinCrawler.enrich_aweme_creator] creator profile blocked, "
                    f"code: {exc.code}, reason: {exc}"
                )
                aweme_info["creator_profile_attempts"] = attempts
                aweme_info["creator_profile_runtime_error"] = exc.code
                self.creator_profile_failure_cache[sec_uid] = {
                    "runtime_error": exc.code,
                    "attempts": attempts,
                }
                return aweme_info
            except DataFetchError as exc:
                last_error = exc
                if "account blocked" in str(exc).lower():
                    break
            if attempt < 3:
                await asyncio.sleep(attempt)
        aweme_info["creator_profile_attempts"] = attempts
        try:
            if last_error and "account blocked" in str(last_error).lower():
                aweme_info["creator_profile_runtime_error"] = "account_blocked"
                self.creator_profile_failure_cache[sec_uid] = {
                    "runtime_error": "account_blocked",
                    "attempts": attempts,
                }
                return aweme_info
            self.creator_profile_enriched_count += 1
            if creator_profile:
                self.creator_profile_cache[sec_uid] = creator_profile
                aweme_info["creator_profile"] = creator_profile
            elif last_error is not None:
                aweme_info["creator_profile_error"] = "detail_request_failed"
            else:
                aweme_info["creator_profile_error"] = "creator_profile_empty"
            if not creator_profile:
                self.creator_profile_failure_cache[sec_uid] = {
                    "error": aweme_info["creator_profile_error"],
                    "attempts": attempts,
                }
            sleep_seconds = float(self.inputs.creator_sleep_seconds())
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
        except DataFetchError as exc:
            aweme_info["creator_profile_error"] = "detail_request_failed"
            self.creator_profile_failure_cache[sec_uid] = {
                "error": "detail_request_failed",
                "attempts": attempts,
            }
            logger.warning(f"[DouYinCrawler.enrich_aweme_creator] get creator profile failed: {exc}")
        return aweme_info

    async def get_specified_awemes(self):
        """Get the information and comments of the specified post from URLs or IDs"""
        logger.info("[DouYinCrawler.get_specified_awemes] Parsing video URLs...")
        aweme_id_list = []
        for video_url in self.settings.DY_SPECIFIED_ID_LIST:
            try:
                video_info = parse_video_info_from_url(video_url)

                # Handling short links
                if video_info.url_type == "short":
                    logger.info(f"[DouYinCrawler.get_specified_awemes] Resolving short link: {video_url}")
                    resolved_url = await self.dy_client.resolve_short_url(video_url)
                    if resolved_url:
                        # Extract video ID from parsed URL
                        video_info = parse_video_info_from_url(resolved_url)
                        logger.info(f"[DouYinCrawler.get_specified_awemes] Short link resolved to aweme ID: {video_info.aweme_id}")
                    else:
                        logger.error(f"[DouYinCrawler.get_specified_awemes] Failed to resolve short link: {video_url}")
                        continue

                aweme_id_list.append(video_info.aweme_id)
                logger.info(f"[DouYinCrawler.get_specified_awemes] Parsed aweme ID: {video_info.aweme_id} from {video_url}")
            except ValueError as e:
                logger.error(f"[DouYinCrawler.get_specified_awemes] Failed to parse video URL: {e}")
                continue

        semaphore = asyncio.Semaphore(self.settings.MAX_CONCURRENCY_NUM)
        task_list = [self.get_aweme_detail(aweme_id=aweme_id, semaphore=semaphore) for aweme_id in aweme_id_list]
        aweme_details = await asyncio.gather(*task_list)
        for aweme_id, aweme_detail in zip(aweme_id_list, aweme_details):
            if not isinstance(aweme_detail, dict):
                continue
            if (
                str(aweme_detail.get("aweme_id") or "") != str(aweme_id)
                or not parser.is_douyin_image_note(aweme_detail)
            ):
                # #116：ID 不一致的空壳和视频都不写内容记录。
                logger.warning(
                    "[DouYinCrawler.get_specified_awemes] skip non-image or mismatched detail, "
                    f"aweme_id: {aweme_id}"
                )
                continue
            aweme_detail = await self.enrich_aweme_creator(aweme_detail)
            if aweme_detail.get("creator_profile_runtime_error"):
                code = str(aweme_detail["creator_profile_runtime_error"])
                raise PlatformRuntimeError(f"douyin creator profile {code}", code=code)
            await self.update_douyin_aweme(
                aweme_item=aweme_detail,
                content_detail_source=parser.DOUYIN_DETAIL_CONTENT_SOURCE,
                expected_aweme_id=aweme_id,
            )
            author_stats = parser._normalized_author_stats(
                aweme_detail.get("author") or {},
                aweme_detail.get("creator_profile") or {},
            )
            if author_stats.get("author_followers_source") != "creator_profile":
                # 与搜索路径一致：没有作者资料粉丝的记录导入必然被拒，不下载正文图。
                logger.warning(
                    "[DouYinCrawler.get_specified_awemes] skip images without creator profile "
                    f"followers, aweme_id: {aweme_id}"
                )
                continue
            await self.get_aweme_media(aweme_item=aweme_detail)
        logger.info("[DouYinCrawler.batch_get_note_comments] Crawling comment mode is not enabled")

    async def get_aweme_detail(self, aweme_id: str, semaphore: asyncio.Semaphore) -> Any:
        """Get note detail"""
        async with semaphore:
            try:
                result = await self.dy_client.get_video_by_id(aweme_id)
                # Sleep after fetching aweme detail
                await asyncio.sleep(self.settings.CRAWLER_MAX_SLEEP_SEC)
                logger.info(f"[DouYinCrawler.get_aweme_detail] Sleeping for {self.settings.CRAWLER_MAX_SLEEP_SEC} seconds after fetching aweme {aweme_id}")
                return result
            except PlatformRuntimeError:
                raise
            except DataFetchError as ex:
                logger.error(f"[DouYinCrawler.get_aweme_detail] Get aweme detail error: {ex}")
                return None
            except KeyError as ex:
                logger.error(f"[DouYinCrawler.get_aweme_detail] have not fund note detail aweme_id:{aweme_id}, err: {ex}")
                return None
            except Exception as ex:
                logger.warning(f"[DouYinCrawler.get_aweme_detail] Skip aweme after request error aweme_id:{aweme_id}, err: {ex}")
                return None





    async def create_douyin_client(self, httpx_proxy: Optional[str]) -> DouYinClient:
        """Create douyin client"""
        cookie_str, cookie_dict = await self.ports.client.convert_browser_context_cookies(
            self.browser_context,
            urls=self.cookie_urls,
        )  # type: ignore
        douyin_client = DouYinClient(
            proxy=httpx_proxy,
            headers={
                "User-Agent": await self.context_page.evaluate("() => navigator.userAgent"),
                "Cookie": cookie_str,
                "Host": "www.douyin.com",
                "Origin": "https://www.douyin.com/",
                "Referer": "https://www.douyin.com/",
                "Content-Type": "application/json;charset=UTF-8",
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
            ports=self.ports.client,
            browser_detail_fallback=self.ports.browser_detail_fallback,
            browser_detail_timeout=self.inputs.browser_detail_timeout,
        )
        return douyin_client

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        if self.settings.SAVE_LOGIN_STATE:
            user_data_dir = str(platform_profile_dir("douyin"))
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
            )  # type: ignore
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
        使用CDP模式启动浏览器
        """
        try:
            self.cdp_manager = self.ports.cdp_manager()
            browser_context = await self.cdp_manager.launch_and_connect(
                playwright=playwright,
                playwright_proxy=playwright_proxy,
                user_agent=user_agent,
                headless=headless,
            )

            # Add anti-detection script
            await self.cdp_manager.add_stealth_script()

            # Show browser information
            browser_info = await self.cdp_manager.get_browser_info()
            logger.info(f"[DouYinCrawler] CDP浏览器信息: {browser_info}")

            return browser_context

        except Exception as e:
            logger.error(f"[DouYinCrawler] CDP模式启动失败，回退到标准模式: {e}")
            # Fall back to standard mode
            chromium = playwright.chromium
            return await self.launch_browser(chromium, playwright_proxy, user_agent, headless)

    async def close(self) -> None:
        """Close browser context"""
        # If you use CDP mode, special processing is required
        if self.cdp_manager:
            await self.cdp_manager.cleanup()
            self.cdp_manager = None
        else:
            await self.browser_context.close()
        logger.info("[DouYinCrawler.close] Browser context closed ...")

    async def get_aweme_media(self, aweme_item: Dict):
        """
        Compatibility entrypoint for strict note-image-only downloads.

        Args:
            aweme_item (Dict): 抖音作品详情
        """
        if not self.settings.ENABLE_GET_MEIDAS:
            logger.info("[DouYinCrawler.get_aweme_media] Crawling image mode is not enabled")
            return
        await self.get_aweme_images(aweme_item)

    async def get_aweme_images(self, aweme_item: Dict):
        """
        get aweme images. please use get_aweme_media

        Args:
            aweme_item (Dict): 抖音作品详情
        """
        if not self.settings.ENABLE_GET_MEIDAS:
            return
        aweme_id = str(aweme_item.get("aweme_id") or "")
        image_assets = parser._extract_note_image_assets(aweme_item)
        if not image_assets:
            return
        fetched_assets: List[Dict] = []
        for asset in image_assets:
            source_index = int(asset["source_index"])
            try:
                content, attempts = await self.ports.fetch_image_bytes_with_retry(
                    lambda: self.dy_client.get_aweme_media(asset["url"]),
                    logger=logger,
                    label=(
                        f"platform=douyin aweme_id={aweme_id} "
                        f"source_index={source_index}"
                    ),
                )
            except ImageDownloadFetchError as exc:
                await self.record_dy_aweme_image_failure(
                    aweme_id,
                    {
                        **asset,
                        "attempts": exc.attempts,
                        "http_status": exc.http_status,
                        "error_code": exc.code,
                    },
                )
                raise DouyinImageDownloadError(
                    aweme_id, source_index, exc.code, exc.attempts
                ) from exc
            await asyncio.sleep(random.random())
            if content is None:
                await self.record_dy_aweme_image_failure(
                    aweme_id,
                    {
                        **asset,
                        "attempts": attempts,
                        "http_status": None,
                        "error_code": "image_download_retryable",
                    },
                )
                raise DouyinImageDownloadError(
                    aweme_id,
                    source_index,
                    "image_download_retryable",
                    attempts,
                )
            fetched_assets.append(
                {**asset, "content": content, "attempts": attempts, "http_status": 200}
            )
        try:
            await self.update_dy_aweme_images(aweme_id, fetched_assets)
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
            await self.record_dy_aweme_image_failure(
                aweme_id,
                {
                    **failed_asset,
                    "attempts": int(failed_asset.get("attempts") or 1),
                    "http_status": 200,
                    "error_code": exc.code,
                },
            )
            raise DouyinImageDownloadError(
                aweme_id,
                source_index,
                exc.code,
                int(failed_asset.get("attempts") or 1),
            ) from exc



    @classmethod
    def bind(cls, dependencies):
        """入口绑定装配工厂；每次构造时取得已解析的配置快照。"""
        class DouYinCrawler(cls):
            def __init__(self):
                super().__init__(**dependencies())

        return DouYinCrawler

    async def update_douyin_aweme(
        self,
        aweme_item: Dict,
        *,
        content_detail_source: str,
        expected_aweme_id: str,
    ):
        content_item = parser.update_douyin_aweme(
            aweme_item, source_keyword=self.source_keyword,
            current_timestamp=self.ports.current_timestamp,
            save_data_option=self.settings.SAVE_DATA_OPTION,
            enable_get_medias=self.settings.ENABLE_GET_MEIDAS,
            content_detail_source=content_detail_source,
            expected_aweme_id=expected_aweme_id,
        )
        logger.info(f"[store.douyin.update_douyin_aweme] douyin aweme id:{aweme_item.get('aweme_id')}, title:{content_item.get('title')}")
        await self.ports.content_sink(self.crawler_type).store_content(content_item=content_item)

    async def update_dy_aweme_images(self, aweme_id: str, image_content_items: List[Dict]):
        return await self.ports.image_stager().store_post_images(aweme_id, image_content_items)

    async def record_dy_aweme_image_failure(self, aweme_id: str, image_content_item: Dict):
        return await self.ports.image_stager().record_failure(aweme_id, image_content_item)
