# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/douyin/client.py
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

import asyncio
import copy
import json
import random
import re
import urllib.parse
from typing import TYPE_CHECKING, Any, Callable, Dict, Union, Optional

import httpx
from playwright.async_api import BrowserContext

from base.base_crawler import AbstractApiClient
from proxy.proxy_mixin import ProxyRefreshMixin
from tools import utils
from tools.httpx_util import make_async_client
from tools.image_download_retry import (
    ImageDownloadFetchError,
    classified_http_image_error,
)
from var import request_keyword_var

if TYPE_CHECKING:
    from proxy.proxy_ip_pool import ProxyIpPool

from .exception import *
from .field import *
from .help import *
from .search_safety import decode_douyin_json_body, validate_douyin_search_response


FIRST_PAGE_VISIBLE_FALLBACK_REASONS = {
    "search_verify_check",
    "invalid_stream_chunk_header",
    "invalid_stream_chunk_size",
    "truncated_stream_chunk",
    "invalid_stream_json",
}


class DouYinClient(AbstractApiClient, ProxyRefreshMixin):

    def __init__(
        self,
        timeout=60,  # If the crawl media option is turned on, Douyin’s short videos will require a longer timeout.
        proxy=None,
        *,
        headers: Dict,
        playwright_page: Optional[Page],
        cookie_dict: Dict,
        proxy_ip_pool: Optional["ProxyIpPool"] = None,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.headers = headers
        self._host = "https://www.douyin.com"
        self.cookie_urls = [
            "https://douyin.com",
            self._host,
            "https://creator.douyin.com",
            "https://douhot.douyin.com",
            "https://live.douyin.com",
        ]
        self.playwright_page = playwright_page
        self.cookie_dict = cookie_dict
        self._observed_search_responses: list[dict[str, Any]] = []
        self._observed_search_response_tasks: set[asyncio.Task] = set()
        if self.playwright_page is not None:
            self.playwright_page.on("response", self._schedule_browser_search_response)
        # Initialize proxy pool (from ProxyRefreshMixin)
        self.init_proxy_pool(proxy_ip_pool)

    def _schedule_browser_search_response(self, response: Any) -> None:
        if "/aweme/v1/web/general/search/" not in response.url:
            return
        task = asyncio.create_task(self.capture_browser_search_response(response))
        self._observed_search_response_tasks.add(task)
        task.add_done_callback(self._observed_search_response_tasks.discard)

    async def capture_browser_search_response(self, response: Any) -> None:
        """Cache a browser-issued search response so the API client does not repeat it."""
        try:
            parsed_url = urllib.parse.urlparse(response.url)
            query = urllib.parse.parse_qs(parsed_url.query)
            payload = decode_douyin_json_body(await response.body())
            record = {
                "keyword": (query.get("keyword") or [""])[0],
                "offset": int((query.get("offset") or ["0"])[0]),
                "search_id": (query.get("search_id") or [""])[0],
                "payload": payload,
            }
        except Exception as exc:
            reason = getattr(exc, "reason", type(exc).__name__)
            utils.logger.warning(
                "[DouYinClient.capture_browser_search_response] browser response ignored, "
                f"reason: {reason}"
            )
            return
        self._observed_search_responses.append(record)
        nil_info = payload.get("search_nil_info")
        utils.logger.info(
            "[DouYinClient.capture_browser_search_response] observed browser response, "
            f"offset: {record['offset']}, search_id_present: {bool(record['search_id'])}, "
            f"data_count: {len(payload.get('data') or [])}, has_more: {payload.get('has_more')}, "
            f"search_nil_type: {nil_info.get('search_nil_type') if isinstance(nil_info, dict) else ''}"
        )

    async def _take_observed_search_response(
        self,
        *,
        keyword: str,
        offset: int,
        search_id: str,
    ) -> Optional[Dict[str, Any]]:
        if self._observed_search_response_tasks:
            await asyncio.gather(
                *tuple(self._observed_search_response_tasks),
                return_exceptions=True,
            )
        matches = [
            (index, record)
            for index, record in enumerate(self._observed_search_responses)
            if (
                record["keyword"] == keyword
                and record["offset"] == offset
                and record["search_id"] == search_id
            )
        ]
        if not matches:
            return None

        selected_index, selected_record = matches[-1]
        for candidate_index, candidate_record in reversed(matches):
            nil_info = candidate_record["payload"].get("search_nil_info")
            if not (
                isinstance(nil_info, dict)
                and nil_info.get("search_nil_type") == "verify_check"
            ):
                selected_index, selected_record = candidate_index, candidate_record
                break
        matched_indexes = {index for index, _ in matches}
        self._observed_search_responses = [
            record
            for index, record in enumerate(self._observed_search_responses)
            if index not in matched_indexes
        ]
        payload = selected_record["payload"]
        nil_info = payload.get("search_nil_info")
        utils.logger.info(
            "[DouYinClient.search_info_by_keyword] reuse browser search response, "
            f"offset: {offset}, search_id_present: {bool(search_id)}, "
            f"duplicates: {len(matches)}, selected_index: {selected_index}, "
            f"data_count: {len(payload.get('data') or [])}, "
            f"search_nil_type: {nil_info.get('search_nil_type') if isinstance(nil_info, dict) else ''}"
        )
        return payload

    async def _scroll_for_observed_search_response(
        self,
        *,
        keyword: str,
        offset: int,
        search_id: str,
    ) -> bool:
        """Drive the visible results page until its own request reaches the wanted offset."""
        if self.playwright_page is None or offset <= 0 or not search_id:
            return False
        for attempt in range(1, 9):
            try:
                viewport = self.playwright_page.viewport_size or {"width": 1920, "height": 1080}
                await self.playwright_page.mouse.move(
                    max(10, int(viewport["width"] * random.uniform(0.68, 0.88))),
                    max(10, int(viewport["height"] * random.uniform(0.55, 0.82))),
                    steps=random.randint(3, 7),
                )
                await self.playwright_page.mouse.wheel(0, random.randint(650, 1050))
                await self.playwright_page.wait_for_timeout(random.randint(1400, 2400))
            except Exception as exc:
                utils.logger.warning(
                    "[DouYinClient._scroll_for_observed_search_response] visible scroll failed, "
                    f"offset: {offset}, attempt: {attempt}, reason: {type(exc).__name__}"
                )
                return False
            if self._observed_search_response_tasks:
                await asyncio.gather(
                    *tuple(self._observed_search_response_tasks),
                    return_exceptions=True,
                )
            if any(
                record["keyword"] == keyword
                and record["offset"] == offset
                and record["search_id"] == search_id
                for record in self._observed_search_responses
            ):
                utils.logger.info(
                    "[DouYinClient._scroll_for_observed_search_response] browser page reached offset, "
                    f"offset: {offset}, attempts: {attempt}"
                )
                return True
        utils.logger.warning(
            "[DouYinClient._scroll_for_observed_search_response] browser page did not reach offset, "
            f"offset: {offset}, attempts: 8"
        )
        return False

    async def _build_visible_first_page_fallback(
        self,
        *,
        keyword: str,
        offset: int,
        search_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Rebuild page zero only when visible cards and a healthy next page prove the chain."""
        if offset != 0 or search_id or self.playwright_page is None:
            return None
        if self._observed_search_response_tasks:
            await asyncio.gather(
                *tuple(self._observed_search_response_tasks),
                return_exceptions=True,
            )
        next_page_records = []
        for record in self._observed_search_responses:
            payload = record["payload"]
            nil_info = payload.get("search_nil_info")
            if (
                record["keyword"] == keyword
                and record["offset"] > 0
                and record["search_id"]
                and payload.get("data")
                and not (
                    isinstance(nil_info, dict)
                    and nil_info.get("search_nil_type") == "verify_check"
                )
            ):
                next_page_records.append(record)
        if not next_page_records:
            return None
        next_page = min(next_page_records, key=lambda record: record["offset"])
        expected_first_page_count = next_page["offset"]
        try:
            visible_ids = await self.playwright_page.locator(
                '[id^="waterfall_item_"]:visible'
            ).evaluate_all(
                r"""elements => elements
                    .map(element => String(element.id || '').replace('waterfall_item_', ''))
                    .filter(value => /^\d+$/.test(value))"""
            )
        except Exception as exc:
            utils.logger.warning(
                "[DouYinClient._build_visible_first_page_fallback] visible card read failed, "
                f"reason: {type(exc).__name__}"
            )
            return None
        first_page_ids = list(dict.fromkeys(visible_ids))[:expected_first_page_count]
        if len(first_page_ids) < expected_first_page_count:
            utils.logger.warning(
                "[DouYinClient._build_visible_first_page_fallback] insufficient visible cards, "
                f"expected: {expected_first_page_count}, observed: {len(first_page_ids)}"
            )
            return None

        data = []
        for aweme_id in first_page_ids:
            try:
                aweme_detail = await self.get_video_by_id(aweme_id)
            except Exception as exc:
                utils.logger.warning(
                    "[DouYinClient._build_visible_first_page_fallback] detail fetch failed, "
                    f"aweme_id: {aweme_id}, reason: {type(exc).__name__}"
                )
                continue
            if isinstance(aweme_detail, dict) and aweme_detail.get("aweme_id"):
                data.append({"aweme_info": aweme_detail})
        if not data:
            return None
        utils.logger.info(
            "[DouYinClient._build_visible_first_page_fallback] rebuilt visible first page, "
            f"visible_ids: {len(first_page_ids)}, detail_count: {len(data)}, "
            f"next_offset: {next_page['offset']}, next_search_id_present: True"
        )
        return {
            "status_code": 0,
            "data": data,
            "has_more": 1,
            "extra": {"logid": next_page["search_id"]},
        }

    async def __process_req_params(
        self,
        uri: str,
        params: Optional[Dict] = None,
        headers: Optional[Dict] = None,
        request_method="GET",
    ):

        if not params:
            return
        headers = headers or self.headers
        browser_facts = await self.playwright_page.evaluate(  # type: ignore
            """() => ({
                language: navigator.language || 'zh-CN',
                platform: navigator.platform || 'MacIntel',
                userAgent: navigator.userAgent || '',
                online: navigator.onLine,
                hardwareConcurrency: navigator.hardwareConcurrency || 8,
                deviceMemory: navigator.deviceMemory || 8,
                screenWidth: window.screen.width || 1920,
                screenHeight: window.screen.height || 1080,
                effectiveType: navigator.connection?.effectiveType || '4g',
                downlink: navigator.connection?.downlink || 10,
                rtt: navigator.connection?.rtt || 50,
            })"""
        )
        local_storage: Dict = await self.playwright_page.evaluate("() => window.localStorage")  # type: ignore
        user_agent = str(browser_facts.get("userAgent") or headers.get("User-Agent") or "")
        browser_version_match = re.search(r"(?:Chrome|Chromium)/(\d+(?:\.\d+){0,3})", user_agent)
        browser_version = browser_version_match.group(1) if browser_version_match else "125.0.0.0"
        web_id = ""
        for storage_key in ("__tea_cache_tokens_1300", "__tea_cache_tokens_6383"):
            try:
                web_id = str(json.loads(local_storage.get(storage_key, "{}")).get("web_id") or "")
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
            if web_id:
                break
        platform_name = str(browser_facts.get("platform") or "MacIntel")
        common_params = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "version_code": "190600",
            "version_name": "19.6.0",
            "pc_client_type": "1",
            "cookie_enabled": "true",
            "browser_language": str(browser_facts.get("language") or "zh-CN"),
            "browser_platform": platform_name,
            "browser_name": "Chrome",
            "browser_version": browser_version,
            "browser_online": str(bool(browser_facts.get("online", True))).lower(),
            "engine_name": "Blink",
            "engine_version": browser_version,
            "os_name": "Mac OS" if "Mac" in platform_name else platform_name,
            "os_version": "10.15.7",
            "cpu_core_num": str(browser_facts.get("hardwareConcurrency") or 8),
            "device_memory": str(browser_facts.get("deviceMemory") or 8),
            "platform": "PC",
            "screen_width": str(browser_facts.get("screenWidth") or 1920),
            "screen_height": str(browser_facts.get("screenHeight") or 1080),
            "effective_type": str(browser_facts.get("effectiveType") or "4g"),
            "downlink": str(browser_facts.get("downlink") or 10),
            "round_trip_time": str(browser_facts.get("rtt") or 50),
            "webid": web_id or get_web_id(),
            "msToken": local_storage.get("xmst"),
            "update_version_code": "0" if "/general/search/stream/" in uri else "170400",
        }
        verify_fp = str(self.cookie_dict.get("s_v_web_id") or "")
        uifid = str(self.cookie_dict.get("UIFID_TEMP") or self.cookie_dict.get("UIFID") or "")
        if verify_fp:
            common_params.update({"verifyFp": verify_fp, "fp": verify_fp})
        if uifid:
            common_params["uifid"] = uifid
        params.update(common_params)
        query_string = urllib.parse.urlencode(params)

        # 20240927 a-bogus update (JS version)
        post_data = {}
        if request_method == "POST":
            post_data = params

        if "/v1/web/general/search" not in uri:
            a_bogus = await get_a_bogus(uri, query_string, post_data, headers["User-Agent"], self.playwright_page)
            params["a_bogus"] = a_bogus

    async def request(self, method, url, **kwargs):
        # Check whether the proxy has expired before each request
        await self._refresh_proxy_if_expired()

        async with make_async_client(proxy=self.proxy) as client:
            response = await client.request(method, url, timeout=self.timeout, **kwargs)
        try:
            if response.content == b"" or response.text == "blocked":
                utils.logger.error(f"request params incrr, response.text: {response.text}")
                raise Exception("account blocked")
            return decode_douyin_json_body(response.content)
        except SearchResponseError as exc:
            utils.logger.error(
                "[DouYinClient.request] search response decode failed, "
                f"reason: {exc.reason}, bytes: {len(response.content)}, "
                f"prefix_hex: {response.content[:48].hex()}"
            )
            raise
        except Exception as e:
            raise DataFetchError(f"{e}, {response.text}")

    async def get(self, uri: str, params: Optional[Dict] = None, headers: Optional[Dict] = None):
        """
        GET请求
        """
        await self.__process_req_params(uri, params, headers)
        headers = headers or self.headers
        return await self.request(method="GET", url=f"{self._host}{uri}", params=params, headers=headers)

    async def post(self, uri: str, data: dict, headers: Optional[Dict] = None):
        await self.__process_req_params(uri, data, headers)
        headers = headers or self.headers
        return await self.request(method="POST", url=f"{self._host}{uri}", data=data, headers=headers)

    async def pong(self, browser_context: BrowserContext) -> bool:
        local_storage = await self.playwright_page.evaluate("() => window.localStorage")
        if local_storage.get("HasUserLogin", "") == "1":
            return True

        _, cookie_dict = await utils.convert_browser_context_cookies(
            browser_context,
            urls=self.cookie_urls,
        )
        return cookie_dict.get("LOGIN_STATUS") == "1"

    async def update_cookies(self, browser_context: BrowserContext, urls: Optional[list[str]] = None):
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            browser_context,
            urls=urls or self.cookie_urls,
        )
        self.headers["Cookie"] = cookie_str
        self.cookie_dict = cookie_dict

    async def search_info_by_keyword(
        self,
        keyword: str,
        offset: int = 0,
        search_channel: SearchChannelType = SearchChannelType.GENERAL,
        sort_type: SearchSortType = SearchSortType.GENERAL,
        publish_time: PublishTimeType = PublishTimeType.UNLIMITED,
        search_id: str = "",
    ):
        """
        DouYin Web Search API
        :param keyword:
        :param offset:
        :param search_channel:
        :param sort_type:
        :param publish_time: ·
        :param search_id: ·
        :return:
        """
        observed_response = await self._take_observed_search_response(
            keyword=keyword,
            offset=offset,
            search_id=search_id,
        )
        if observed_response is None and offset > 0 and search_id:
            await self._scroll_for_observed_search_response(
                keyword=keyword,
                offset=offset,
                search_id=search_id,
            )
            observed_response = await self._take_observed_search_response(
                keyword=keyword,
                offset=offset,
                search_id=search_id,
            )
        if observed_response is not None:
            return validate_douyin_search_response(observed_response)

        query_params = {
            'search_channel': search_channel.value,
            'enable_history': '1',
            'keyword': keyword,
            'search_source': 'normal_search',
            'query_correct_type': '1',
            'is_filter_search': '0',
            'from_group_id': '',
            'disable_rs': '0',
            'offset': offset,
            'count': '10',
            'need_filter_settings': '0',
            'list_type': 'single',
            'pc_search_top_1_params': json.dumps(
                {"enable_ai_search_top_1": 1},
                separators=(",", ":"),
            ),
            'search_id': search_id,
            'pc_libra_divert': 'Mac',
            'support_h265': '1',
            'support_dash': '1',
        }
        if sort_type.value != SearchSortType.GENERAL.value or publish_time.value != PublishTimeType.UNLIMITED.value:
            query_params["filter_selected"] = json.dumps({"sort_type": str(sort_type.value), "publish_time": str(publish_time.value)})
            query_params["is_filter_search"] = 1
            query_params["search_source"] = "tab_search"
        referer_url = f"https://www.douyin.com/search/{keyword}?aid=f594bbd9-a0e2-4651-9319-ebe3cb6298c1&type=general"
        headers = copy.copy(self.headers)
        headers["Referer"] = urllib.parse.quote(referer_url, safe=':/')
        endpoint = (
            "/aweme/v1/web/general/search/stream/"
            if offset == 0 and not search_id
            else "/aweme/v1/web/general/search/single/"
        )
        if endpoint.endswith("/stream/"):
            query_params["need_filter_settings"] = "1"
        try:
            response = await self.get(
                endpoint,
                query_params,
                headers=headers,
            )
            return validate_douyin_search_response(response)
        except SearchResponseError as exc:
            if exc.reason not in FIRST_PAGE_VISIBLE_FALLBACK_REASONS:
                raise
            fallback = await self._build_visible_first_page_fallback(
                keyword=keyword,
                offset=offset,
                search_id=search_id,
            )
            if fallback is None:
                raise
            return validate_douyin_search_response(fallback)

    async def get_video_by_id(self, aweme_id: str) -> Any:
        """
        DouYin Video Detail API
        :param aweme_id:
        :return:
        """
        params = {"aweme_id": aweme_id}
        headers = copy.copy(self.headers)
        del headers["Origin"]
        res = await self.get("/aweme/v1/web/aweme/detail/", params, headers)
        return res.get("aweme_detail", {})

    async def get_aweme_comments(self, aweme_id: str, cursor: int = 0):
        """get note comments

        """
        uri = "/aweme/v1/web/comment/list/"
        params = {"aweme_id": aweme_id, "cursor": cursor, "count": 20, "item_type": 0}
        keywords = request_keyword_var.get()
        referer_url = "https://www.douyin.com/search/" + keywords + '?aid=3a3cec5a-9e27-4040-b6aa-ef548c2c1138&publish_time=0&sort_type=0&source=search_history&type=general'
        headers = copy.copy(self.headers)
        headers["Referer"] = urllib.parse.quote(referer_url, safe=':/')
        return await self.get(uri, params)

    async def get_sub_comments(self, aweme_id: str, comment_id: str, cursor: int = 0):
        """
            获取子评论
        """
        uri = "/aweme/v1/web/comment/list/reply/"
        params = {
            'comment_id': comment_id,
            "cursor": cursor,
            "count": 20,
            "item_type": 0,
            "item_id": aweme_id,
        }
        keywords = request_keyword_var.get()
        referer_url = "https://www.douyin.com/search/" + keywords + '?aid=3a3cec5a-9e27-4040-b6aa-ef548c2c1138&publish_time=0&sort_type=0&source=search_history&type=general'
        headers = copy.copy(self.headers)
        headers["Referer"] = urllib.parse.quote(referer_url, safe=':/')
        return await self.get(uri, params)

    async def get_aweme_all_comments(
        self,
        aweme_id: str,
        crawl_interval: float = 1.0,
        is_fetch_sub_comments=False,
        callback: Optional[Callable] = None,
        max_count: int = 10,
    ):
        """
        获取帖子的所有评论，包括子评论
        :param aweme_id: 帖子ID
        :param crawl_interval: 抓取间隔
        :param is_fetch_sub_comments: 是否抓取子评论
        :param callback: 回调函数，用于处理抓取到的评论
        :param max_count: 一次帖子爬取的最大评论数量
        :return: 评论列表
        """
        result = []
        comments_has_more = 1
        comments_cursor = 0
        while comments_has_more and len(result) < max_count:
            comments_res = await self.get_aweme_comments(aweme_id, comments_cursor)
            comments_has_more = comments_res.get("has_more", 0)
            comments_cursor = comments_res.get("cursor", 0)
            comments = comments_res.get("comments", [])
            if not comments:
                continue
            if len(result) + len(comments) > max_count:
                comments = comments[:max_count - len(result)]
            result.extend(comments)
            if callback:  # If there is a callback function, execute the callback function
                await callback(aweme_id, comments)

            await asyncio.sleep(crawl_interval)
            if not is_fetch_sub_comments:
                continue
            # Get secondary reviews
            for comment in comments:
                reply_comment_total = comment.get("reply_comment_total")

                if reply_comment_total > 0:
                    comment_id = comment.get("cid")
                    sub_comments_has_more = 1
                    sub_comments_cursor = 0

                    while sub_comments_has_more:
                        sub_comments_res = await self.get_sub_comments(aweme_id, comment_id, sub_comments_cursor)
                        sub_comments_has_more = sub_comments_res.get("has_more", 0)
                        sub_comments_cursor = sub_comments_res.get("cursor", 0)
                        sub_comments = sub_comments_res.get("comments", [])

                        if not sub_comments:
                            continue
                        result.extend(sub_comments)
                        if callback:  # If there is a callback function, execute the callback function
                            await callback(aweme_id, sub_comments)
                        await asyncio.sleep(crawl_interval)
        return result

    async def get_user_info(self, sec_user_id: str):
        uri = "/aweme/v1/web/user/profile/other/"
        params = {
            "sec_user_id": sec_user_id,
            "publish_video_strategy_type": 2,
            "personal_center_strategy": 1,
        }
        return await self.get(uri, params)

    async def get_user_aweme_posts(self, sec_user_id: str, max_cursor: str = "") -> Dict:
        uri = "/aweme/v1/web/aweme/post/"
        params = {
            "sec_user_id": sec_user_id,
            "count": 18,
            "max_cursor": max_cursor,
            "locate_query": "false",
            "publish_video_strategy_type": 2,
        }
        return await self.get(uri, params)

    async def get_all_user_aweme_posts(self, sec_user_id: str, callback: Optional[Callable] = None):
        posts_has_more = 1
        max_cursor = ""
        result = []
        while posts_has_more == 1:
            aweme_post_res = await self.get_user_aweme_posts(sec_user_id, max_cursor)
            posts_has_more = aweme_post_res.get("has_more", 0)
            max_cursor = aweme_post_res.get("max_cursor")
            aweme_list = aweme_post_res.get("aweme_list") if aweme_post_res.get("aweme_list") else []
            utils.logger.info(f"[DouYinClient.get_all_user_aweme_posts] get sec_user_id:{sec_user_id} video len : {len(aweme_list)}")
            if callback:
                await callback(aweme_list)
            result.extend(aweme_list)
        return result

    async def get_aweme_media(self, url: str) -> Union[bytes, None]:
        async with make_async_client(proxy=self.proxy) as client:
            try:
                response = await client.request("GET", url, timeout=self.timeout, follow_redirects=True)
                response.raise_for_status()
                return response.content
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                utils.logger.error(
                    f"[DouYinClient.get_aweme_media] HTTP {status} for {exc.request.url}"
                )
                raise classified_http_image_error(status, f"HTTP {status}") from exc
            except httpx.HTTPError as exc:  # transport error without an HTTP response
                utils.logger.error(f"[DouYinClient.get_aweme_media] {exc.__class__.__name__} for {exc.request.url} - {exc}")  # Keep the original exception type name for developers to debug
                raise ImageDownloadFetchError(
                    str(exc), code="image_download_retryable", retryable=True
                ) from exc

    async def resolve_short_url(self, short_url: str) -> str:
        """
        解析抖音短链接,获取重定向后的真实URL
        Args:
            short_url: 短链接,如 https://v.douyin.com/iF12345ABC/
        Returns:
            重定向后的完整URL
        """
        async with make_async_client(proxy=self.proxy, follow_redirects=False) as client:
            try:
                utils.logger.info(f"[DouYinClient.resolve_short_url] Resolving short URL: {short_url}")
                response = await client.get(short_url, timeout=10)

                # Short links usually return a 302 redirect
                if response.status_code in [301, 302, 303, 307, 308]:
                    redirect_url = response.headers.get("Location", "")
                    utils.logger.info(f"[DouYinClient.resolve_short_url] Resolved to: {redirect_url}")
                    return redirect_url
                else:
                    utils.logger.warning(f"[DouYinClient.resolve_short_url] Unexpected status code: {response.status_code}")
                    return ""
            except Exception as e:
                utils.logger.error(f"[DouYinClient.resolve_short_url] Failed to resolve short URL: {e}")
                return ""
