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
from trippostcollect.core import resources
from trippostcollect.core.paths import MEDIACRAWLER_DIR

import asyncio
import os
import random
from asyncio import Task
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Page,
    Playwright,
    async_playwright,
)

import config
from base.base_crawler import AbstractCrawler
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import douyin as douyin_store
from tools import utils
from tools.image_download_retry import (
    ImageDownloadFetchError,
    fetch_image_bytes_with_retry,
    is_runtime_blocking_image_error,
)
from tools.image_manifest import ImageStagingError
from tools.trippostcollect_behavior import project_browser_args, run_required_human_behavior
from tools.trippostcollect_adaptive import (
    AdaptiveAccumulator,
    append_execution_event,
    env_int,
    should_reseed_douyin_frontier,
)
from tools.cdp_browser import CDPBrowserManager
from trippostcollect.records.topic_relevance import (
    topic_relevant_for_web_post,
)
from var import crawler_type_var, source_keyword_var

from .client import DouYinClient
from .exception import DataFetchError, SearchResponseError
from .field import PublishTimeType
from .help import parse_video_info_from_url, parse_creator_info_from_url
from .login import DouYinLogin
from .search_safety import inspect_empty_first_page


class DouyinImageDownloadError(RuntimeError):
    """A Douyin note image exhausted its applicable fetch attempts."""

    def __init__(self, aweme_id: str, source_index: int, code: str, attempts: int = 1):
        super().__init__(
            f"Douyin image download failed: aweme_id={aweme_id}, "
            f"source_index={source_index}, code={code}"
        )
        self.aweme_id = aweme_id
        self.source_index = source_index
        self.code = code
        self.attempts = max(1, int(attempts))


class DouYinCrawler(AbstractCrawler):
    context_page: Page
    dy_client: DouYinClient
    browser_context: BrowserContext
    cdp_manager: Optional[CDPBrowserManager]

    def __init__(self) -> None:
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
        if config.ENABLE_IP_PROXY:
            self.ip_proxy_pool = await create_ip_pool(config.IP_PROXY_POOL_COUNT, enable_validate_ip=True)
            ip_proxy_info: IpInfoModel = await self.ip_proxy_pool.get_proxy()
            playwright_proxy_format, httpx_proxy_format = utils.format_proxy_info(ip_proxy_info)

        async with async_playwright() as playwright:
            # Select startup mode based on configuration
            if config.ENABLE_CDP_MODE:
                utils.logger.info("[DouYinCrawler] 使用CDP模式启动浏览器")
                self.browser_context = await self.launch_browser_with_cdp(
                    playwright,
                    playwright_proxy_format,
                    None,
                    headless=config.CDP_HEADLESS,
                )
            else:
                utils.logger.info("[DouYinCrawler] 使用标准模式启动浏览器")
                # Launch a browser context.
                chromium = playwright.chromium
                self.browser_context = await self.launch_browser(
                    chromium,
                    playwright_proxy_format,
                    user_agent=None,
                    headless=config.HEADLESS,
                )
                # stealth.min.js is a js script to prevent the website from detecting the crawler.
                with resources.path("js/stealth.min.js") as stealth_path:
                    await self.browser_context.add_init_script(path=str(stealth_path))

            self.context_page = await self.browser_context.new_page()
            await self.context_page.goto(self.index_url, wait_until="domcontentloaded")

            self.dy_client = await self.create_douyin_client(httpx_proxy_format)
            if not await self.dy_client.pong(browser_context=self.browser_context):
                login_obj = DouYinLogin(
                    login_type=config.LOGIN_TYPE,
                    login_phone="",  # you phone number
                    browser_context=self.browser_context,
                    context_page=self.context_page,
                    cookie_str=config.COOKIES,
                )
                await login_obj.begin()
                await self.dy_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )
            crawler_type_var.set(config.CRAWLER_TYPE)
            if config.CRAWLER_TYPE == "search":
                # Search for notes and retrieve their comment information.
                behavior_keyword = config.KEYWORDS.split(",", maxsplit=1)[0].strip()
                search_url = f"{self.index_url}/search/{quote(behavior_keyword)}?type=general"
                await self.context_page.goto(search_url, wait_until="domcontentloaded")
                await run_required_human_behavior(self.context_page, "douyin")
                await self.dy_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )
                await self.search()
            elif config.CRAWLER_TYPE == "detail":
                # Get the information and comments of the specified post
                behavior_keyword = config.KEYWORDS.split(",", maxsplit=1)[0].strip()
                source_keyword_var.set(behavior_keyword)
                search_url = f"{self.index_url}/search/{quote(behavior_keyword)}?type=general"
                await self.context_page.goto(search_url, wait_until="domcontentloaded")
                await run_required_human_behavior(self.context_page, "douyin")
                await self.dy_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )
                await self.get_specified_awemes()
            elif config.CRAWLER_TYPE == "creator":
                # Get the information and comments of the specified creator
                await run_required_human_behavior(self.context_page, "douyin")
                await self.get_creators_and_videos()

            utils.logger.info("[DouYinCrawler.start] Douyin Crawler finished ...")

    async def search(self) -> None:
        utils.logger.info("[DouYinCrawler.search] Begin search douyin keywords")
        dy_limit_count = 10  # Must match the search API count parameter.
        start_page = config.START_PAGE  # start page number
        accumulator = AdaptiveAccumulator.from_environment("douyin")
        for keyword in config.KEYWORDS.split(","):
            source_keyword_var.set(keyword)
            utils.logger.info(f"[DouYinCrawler.search] Current keyword: {keyword}")
            aweme_list: List[str] = []
            refresh_max_pages = env_int("TRIPPOSTCOLLECT_DISCOVERY_TOP_REFRESH_MAX_PAGES", 0)
            source_exhausted = os.environ.get("TRIPPOSTCOLLECT_DISCOVERY_SOURCE_EXHAUSTED") == "1"
            frontier_offset = env_int(
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_OFFSET",
                max(0, (start_page - 1) * dy_limit_count),
            )
            frontier_cursor = os.environ.get(
                "TRIPPOSTCOLLECT_DISCOVERY_RESUME_CURSOR",
                "",
            )
            phases: list[tuple[str, int, int, str, int | None]] = []
            if start_page > 1 and refresh_max_pages > 0:
                phases.append(("refresh", 1, 0, "", refresh_max_pages))
            if not source_exhausted:
                phases.append(("frontier", start_page, frontier_offset, frontier_cursor, None))

            refresh_session_cursor = ""
            refresh_resume_page: int | None = None
            refresh_resume_offset: int | None = None
            for discovery_phase, phase_page, phase_offset, phase_cursor, phase_limit in phases:
                if (
                    discovery_phase == "frontier"
                    and refresh_session_cursor
                    and phase_cursor != refresh_session_cursor
                ):
                    saved_page = phase_page
                    saved_offset = phase_offset
                    covered_by_refresh = bool(
                        refresh_resume_page is not None
                        and refresh_resume_offset is not None
                        and refresh_resume_offset >= phase_offset
                    )
                    if covered_by_refresh:
                        phase_page = refresh_resume_page
                        phase_offset = refresh_resume_offset
                    append_execution_event(
                        "douyin_frontier_cursor_rebound",
                        {
                            "platform": "douyin",
                            "saved_page": saved_page,
                            "saved_offset": saved_offset,
                            "resume_page": phase_page,
                            "resume_offset": phase_offset,
                            "saved_cursor_present": bool(phase_cursor),
                            "refresh_cursor_present": True,
                            "covered_by_refresh": covered_by_refresh,
                        },
                    )
                    phase_cursor = refresh_session_cursor
                refresh_candidate_before = len(accumulator.seen_candidate_identities)
                page = phase_page
                next_offset = phase_offset
                dy_search_id = phase_cursor
                phase_batches = 0
                source_has_more = None
                next_search_id = ""
                resume_page = page
                resume_offset = next_offset
                while (
                    accumulator.can_continue
                    and (phase_limit is None or phase_batches < phase_limit)
                ):
                    requested_page = page
                    requested_offset = next_offset
                    requested_search_id = dy_search_id
                    try:
                        utils.logger.info(
                            f"[DouYinCrawler.search] search douyin keyword: {keyword}, "
                            f"page: {requested_page}, offset: {requested_offset}"
                        )
                        posts_res = await self.dy_client.search_info_by_keyword(
                            keyword=keyword,
                            offset=requested_offset,
                            publish_time=PublishTimeType(config.PUBLISH_TIME_TYPE),
                            search_id=requested_search_id,
                        )
                    except SearchResponseError as exc:
                        utils.logger.error(
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
                    except DataFetchError:
                        utils.logger.error(
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
                    response_logid = posts_res.get("extra", {}).get("logid", "")
                    next_search_id = requested_search_id or response_logid
                    resume_page = requested_page + 1
                    resume_offset = requested_offset + dy_limit_count
                    fresh_first_page = bool(
                        requested_page == 1
                        and requested_offset == 0
                        and not requested_search_id
                    )
                    append_execution_event(
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
                            phase_batches += 1
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
                                count_stagnation=discovery_phase == "frontier",
                            ):
                                break
                            await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                            continue
                        if fresh_first_page:
                            page_check = await inspect_empty_first_page(self.context_page)
                            append_execution_event(
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
                        if discovery_phase == "frontier":
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
                        author_stats = douyin_store._normalized_author_stats(
                            author,
                            creator_profile,
                        )
                        statistics = aweme_info.get("statistics") or {}
                        valid = bool(
                            aweme_id
                            and aweme_info.get("desc")
                            and aweme_info.get("create_time")
                            and author.get("uid")
                            and author.get("nickname")
                            and douyin_store._extract_note_image_list(aweme_info)
                            and author_stats.get("followers_observed")
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
                                utils.logger.error(
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
                        await douyin_store.update_douyin_aweme(aweme_item=aweme_info)
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

                    await self.batch_get_note_comments(page_aweme_list)
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
                        count_stagnation=discovery_phase == "frontier",
                    ):
                        break
                    if source_has_more in (False, 0):
                        if discovery_phase == "frontier":
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
                    phase_batches += 1
                    await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                    utils.logger.info(
                        f"[DouYinCrawler.search] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} "
                        f"seconds after page {requested_page}"
                    )
                refresh_new_candidate_count = (
                    len(accumulator.seen_candidate_identities)
                    - refresh_candidate_before
                )
                if (
                    discovery_phase == "refresh"
                    and source_has_more in (True, 1)
                    and next_search_id
                    and not accumulator.stop_reason
                ):
                    refresh_session_cursor = next_search_id
                    refresh_resume_page = resume_page
                    refresh_resume_offset = resume_offset
                if (
                    discovery_phase == "refresh"
                    and should_reseed_douyin_frontier(
                        saved_source_exhausted=source_exhausted,
                        refresh_has_more=source_has_more,
                        refresh_next_cursor=next_search_id,
                        refresh_new_candidate_count=refresh_new_candidate_count,
                    )
                    and not accumulator.stop_reason
                ):
                    source_exhausted = False
                    phases.append(
                        ("frontier", resume_page, resume_offset, next_search_id, None)
                    )
                    append_execution_event(
                        "discovery_frontier_reseeded",
                        {
                            "platform": "douyin",
                            "reason": "new_candidates_after_saved_exhaustion",
                            "saved_resume_page": start_page,
                            "saved_resume_offset": frontier_offset,
                            "saved_resume_cursor": frontier_cursor,
                            "resume_page": resume_page,
                            "resume_offset": resume_offset,
                            "resume_cursor": next_search_id,
                            "refresh_new_candidate_count": refresh_new_candidate_count,
                        },
                    )
            if source_exhausted and not accumulator.stop_reason:
                accumulator.mark_source_exhausted(
                    "saved_source_exhausted",
                    source_page=start_page,
                    source_offset=frontier_offset,
                    source_cursor=frontier_cursor,
                    source_has_more=False,
                    raw_batch_count=0,
                    resume_page=start_page,
                    resume_offset=frontier_offset,
                    resume_cursor=frontier_cursor,
                    discovery_phase="frontier",
                )
            utils.logger.info(f"[DouYinCrawler.search] keyword:{keyword}, aweme_list:{aweme_list}")

    async def enrich_aweme_creator(self, aweme_info: Dict) -> Dict:
        if os.environ.get("TRIPPOSTCOLLECT_DOUYIN_ENRICH_CREATORS") != "1":
            return aweme_info
        author = aweme_info.get("author") or {}
        if os.environ.get("TRIPPOSTCOLLECT_DOUYIN_ENRICH_ONLY_IMAGES", "1") == "1":
            if not douyin_store._extract_note_image_list(aweme_info) and str(aweme_info.get("aweme_type")) not in {"68"}:
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
        max_enrich = int(os.environ.get("TRIPPOSTCOLLECT_DOUYIN_MAX_CREATOR_ENRICH", "30"))
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
            sleep_seconds = float(os.environ.get("TRIPPOSTCOLLECT_DOUYIN_CREATOR_SLEEP_SECONDS", "0.25"))
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
        except DataFetchError as exc:
            aweme_info["creator_profile_error"] = "detail_request_failed"
            self.creator_profile_failure_cache[sec_uid] = {
                "error": "detail_request_failed",
                "attempts": attempts,
            }
            utils.logger.warning(f"[DouYinCrawler.enrich_aweme_creator] get creator profile failed: {exc}")
        return aweme_info

    async def get_specified_awemes(self):
        """Get the information and comments of the specified post from URLs or IDs"""
        utils.logger.info("[DouYinCrawler.get_specified_awemes] Parsing video URLs...")
        aweme_id_list = []
        for video_url in config.DY_SPECIFIED_ID_LIST:
            try:
                video_info = parse_video_info_from_url(video_url)

                # Handling short links
                if video_info.url_type == "short":
                    utils.logger.info(f"[DouYinCrawler.get_specified_awemes] Resolving short link: {video_url}")
                    resolved_url = await self.dy_client.resolve_short_url(video_url)
                    if resolved_url:
                        # Extract video ID from parsed URL
                        video_info = parse_video_info_from_url(resolved_url)
                        utils.logger.info(f"[DouYinCrawler.get_specified_awemes] Short link resolved to aweme ID: {video_info.aweme_id}")
                    else:
                        utils.logger.error(f"[DouYinCrawler.get_specified_awemes] Failed to resolve short link: {video_url}")
                        continue

                aweme_id_list.append(video_info.aweme_id)
                utils.logger.info(f"[DouYinCrawler.get_specified_awemes] Parsed aweme ID: {video_info.aweme_id} from {video_url}")
            except ValueError as e:
                utils.logger.error(f"[DouYinCrawler.get_specified_awemes] Failed to parse video URL: {e}")
                continue

        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [self.get_aweme_detail(aweme_id=aweme_id, semaphore=semaphore) for aweme_id in aweme_id_list]
        aweme_details = await asyncio.gather(*task_list)
        for aweme_detail in aweme_details:
            if aweme_detail is not None:
                aweme_detail = await self.enrich_aweme_creator(aweme_detail)
                await douyin_store.update_douyin_aweme(aweme_item=aweme_detail)
                await self.get_aweme_media(aweme_item=aweme_detail)
        await self.batch_get_note_comments(aweme_id_list)

    async def get_aweme_detail(self, aweme_id: str, semaphore: asyncio.Semaphore) -> Any:
        """Get note detail"""
        async with semaphore:
            try:
                result = await self.dy_client.get_video_by_id(aweme_id)
                # Sleep after fetching aweme detail
                await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                utils.logger.info(f"[DouYinCrawler.get_aweme_detail] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching aweme {aweme_id}")
                return result
            except DataFetchError as ex:
                utils.logger.error(f"[DouYinCrawler.get_aweme_detail] Get aweme detail error: {ex}")
                return None
            except KeyError as ex:
                utils.logger.error(f"[DouYinCrawler.get_aweme_detail] have not fund note detail aweme_id:{aweme_id}, err: {ex}")
                return None
            except Exception as ex:
                utils.logger.warning(f"[DouYinCrawler.get_aweme_detail] Skip aweme after request error aweme_id:{aweme_id}, err: {ex}")
                return None

    async def batch_get_note_comments(self, aweme_list: List[str]) -> None:
        """
        Batch get note comments
        """
        if not config.ENABLE_GET_COMMENTS:
            utils.logger.info("[DouYinCrawler.batch_get_note_comments] Crawling comment mode is not enabled")
            return

        task_list: List[Task] = []
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        for aweme_id in aweme_list:
            task = asyncio.create_task(self.get_comments(aweme_id, semaphore), name=aweme_id)
            task_list.append(task)
        if len(task_list) > 0:
            await asyncio.wait(task_list)

    async def get_comments(self, aweme_id: str, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            try:
                # Pass the list of keywords to the get_aweme_all_comments method
                # Use fixed crawling interval
                crawl_interval = config.CRAWLER_MAX_SLEEP_SEC
                await self.dy_client.get_aweme_all_comments(
                    aweme_id=aweme_id,
                    crawl_interval=crawl_interval,
                    is_fetch_sub_comments=config.ENABLE_GET_SUB_COMMENTS,
                    callback=douyin_store.batch_update_dy_aweme_comments,
                    max_count=config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES,
                )
                # Sleep after fetching comments
                await asyncio.sleep(crawl_interval)
                utils.logger.info(f"[DouYinCrawler.get_comments] Sleeping for {crawl_interval} seconds after fetching comments for aweme {aweme_id}")
                utils.logger.info(f"[DouYinCrawler.get_comments] aweme_id: {aweme_id} comments have all been obtained and filtered ...")
            except DataFetchError as e:
                utils.logger.error(f"[DouYinCrawler.get_comments] aweme_id: {aweme_id} get comments failed, error: {e}")

    async def get_creators_and_videos(self) -> None:
        """
        Get the information and videos of the specified creator from URLs or IDs
        """
        utils.logger.info("[DouYinCrawler.get_creators_and_videos] Begin get douyin creators")
        utils.logger.info("[DouYinCrawler.get_creators_and_videos] Parsing creator URLs...")

        for creator_url in config.DY_CREATOR_ID_LIST:
            try:
                creator_info_parsed = parse_creator_info_from_url(creator_url)
                user_id = creator_info_parsed.sec_user_id
                utils.logger.info(f"[DouYinCrawler.get_creators_and_videos] Parsed sec_user_id: {user_id} from {creator_url}")
            except ValueError as e:
                utils.logger.error(f"[DouYinCrawler.get_creators_and_videos] Failed to parse creator URL: {e}")
                continue

            creator_info: Dict = await self.dy_client.get_user_info(user_id)
            if creator_info:
                await douyin_store.save_creator(user_id, creator=creator_info)

            # Get all video information of the creator
            all_video_list = await self.dy_client.get_all_user_aweme_posts(sec_user_id=user_id, callback=self.fetch_creator_video_detail)

            video_ids = [video_item.get("aweme_id") for video_item in all_video_list]
            await self.batch_get_note_comments(video_ids)

    async def fetch_creator_video_detail(self, video_list: List[Dict]):
        """
        Concurrently obtain the specified post list and save the data
        """
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [self.get_aweme_detail(post_item.get("aweme_id"), semaphore) for post_item in video_list]

        note_details = await asyncio.gather(*task_list)
        for aweme_item in note_details:
            if aweme_item is not None:
                await douyin_store.update_douyin_aweme(aweme_item=aweme_item)
                await self.get_aweme_media(aweme_item=aweme_item)

    async def create_douyin_client(self, httpx_proxy: Optional[str]) -> DouYinClient:
        """Create douyin client"""
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
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
            proxy_ip_pool=self.ip_proxy_pool,  # Pass proxy pool for automatic refresh
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
        if config.SAVE_LOGIN_STATE:
            user_data_dir = os.path.join(MEDIACRAWLER_DIR, "browser_data", config.USER_DATA_DIR % config.PLATFORM)  # type: ignore
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
            self.cdp_manager = CDPBrowserManager()
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
            utils.logger.info(f"[DouYinCrawler] CDP浏览器信息: {browser_info}")

            return browser_context

        except Exception as e:
            utils.logger.error(f"[DouYinCrawler] CDP模式启动失败，回退到标准模式: {e}")
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
        utils.logger.info("[DouYinCrawler.close] Browser context closed ...")

    async def get_aweme_media(self, aweme_item: Dict):
        """
        Compatibility entrypoint for strict note-image-only downloads.

        Args:
            aweme_item (Dict): 抖音作品详情
        """
        if not config.ENABLE_GET_MEIDAS:
            utils.logger.info("[DouYinCrawler.get_aweme_media] Crawling image mode is not enabled")
            return
        await self.get_aweme_images(aweme_item)

    async def get_aweme_images(self, aweme_item: Dict):
        """
        get aweme images. please use get_aweme_media

        Args:
            aweme_item (Dict): 抖音作品详情
        """
        if not config.ENABLE_GET_MEIDAS:
            return
        aweme_id = str(aweme_item.get("aweme_id") or "")
        image_assets = douyin_store._extract_note_image_assets(aweme_item)
        if not image_assets:
            return
        fetched_assets: List[Dict] = []
        for asset in image_assets:
            source_index = int(asset["source_index"])
            try:
                content, attempts = await fetch_image_bytes_with_retry(
                    lambda: self.dy_client.get_aweme_media(asset["url"]),
                    logger=utils.logger,
                    label=(
                        f"platform=douyin aweme_id={aweme_id} "
                        f"source_index={source_index}"
                    ),
                )
            except ImageDownloadFetchError as exc:
                await douyin_store.record_dy_aweme_image_failure(
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
                await douyin_store.record_dy_aweme_image_failure(
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
            await douyin_store.update_dy_aweme_images(aweme_id, fetched_assets)
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
            await douyin_store.record_dy_aweme_image_failure(
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

    async def get_aweme_video(self, aweme_item: Dict):
        """
        get aweme videos. please use get_aweme_media

        Args:
            aweme_item (Dict): 抖音作品详情
        """
        if not config.ENABLE_GET_MEIDAS:
            return
        aweme_id = aweme_item.get("aweme_id")

        # The video URL will always exist, but when it is a short video type, the file is actually an audio file.
        video_download_url: str = douyin_store._extract_video_download_url(aweme_item)

        if not video_download_url:
            return
        content = await self.dy_client.get_aweme_media(video_download_url)
        await asyncio.sleep(random.random())
        if content is None:
            return
        extension_file_name = "video.mp4"
        await douyin_store.update_dy_aweme_video(aweme_id, content, extension_file_name)
